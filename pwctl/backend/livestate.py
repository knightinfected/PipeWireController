"""The audio graph, handed to pages when it changes instead of on a timer.

Pages that draw live state ran `pw-dump` every 3 s whether or not anything
had moved.  This module replaces those timers with two long-lived listeners
used only as a doorbell:

* `pw-dump -m -N`.  Its messages are filtered to the object types a page
  draws (Node, Port, Link, Device, Metadata), minus our own level meters'
  node, ports and links, and minus a Port whose only change is its params
  (see _port_params_only).  What is left at rest is nothing: 0 messages in
  60 s, with or without playback and meters (measured 2026-10-15).  The
  `Client` churn the unfiltered stream carries was our own polling.
* `pw-metadata -m -n default`, because on PipeWire 1.0.x `pw-dump -m` never
  prints WirePlumber's default-device change (measured in Ubuntu 24.04
  containers).  Any line it prints rings the bell.  WirePlumber makes a new
  `default` metadata object when it restarts, and a running `pw-metadata`
  stays bound to the old one, deaf; the graph listener sees the new one
  appear and this one is restarted (see new_default_metadata).

A ring is not data.  After 50 ms of quiet (500 ms at most, and never
within 500 ms of the last re-read, so a slider drag costs two a second) one
fresh `pw.pw_dump()` runs, and every
subscriber gets it: exactly the parsed list a page's own poll used to get.
The stream is never used as a copy of the graph, because Metadata messages
carry only the changed keys, a deleted key is invisible and ids are
recycled (all measured).  A failed dump ([] -- a real graph is never empty)
is "cannot tell" and is not delivered, so pages keep what they have.

The listeners exit when PipeWire restarts (rc 0, after announcing every
object as removed).  Each is respawned with backoff (100 ms -> 2 s), and
until the graph has been quiet for 300 ms (2 s at most) after that, rings
wait, because the first dump after a restart is partial (5, then 7, then 9
audio nodes by 294 ms).  A graph listener that keeps failing to start puts
a 3 s timer behind the same interface, and a 30 s re-read runs regardless,
for any gap nobody has found yet.

subscribe() answers None when `pw-dump` is not installed: the caller keeps
its own timer.  Nothing is cached.  Call everything from the main thread;
callbacks run on its main context.  The listeners run while anyone is
subscribed, and stop() is also called from the app's shutdown.

Design: vault DR-004.  Probe: docs.local/probes/2026-10-15-live-state-options/.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import traceback
from pathlib import Path

from gi.repository import GLib

from . import levels, pw

KEEP = frozenset({'Node', 'Port', 'Link', 'Device', 'Metadata'})

# argv[0] is replaced by a marker so a later run can find a listener this one
# left behind (see reap_orphans).  The binary is still found on PATH.
GRAPH = ('pw-dump', ['pwctl-livestate-graph', '-m', '-N'])
META = ('pw-metadata', ['pwctl-livestate-meta', '-m', '-n', 'default'])
MARKER = 'pwctl-livestate-'

QUIET_MS, CAP_MS = 50, 500                  # bursts end <= 90 ms, gaps <= 24
# A slider drag writes once per step, often more than 50 ms apart, so quiet
# alone would re-read on every step: rings never start dumps closer than this.
MIN_GAP_MS = 500
SETTLE_QUIET_MS, SETTLE_CAP_MS = 300, 2000  # after a listener died
BACKOFF_MS, BACKOFF_MAX_MS = 100, 2000
SAFETY_S = 30
FALLBACK_AFTER = 3          # failed graph starts in a row before the timer
FALLBACK_S = 3


def _now_ms() -> int:
    return GLib.get_monotonic_time() // 1000


def _short(kind) -> str | None:
    return kind.rsplit(':', 1)[-1] if isinstance(kind, str) else None


def _removed(obj) -> bool:
    """`{"id": N, "info": null}`: a removal is the only shape with no type.

    Not "no info": Metadata, SecurityContext and Profiler objects carry no
    `info` key at all (their content is `props` and `metadata`).
    """
    return obj.get('type') is None


def _is_meter(obj, meters: set) -> bool:
    """Our own level meter: its node, the node's ports, the link feeding it.

    Classified from the object itself every time, and `meters` kept in step,
    because ids are recycled: a freed meter id can come back as a real node.
    """
    oid = obj.get('id')
    if _removed(obj):
        if oid in meters:
            meters.discard(oid)
            return True
        return False
    info = obj.get('info') or {}
    props = info.get('props') or {}
    hit = bool(props.get(levels.MARKER)
               or props.get('node.name') == levels.METER_NODE
               or props.get('node.id') in meters
               or info.get('input-node-id') in meters)
    if hit:
        meters.add(oid)
    else:
        meters.discard(oid)
    return hit


def _port_params_only(obj) -> bool:
    """A Port whose only change is its params: format and buffers, which
    are renegotiated whenever anything links to it or unlinks, our own
    meters included (measured: a meter starting re-sends its target's two
    monitor ports with change-mask ['params'], and again when it stops).
    No page reads a port's params; graph.snapshot() reads its props and
    direction.  A message with no change-mask is kept."""
    mask = (obj.get('info') or {}).get('change-mask')
    return isinstance(mask, list) and mask == ['params']


def rings(objs, types: dict, meters: set) -> bool:
    """Whether one `pw-dump -m` message is worth a fresh dump.

    `types` maps id -> short type, because a removal (`{"id": N, "info":
    null}`) carries no type at all.  A removal of an id never announced does
    not ring: the listener primes with the whole graph, so that object was
    created later and gone before pw-dump printed it, a client living a few
    milliseconds (measured: 1 of 120 one-shot clients).  Ringing for it would
    let our own one-shot dumps ring the bell again.
    """
    ring = False
    for obj in objs:
        if not isinstance(obj, dict):
            ring = True
            continue
        oid = obj.get('id')
        if _removed(obj):
            kind = types.pop(oid, None)
            if kind is None:
                continue                    # never announced: see above
        else:
            kind = _short(obj.get('type'))
            types[oid] = kind
        meter = _is_meter(obj, meters)     # always, to keep `meters` right
        if kind is None:
            ring = True
        elif kind in KEEP and not meter:
            if not (kind == 'Port' and _port_params_only(obj)):
                ring = True
    return ring


def new_default_metadata(objs, known: set) -> bool:
    """Whether a NEW `default` Metadata object appeared in this message.

    WirePlumber owns that object and makes a new one when it restarts.
    `pw-metadata -m` stays bound to the old one and goes deaf: alive, but
    printing nothing ever again (measured on 1.0.5 and 1.6.2, containers).
    `pw-dump -m` does show the swap, on 1.0.5 too.  `known` holds the ids
    seen so far and is kept in step.
    """
    new = False
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        if _removed(obj):
            known.discard(obj.get('id'))
        elif (_short(obj.get('type')) == 'Metadata'
              and (obj.get('props') or {}).get('metadata.name') == 'default'):
            if obj.get('id') not in known:
                known.add(obj.get('id'))
                new = True
    return new


class _Sub:
    __slots__ = ('callback', 'live')

    def __init__(self, callback):
        self.callback = callback
        self.live = True


class _Child:
    """One listener process and the thread reading it."""

    def __init__(self, kind, gen):
        self.kind = kind            # 'graph' or 'meta'
        self.gen = gen
        self.proc = None
        self.primed = False         # it has said something since it started


class _Bell:
    def __init__(self):
        self.subs: list[_Sub] = []
        self.active = False         # between start() and stop()
        self.gen = 0                # bumped by stop(): stale posts are dropped
        self.children: dict[str, _Child] = {}
        self.fails = {'graph': 0, 'meta': 0}
        self.respawn_src = {'graph': 0, 'meta': 0}
        self.settling = False
        self.window = None          # [first ring, last ring, quiet, cap] in ms
        self.window_src = 0
        self.dumping = False
        self.again = False
        self.last_dump = -MIN_GAP_MS    # ms, when the last dump started
        self.safety_src = 0
        self.fallback_src = 0
        self.stats = {'dumps': 0, 'delivered': 0, 'spawns': 0, 'rings': 0}

    # ------------------------------------------------------------ children --
    def _spawn(self, kind) -> bool:
        exe, argv = GRAPH if kind == 'graph' else META
        child = _Child(kind, self.gen)
        try:
            child.proc = subprocess.Popen(
                argv, executable=exe, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                encoding='utf-8', errors='replace')
        except OSError:
            return False
        self.children[kind] = child
        self.stats['spawns'] += 1
        reader = self._read_graph if kind == 'graph' else self._read_meta
        threading.Thread(target=reader, args=(child,), daemon=True).start()
        return True

    def _read_graph(self, child):
        # pw-dump ends every top-level message with a line that is exactly
        # `]` (put_end(d, "]\n", 0)); nested lines are indented and a JSON
        # string cannot hold a raw newline.  Checked against a character-level
        # framer on every message of the October probe.
        types, meters, lines, defaults = {}, set(), [], set()
        first = True
        for line in child.proc.stdout:
            lines.append(line)
            if line.rstrip('\r\n') != ']':
                continue
            text, lines = ''.join(lines), []
            try:
                objs = json.loads(text)
            except ValueError:
                objs = None
            if first:
                # The whole graph, which teaches the filter every id's type
                # and every meter already running.
                first = False
                if objs is not None:
                    rings(objs, types, meters)
                    new_default_metadata(objs, defaults)
                GLib.idle_add(self._primed, child)
                continue
            if objs is not None and new_default_metadata(objs, defaults):
                GLib.idle_add(self._rebind_meta, child)
            if objs is None or rings(objs, types, meters):
                GLib.idle_add(self._ring_from, child)
        child.proc.wait()
        GLib.idle_add(self._exited, child)

    def _read_meta(self, child):
        first = True
        for _line in child.proc.stdout:
            if first:
                first = False
                GLib.idle_add(self._primed, child)
            # Its first lines are the current values, which ring once and
            # fold into the start's own window.
            GLib.idle_add(self._ring_from, child)
        child.proc.wait()
        GLib.idle_add(self._exited, child)

    def _current(self, child) -> bool:
        return (child.gen == self.gen
                and self.children.get(child.kind) is child)

    def _primed(self, child):
        if self._current(child):
            child.primed = True
            self.fails[child.kind] = 0
            if child.kind == 'graph':
                if self.fallback_src:
                    GLib.source_remove(self.fallback_src)
                    self.fallback_src = 0
                self._ring()
        return False

    def _ring_from(self, child):
        if self._current(child):
            self._ring()
        return False

    def _rebind_meta(self, graph):
        # WirePlumber made a new `default` metadata: the running pw-metadata
        # is bound to the old one and will never speak again.  Restart it; a
        # fresh one binds the new object (its exit respawns it).
        meta = self.children.get('meta')
        if self._current(graph) and meta is not None:
            _kill(meta.proc)
        return False

    def _exited(self, child):
        if not self._current(child):
            return False
        del self.children[child.kind]
        # The graph listener's last words are "every object was removed",
        # which is our own restart, not news: drop that ring, and make every
        # ring wait for the graph to settle until the respawn has been read.
        self.settling = True
        if child.kind == 'graph':
            self._close_window()
        self._schedule_respawn(child.kind, failed=not child.primed)
        return False

    def _schedule_respawn(self, kind, failed):
        if failed:
            self.fails[kind] += 1
        if (kind == 'graph' and self.fails['graph'] >= FALLBACK_AFTER
                and not self.fallback_src):
            self.fallback_src = GLib.timeout_add_seconds(
                FALLBACK_S, self._fallback_tick)
        n = max(self.fails[kind], 1)
        delay = min(BACKOFF_MS * 2 ** (n - 1), BACKOFF_MAX_MS)
        self.respawn_src[kind] = GLib.timeout_add(delay, self._respawn,
                                                  kind, self.gen)

    def _respawn(self, kind, gen):
        self.respawn_src[kind] = 0
        if gen == self.gen and self.active and kind not in self.children:
            if not self._spawn(kind):
                self._schedule_respawn(kind, failed=True)
        return False

    def _fallback_tick(self):
        self._dump()
        return True

    def _safety_tick(self):
        self._dump()
        return True

    # ------------------------------------------------------------- window --
    def _close_window(self):
        if self.window_src:
            GLib.source_remove(self.window_src)
            self.window_src = 0
        self.window = None

    def _ring(self):
        self.stats['rings'] += 1
        quiet, cap = ((SETTLE_QUIET_MS, SETTLE_CAP_MS) if self.settling
                      else (QUIET_MS, CAP_MS))
        now = _now_ms()
        w = self.window
        if w is None:
            w = self.window = [now, now, quiet, cap]
        else:
            w[1] = now
            w[2], w[3] = max(w[2], quiet), max(w[3], cap)
        due = max(min(w[1] + w[2], w[0] + w[3]),
                  self.last_dump + MIN_GAP_MS)
        if self.window_src:
            GLib.source_remove(self.window_src)
        self.window_src = GLib.timeout_add(max(due - now, 0),
                                           self._window_due)

    def _window_due(self):
        self.window_src = 0
        self.window = None
        self._dump()
        return False

    # --------------------------------------------------------------- dump --
    def _dump(self):
        if self.dumping:
            self.again = True           # once more when this one lands
            return
        self.dumping = True
        self.last_dump = _now_ms()
        self.stats['dumps'] += 1
        gen = self.gen

        def work():
            try:
                dump = pw.pw_dump()
            except Exception:           # never leave `dumping` stuck
                dump = []
            GLib.idle_add(self._dumped, dump, gen)
        threading.Thread(target=work, daemon=True).start()

    def _dumped(self, dump, gen):
        if gen != self.gen:
            return False
        self.dumping = False
        if dump:
            graph = self.children.get('graph')
            if graph is not None and graph.primed:
                self.settling = False
            self.stats['delivered'] += 1
            for sub in list(self.subs):
                if sub.live:
                    try:
                        sub.callback(dump)
                    except Exception:   # one page must not starve the rest
                        traceback.print_exc()
        if self.again and self.subs:
            self.again = False
            self._dump()
        return False

    # ---------------------------------------------------------- lifecycle --
    def start(self) -> bool:
        self.settling = False
        self.fails = {'graph': 0, 'meta': 0}
        if not self._spawn('graph'):
            return False
        self.active = True
        self._spawn('meta')             # optional: only 1.0.x needs it
        self.safety_src = GLib.timeout_add_seconds(SAFETY_S,
                                                   self._safety_tick)
        return True

    def stop(self):
        self.active = False
        self.gen += 1
        children, self.children = list(self.children.values()), {}
        for kind, src in self.respawn_src.items():
            if src:
                GLib.source_remove(src)
            self.respawn_src[kind] = 0
        for attr in ('safety_src', 'fallback_src'):
            if getattr(self, attr):
                GLib.source_remove(getattr(self, attr))
                setattr(self, attr, 0)
        self._close_window()
        self.dumping = self.again = False
        self.settling = False
        for child in children:
            _kill(child.proc)


def _kill(proc):
    """By PID, never by pattern (the pkill -f self-kill gotcha)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass


