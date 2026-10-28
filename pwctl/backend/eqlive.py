"""Live equalizer controls: writing a running filter graph's values.

A biquad-engine equalizer (`enhance.ENGINE_BIQUAD`) is a filter-chain whose
nodes expose `<node>:<Port>` controls (`pre:Mult`, `b3:Gain`, ...).  Writing
one changes the sound at once, with no restart and nothing moved -- measured
in docs.local/probes/2026-10-27-eq-engine.  Three facts shape this module:

- **Every write clicks.**  PipeWire's `biquad_set()` clears the filter's state
  on any change, so a drag must not write per pixel.  Writes to one node are
  kept `interval` apart, the newest value wins, and the last one is always
  written.
- **A spawned `pw-cli` costs ~20 ms; a persistent one costs nothing** per
  write.  So one `pw-cli` reads `s <id> Props {...}` lines on stdin.
- **`pw-cli` exits 0 for a write to a node that is gone**, printing
  `Error: "s: unknown global '<id>'"`.  Its output is read, and those
  errors are kept so a caller can tell its node id went stale.
- **A fresh `pw-cli` knows no objects until it prints "remote 0 is named"**
  (~5 ms): a write sent before that fails as "unknown global".  And it
  **does not exit when its stdin closes**, so `close()` waits for an answer
  to a last harmless command (`i 0`) before stopping it.

No GTK and no `enhance` import: `enhance` uses `write_now()` from here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
import weakref

# Measured: a 1 kHz sine written 20 times a second shows a step per write
# (max second difference 0.119 against 0.0057 clean).  ~7 writes a second
# while dragging keeps the curve audible without a buzz.  The final value is
# always written, so a slow interval only delays it, never loses it.
LIVE_INTERVAL = 0.15

_UNKNOWN = re.compile(r"unknown global '(\d+)'")

# The persistent pw-cli runs under this name (argv[0]), so an orphan left by
# a crash can be found and ended at the next start -- pw-cli does not exit
# when its stdin closes -- and it shows as this client in pw-dump.
WRITER_NAME = 'pwctl-eq-writer'
_writers: 'weakref.WeakSet[ControlWriter]' = weakref.WeakSet()


def format_params(controls: dict) -> str:
    """`{ params = [ "b1:Gain" 3.5 ... ] }` -- the Props pod pw-cli takes."""
    parts = []
    for key, value in controls.items():
        parts.append(f'"{key}" {float(value):.9g}')
    return '{ params = [ ' + ' '.join(parts) + ' ] }'


def command(node_id: int, controls: dict) -> str:
    return f's {int(node_id)} Props {format_params(controls)}'


def write_now(node_id: int, controls: dict, run=None) -> bool:
    """One write, synchronously, with a spawned pw-cli.  For one-off writes
    (a Save, a bypass) where starting a writer thread is not worth it."""
    if run is None:
        from .system import run
    rc, out, err = run(['pw-cli', 's', str(int(node_id)), 'Props',
                        format_params(controls)], timeout=5)
    return rc == 0 and 'unknown global' not in (out + err)


def read_controls(dump, node_name: str) -> dict | None:
    """The live `<node>:<Port>` values of a running filter graph, by node
    name; None when no such node is running or it exposes no controls.

    They are in the node's second `Props` entry (the first is
    audioconvert's own), as a flat `[name, value, name, value, ...]` list.
    """
    for obj in dump or []:
        if obj.get('type') != 'PipeWire:Interface:Node':
            continue
        info = obj.get('info') or {}
        if (info.get('props') or {}).get('node.name') != node_name:
            continue
        for props in (info.get('params') or {}).get('Props') or []:
            flat = props.get('params') if isinstance(props, dict) else None
            if not flat:
                continue
            pairs = dict(zip(flat[0::2], flat[1::2]))
            graph = {k: v for k, v in pairs.items()
                     if isinstance(k, str) and ':' in k
                     and isinstance(v, (int, float))
                     and not isinstance(v, bool)}
            if graph:
                return graph
        return None
    return None


class ControlWriter:
    """Throttled, coalescing, latest-wins writes through one `pw-cli`.

    `write()` never blocks.  `flush()` waits until everything asked for has
    been sent.  `close()` flushes and ends the `pw-cli`.  If `pw-cli` is
    missing or dies, writes fall back to `write_now()`.
    """

    def __init__(self, interval: float = LIVE_INTERVAL, popen=None,
                 fallback=None):
        self.interval = interval
        self._popen = popen or subprocess.Popen
        self._fallback = fallback or write_now
        self._cv = threading.Condition()
        self._pending: dict[int, dict] = {}
        self._last: dict[int, float] = {}
        self._busy = False
        self._closed = False
        self._proc = None
        self._ready = threading.Event()
        self._acked = threading.Event()
        self.sent = 0                 # writes sent, for tests and diagnostics
        self.stale: set[int] = set()  # ids pw-cli called "unknown global"
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='eq-control-writer')
        self._thread.start()
        _writers.add(self)

    # -- public --
    def write(self, node_id: int, controls: dict, urgent: bool = False):
        if not controls:
            return
        with self._cv:
            if self._closed:
                return
            merged = self._pending.setdefault(int(node_id), {})
            merged.update(controls)
            if urgent:
                self._last.pop(int(node_id), None)
            self._cv.notify_all()

    def flush(self, timeout: float = 3.0) -> bool:
        end = time.monotonic() + timeout
        with self._cv:
            while self._pending or self._busy:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                # a pending write may be waiting out its interval: let it go
                for nid in list(self._pending):
                    self._last.pop(nid, None)
                self._cv.notify_all()
                self._cv.wait(min(left, 0.05))
        return True

    def close(self, timeout: float = 3.0):
        self.flush(timeout)
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        self._thread.join(timeout)
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        # pw-cli runs lines in order: once `i 0` is answered, every write
        # before it has been handed to PipeWire.
        self._acked.clear()
        try:
            proc.stdin.write('i 0\n')
            proc.stdin.flush()
        except (OSError, ValueError):
            pass
        self._acked.wait(1.0)
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()

    # -- worker --
    def _run(self):
        while True:
            with self._cv:
                while not self._closed and not self._pending:
                    self._cv.wait()
                if self._closed and not self._pending:
                    return
                now = time.monotonic()
                due, wait = [], None
                for nid in self._pending:
                    gap = now - self._last.get(nid, -1e9)
                    if gap >= self.interval:
                        due.append(nid)
                    else:
                        left = self.interval - gap
                        wait = left if wait is None else min(wait, left)
                if not due:
                    self._cv.wait(wait)
                    continue
                batch = {nid: self._pending.pop(nid) for nid in due}
                self._busy = True
            for nid, controls in batch.items():
                self._send(nid, controls)
            with self._cv:
                stamp = time.monotonic()
                for nid in batch:
                    self._last[nid] = stamp
                self._busy = False
                self._cv.notify_all()

    def _send(self, node_id: int, controls: dict):
        line = command(node_id, controls) + '\n'
        for _ in range(2):
            proc = self._ensure()
            if proc is None:
                break
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
                self.sent += 1
                return
            except (BrokenPipeError, OSError, ValueError):
                self._proc = None
        if self._fallback(node_id, controls):
            self.sent += 1
        else:
            self.stale.add(int(node_id))

    def _ensure(self):
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return proc
        try:
            proc = self._popen([WRITER_NAME],
                               executable=shutil.which('pw-cli') or 'pw-cli',
                               stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1)
        except (OSError, ValueError):
            self._proc = None
            return None
        self._proc = proc
        self._ready.clear()
        if getattr(proc, 'stdout', None) is not None:
            threading.Thread(target=self._drain, args=(proc,), daemon=True,
                             name='eq-control-reader').start()
            self._ready.wait(2.0)
        return proc

    def _drain(self, proc):
        try:
            for line in proc.stdout:
                if 'is named' in line:
                    self._ready.set()
                elif line.strip() == 'id: 0':
                    self._acked.set()
                m = _UNKNOWN.search(line)
                if m:
                    self.stale.add(int(m.group(1)))
        except (OSError, ValueError):
            pass
        self._ready.set()
        self._acked.set()


def stop_all(timeout: float = 1.0):
    """End every writer of this process (app shutdown)."""
    for writer in list(_writers):
        writer.close(timeout)


def reap_orphans() -> int:
    """End writers an earlier run left behind (startup only; skips ours)."""
    import os
    import signal
    from pathlib import Path

    me = os.getpid()
    marker = WRITER_NAME.encode() + b'\0'
    killed = 0
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if not (entry / 'cmdline').read_bytes().startswith(marker):
                continue
            stat = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(stat[1]) == me:
                continue
            os.kill(int(entry.name), signal.SIGTERM)
            killed += 1
        except (OSError, ValueError, IndexError):
            continue
    return killed
