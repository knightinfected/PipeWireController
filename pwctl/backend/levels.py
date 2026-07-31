"""Live audio level metering.

One `pw-record` per metered node, but *not* streaming audio: setting
`resample.peaks=true` makes PipeWire's own resampler emit peak values instead
of samples, so at `--rate 25` we read a single float per update — ~100 bytes/s
per meter instead of ~96 kB/s.

The capture stream is marked `node.passive` (never drives the graph, so
metering can't hold a device awake) and `stream.monitor` (it is a monitor tap,
not a recording).

Backend module: no GTK imports.  The reader threads only store the newest raw
peak; all ballistics (dB mapping, decay, peak-hold, silence-on-stall) are
computed in `level()` from elapsed time, so the UI can draw at whatever frame
rate it likes and still get frame-rate-independent behaviour.

    levels.subscribe(serial)          # start metering (ref-counted)
    disp, hold = levels.level(serial) # 0..1 for drawing, called per frame
    levels.unsubscribe(serial)        # stop when the row goes away
"""

from __future__ import annotations

import math
import struct
import subprocess
import threading
import time

from . import system

# -- capture ----------------------------------------------------------------

PEAK_RATE = 25          # peak updates per second (PipeWire resampler side)

# Safety cap so a huge graph can't spawn unbounded capture processes.  Each
# pw-record costs ~0.6 MB proportional (its ~9 MB RSS is nearly all shared
# libraries) plus one passive node in the graph, so 40 is roughly 25 MB — and
# 24, the original value, was below the endpoint count of the machines that
# actually asked for meters: a mixer full of plugin sinks has 30-40 of them.
# Rows that don't get a slot show no meter at all rather than an empty one
# (see ui/volume.py), and pick one up as soon as another row lets go.
MAX_METERS = 40

# A meter is a real capture stream, and some desktop shells raise an "app is
# recording" indicator for those.  `stream.monitor=true` is the property that
# declares this a monitor tap rather than a recording; we identify ourselves
# honestly and let shells filter on that.  If one ever proves to need more,
# the fix is ours to make here — not to borrow another application's identity.
MARKER = 'pwctl.meter'      # stamped on every meter stream so our own graph
                            # and monitor views can hide them by flag rather
                            # than by matching node names
METER_NODE = 'pwctl.meter'  # node.name of the capture taps.  pw-top only
                            # prints node.name, so the Monitor page has to
                            # recognise our own taps by name there.

# -- ballistics -------------------------------------------------------------

FLOOR_DB = -60.0        # bottom of the scale; peaks below this read as empty
DECAY_PER_SEC = 0.9     # how fast the bar falls (full scale in ~1.1 s)
HOLD_SEC = 1.2          # peak marker sits still this long before falling
HOLD_DECAY_PER_SEC = 0.35
STALE_SEC = 0.4         # no data this long => silence, not a frozen bar


def available() -> bool:
    return system.have('pw-record')


def to_norm(peak: float) -> float:
    """Linear peak (0..1) -> 0..1 position on a dB scale.

    Music sits far lower than people expect — normal listening is around
    -25 dBFS, which is 5% of a linear bar but ~58% of a dB one.  Metering
    linearly makes a working meter look dead, so everything is mapped
    through dB before it reaches the screen.
    """
    if peak <= 0.0 or not math.isfinite(peak):
        return 0.0
    db = 20.0 * math.log10(min(peak, 1.0))
    if db <= FLOOR_DB:
        return 0.0
    return min(1.0, (db - FLOOR_DB) / -FLOOR_DB)