_bell = _Bell()


def subscribe(callback):
    """Call callback(dump) on the main loop whenever the graph changes.

    `dump` is a fresh, parsed `pw-dump`, shared by every subscriber: treat it
    as read-only.  One arrives shortly after subscribing, then one per change.
    Returns a token for unsubscribe(), or None when `pw-dump` is not there
    to listen with: the caller keeps polling then.
    """
    if shutil.which(GRAPH[0]) is None:
        return None
    sub = _Sub(callback)
    if not _bell.active:
        if not _bell.start():
            return None
        _bell.subs.append(sub)          # the listener's first ring serves it
    else:
        _bell.subs.append(sub)
        refresh()
    return sub


def unsubscribe(token):
    """Stop delivering to `token`; the listeners stop with the last one."""
    if token is None or not token.live:
        return
    token.live = False
    if token in _bell.subs:
        _bell.subs.remove(token)
    if not _bell.subs:
        _bell.stop()


def refresh():
    """Deliver a fresh dump now, without waiting for a change.

    For a page that changed something no event covers (its own rules file),
    or that needs the graph re-read after a failed action.  A dump already
    on its way is followed by one more.
    """
    if not _bell.active:
        return
    _bell._close_window()
    _bell._dump()


def stop():
    """Stop the listeners and forget every subscriber (app shutdown)."""
    for sub in _bell.subs:
        sub.live = False
    _bell.subs = []
    _bell.stop()


def listener_pids() -> list[int]:
    """The listeners' PIDs, for tests and for the orphan check."""
    return [c.proc.pid for c in _bell.children.values() if c.proc]


def reap_orphans() -> int:
    """Kill listeners left behind by an earlier run of the app.

    A crash or a `kill -9` skips stop().  An orphaned `pw-dump -m` dies at
    its next write, but `pw-metadata -m` can wait hours for one.  The app is
    single-instance, so at startup any process whose argv[0] carries our
    marker belongs to a run that is gone.  Startup only.
    """
    me = os.getpid()
    marker = MARKER.encode()
    killed = 0
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv0 = (entry / 'cmdline').read_bytes().split(b'\0', 1)[0]
            if not argv0.startswith(marker):
                continue
            stat = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(stat[1]) == me:
                continue
            os.kill(int(entry.name), signal.SIGTERM)
            killed += 1
        except (OSError, ValueError, IndexError):
            continue
    return killed