class _Meter:
    """One capture process + reader thread for one node serial."""

    def __init__(self, serial: int):
        self.serial = serial
        self.refs = 1
        self.error: str | None = None

        self._lock = threading.Lock()
        self._peak = 0.0            # newest raw linear peak from the thread
        self._stamp = 0.0           # when it arrived
        self._started = time.monotonic()

        # display state, advanced by level()
        self._disp = 0.0
        self._hold = 0.0
        self._hold_stamp = 0.0
        self._tick = time.monotonic()

        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        # Guards the check-then-spawn in _run against a concurrent stop().  A
        # row that appears and disappears within a frame (fast scrolling, page
        # switch) would otherwise be told to stop before the thread had
        # spawned anything, and then spawn an orphan nobody owns.
        self._proc_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # -- capture side ------------------------------------------------------
    def _props(self) -> str:
        # SPA JSON, so any value containing a space must be quoted or
        # pw-record rejects the whole string with "Expected object key" and
        # never starts.
        props = {
            'resample.peaks': 'true',   # emit peaks, not audio
            'node.passive': 'true',     # never drive the graph
            'stream.monitor': 'true',   # a monitor tap, not a recording
            'media.name': 'Peak detect',
            'node.name': METER_NODE,
            'application.name': 'PipeWire Controller',
            MARKER: 'true',
        }
        return ' '.join(f'{k}="{v}"' for k, v in props.items())

    def _run(self):
        argv = ['pw-record', '--target', str(self.serial),
                '-P', self._props(),
                '--rate', str(PEAK_RATE), '--channels', '1',
                '--format', 'f32', '--container', 'raw', '-']
        with self._proc_lock:
            if self._stop.is_set():
                return              # stopped before we ever spawned
            try:
                # stderr is kept, not discarded: a bad target or a malformed
                # property makes pw-record exit immediately, and without this
                # the only symptom is a meter that sits at zero forever.
                self._proc = subprocess.Popen(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    bufsize=0)
            except OSError as e:
                self.error = str(e)
                return

        stdout = self._proc.stdout
        while not self._stop.is_set():
            try:
                raw = stdout.read(4)        # exactly one f32 = one peak
            except (OSError, ValueError):
                break
            if not raw or len(raw) < 4:
                break                       # EOF: target vanished or we quit
            value = struct.unpack('<f', raw)[0]
            if not math.isfinite(value):
                continue
            with self._lock:
                self._peak = abs(value)
                self._stamp = time.monotonic()

        if not self._stop.is_set() and self.error is None:
            self.error = self._stderr_text() or 'capture ended'

    def _stderr_text(self) -> str:
        """pw-record's complaint, if it had one.

        It echoes the output filename ('-') on stderr before anything else,
        so the first line is never the interesting one.
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ''
        try:
            raw = proc.stderr.read(512) or b''
        except (OSError, ValueError):
            return ''
        lines = [ln.strip() for ln in
                 raw.decode('utf-8', 'replace').splitlines() if ln.strip()]
        lines = [ln for ln in lines if ln != '-']
        for ln in lines:
            if 'error' in ln.lower():
                return ln
        return lines[0] if lines else ''

    def stop(self):
        # Set the flag first, then take the lock: whichever side wins, the
        # process is either never spawned or spawned and then killed here.
        self._stop.set()
        with self._proc_lock:
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        for pipe in (proc.stdout, proc.stderr) if proc else ():
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass

    # -- display side ------------------------------------------------------
    def level(self) -> tuple[float, float]:
        now = time.monotonic()
        with self._lock:
            peak, stamp = self._peak, self._stamp

        # A paused or suspended node simply stops sending peaks.  Without this
        # the bar would freeze at its last value instead of falling to
        # silence — the classic frozen-meter bug in every mixer that ships
        # one.
        target = 0.0 if (stamp and now - stamp > STALE_SEC) else to_norm(peak)

        dt = max(0.0, now - self._tick)
        self._tick = now

        # instant attack, timed decay
        self._disp = max(target, self._disp - DECAY_PER_SEC * dt)
        self._disp = min(1.0, max(0.0, self._disp))

        if target >= self._hold:
            self._hold = target
            self._hold_stamp = now
        elif now - self._hold_stamp > HOLD_SEC:
            self._hold = max(self._disp, self._hold - HOLD_DECAY_PER_SEC * dt)
        self._hold = min(1.0, max(0.0, self._hold))

        return self._disp, self._hold

    def live(self) -> bool:
        """True once real peaks are arriving (not merely 'process started')."""
        with self._lock:
            stamp = self._stamp
        return bool(stamp) and time.monotonic() - stamp <= STALE_SEC


# -- registry ---------------------------------------------------------------

_meters: dict[int, _Meter] = {}
_registry_lock = threading.Lock()


def subscribe(serial: int) -> bool:
    """Start (or ref-count) metering for a node serial.

    Note this is the object *serial*, not the object id — `pw-record --target`
    resolves serials, and the two only coincide for early-created nodes.
    """
    try:
        serial = int(serial)
    except (TypeError, ValueError):
        return False
    if serial <= 0 or not available():
        return False
    with _registry_lock:
        meter = _meters.get(serial)
        if meter is not None:
            meter.refs += 1
            return True
        if len(_meters) >= MAX_METERS:
            return False
        _meters[serial] = _Meter(serial)
        return True


def unsubscribe(serial: int):
    """Drop one reference; the capture process stops at the last one."""
    try:
        serial = int(serial)
    except (TypeError, ValueError):
        return
    with _registry_lock:
        meter = _meters.get(serial)
        if meter is None:
            return
        meter.refs -= 1
        if meter.refs > 0:
            return
        del _meters[serial]
    meter.stop()


def level(serial: int) -> tuple[float, float]:
    """(bar, peak-hold) as 0..1, ready to draw.  Unmetered nodes read 0."""
    with _registry_lock:
        meter = _meters.get(int(serial) if serial else -1)
    return meter.level() if meter is not None else (0.0, 0.0)


def live(serial: int) -> bool:
    """True when this node is actually passing audio right now."""
    with _registry_lock:
        meter = _meters.get(int(serial) if serial else -1)
    return meter.live() if meter is not None else False


def active_serials() -> list[int]:
    with _registry_lock:
        return sorted(_meters)


def at_capacity() -> bool:
    """True when every meter slot is taken.

    Lets the UI tell "no slot free right now, try again later" apart from
    "this system can't meter at all", so it only retries in the first case.
    """
    with _registry_lock:
        return len(_meters) >= MAX_METERS


def stop_all():
    """Tear down every meter — call on shutdown, however it was triggered."""
    with _registry_lock:
        meters = list(_meters.values())
        _meters.clear()
    for meter in meters:
        meter.stop()


def reap_orphans() -> int:
    """Kill meter captures left behind by an earlier run.

    A pw-record is a child process, not a thread: if the app is killed or
    crashes it keeps running, keeps a monitor link open on its device and
    keeps showing up in pw-top forever.  The app is single-instance, so at
    startup any capture carrying our marker belongs to a run that is gone.

    Startup only — it deliberately skips our own children, but nothing else
    distinguishes a live meter from an abandoned one.
    """
    import os
    import signal
    from pathlib import Path

    me = os.getpid()
    marker = MARKER.encode()
    killed = 0
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / 'cmdline').read_bytes()
            if b'pw-record' not in cmdline or marker not in cmdline:
                continue
            # never touch a capture this process started
            stat = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(stat[1]) == me:
                continue
            os.kill(int(entry.name), signal.SIGTERM)
            killed += 1
        except (OSError, ValueError, IndexError):
            continue        # vanished, or not ours to read
    return killed


if __name__ == '__main__':      # manual check: python3 -m pwctl.backend.levels <serial>
    import sys
    if len(sys.argv) < 2:
        raise SystemExit('usage: levels.py <object.serial> [seconds]')
    target = int(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    if not subscribe(target):
        raise SystemExit(f'could not meter serial {target}')
    try:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            bar, hold = level(target)
            flag = 'LIVE' if live(target) else '    '
            print(f'\r{flag} |{"#" * int(bar * 40):<40}| '
                  f'{bar * 100:5.1f}%  hold {hold * 100:5.1f}%', end='', flush=True)
            time.sleep(1 / 60)
    finally:
        print()
        meter = _meters.get(target)
        if meter is not None and meter.error:
            print(f'error: {meter.error}')
        stop_all()
