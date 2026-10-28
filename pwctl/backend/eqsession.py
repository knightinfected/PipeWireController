"""An equalizer editing session: the editor's model, without GTK.

The session holds the saved curve, the curve being edited and an undo
history, and keeps a running equalizer *playing the edit*:

- a change a control write can make (a gain, a frequency, a Q, a band on or
  off, the preamp, bypass) goes to `eqlive.ControlWriter` at once;
- a change it cannot (a band added, removed or retyped, or the first edit of
  an equalizer still running the old file engine) restarts the unit on the
  edited curve (`enhance.preview`), at most one restart at a time and always
  ending on the newest curve.

Nothing is saved until `commit()`.  `cancel()` puts back whatever plays.
While the running sound is not the saved one, `enhance.mark_live()` says so
on disk, so a crash is put right next start (`enhance.reconcile`).

Threads: edits come from the UI thread and never block.  Anything that runs
a command goes through `run_async(fn, done)` -- `widgets.async_call` in the
app, a plain call in a test.
"""

from __future__ import annotations

from . import enhance
from . import eqcurve as E
from .eqlive import ControlWriter

# States the editor shows.
OFF = 'off'                # not running: edits apply when saved
LIVE = 'live'              # edits are heard as they are made
RESTARTING = 'restarting'  # a band was added/removed/retyped: coming back
FAILED = 'failed'          # the last restart failed; edits still apply on save

_LABEL_TO_CODE = {t.builtin: t.code for t in E.TYPES.values()}


def _running_types(conf) -> tuple | None:
    """The band types a running biquad graph was built with, or None."""
    if not isinstance(conf, dict):
        return None
    for module in conf.get('context.modules') or []:
        if module.get('name') != 'libpipewire-module-filter-chain':
            continue
        nodes = ((module.get('args') or {}).get('filter.graph') or {}) \
            .get('nodes') or []
        bands = [n for n in nodes if str(n.get('name', '')).startswith('b')]
        return tuple(_LABEL_TO_CODE.get(n.get('label'), '?') for n in bands)
    return None


def _types(curve: E.Curve) -> tuple:
    return tuple(b.type for b in curve.bands)


class EqSession:
    def __init__(self, enh, run_async=None, writer=None, on_status=None):
        self.enh = enh
        stored = enhance.curve_of(enh) if enh is not None else E.default_curve()
        self.saved = stored
        self.curve = stored.clamped()
        # A curve stored past what the editor can hold (made before the
        # caps) opens brought inside them; say so instead of hiding it.
        self.capped = self.curve != stored
        self.bypassed = False
        self.status = OFF
        self.on_status = on_status
        self._run_async = run_async or (lambda fn, done=None: (
            done(fn(), None) if done else fn()))
        self._writer = writer
        self._undo: list[E.Curve] = []
        self._redo: list[E.Curve] = []
        self._group = None
        self._node_id = None
        self._live_types = None      # what the running graph can take
        self._touched = False        # we changed what plays
        self._restart_busy = False
        self._restart_again = False
        self._closed = False

    # ------------------------------------------------------------- start --
    def start(self):
        """Find out whether the edit can be heard (runs a command: call it
        through run_async, or from a worker thread)."""
        enh = self.enh
        if enh is None or not enh.enabled:
            return self._set_status(OFF)
        from . import pw
        dump = pw.pw_dump()
        node = next((n for n in pw.list_audio_nodes(dump)
                     if n.name == enh.node_name), None)
        if node is None:
            return self._set_status(OFF)
        self._node_id = node.id
        running = enhance.running_conf(enh)
        # The graph takes control writes only if it is the conf this version
        # would build for the saved curve (else: the old file engine, or an
        # older generator), and only while it really exposes controls.
        if (enhance.same_shape(running, enhance.build_conf(enh))
                and enhance.is_live(enh, dump)):
            self._live_types = _running_types(running)
        else:
            self._live_types = None
        if self._writer is None:
            self._writer = ControlWriter()
        self._set_status(LIVE)

    @property
    def can_hear(self) -> bool:
        return self.status in (LIVE, RESTARTING)

    # ------------------------------------------------------------- edits --
    def set(self, curve: E.Curve, group=None):
        """Make `curve` the edit.  Consecutive calls with the same `group`
        (one drag, one slider) are one undo step."""
        curve = curve.clamped()
        if curve == self.curve:
            return
        if group is None or group != self._group:
            self._undo.append(self.curve)
            del self._undo[:-200]
        self._group = group
        self._redo.clear()
        self.curve = curve
        self._sync()

    def end_group(self):
        """The drag or slider that was coalescing has let go."""
        self._group = None
        if self._writer is not None and self._node_id is not None:
            self._writer.flush(0)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self):
        if not self._undo:
            return
        self._redo.append(self.curve)
        self.curve = self._undo.pop()
        self._group = None
        self._sync(urgent=True)

    def redo(self):
        if not self._redo:
            return
        self._undo.append(self.curve)
        self.curve = self._redo.pop()
        self._group = None
        self._sync(urgent=True)

    @property
    def dirty(self) -> bool:
        return self.curve != self.saved.clamped() or self.capped

    def set_bypass(self, on: bool):
        if on == self.bypassed:
            return
        self.bypassed = on
        self._sync(urgent=True)

    # -------------------------------------------------------------- sound --
    def _sync(self, urgent=False):
        if self._closed or self.status not in (LIVE, RESTARTING, FAILED):
            return
        if self.status == FAILED:
            return
        if self._live_types is not None and \
                _types(self.curve) == self._live_types and \
                not self._restart_busy:
            self._mark()
            self._writer.write(self._node_id,
                               enhance.eq_controls(self.curve,
                                                   bypass=self.bypassed),
                               urgent=urgent)
            return
        self._restart_soon()

    def _mark(self):
        if not self._touched:
            self._touched = True
            enhance.mark_live(self.enh, 'editing')

    def _restart_soon(self):
        if self._restart_busy:
            self._restart_again = True
            return
        self._restart_busy = True
        self._restart_again = False
        self._mark()
        self._set_status(RESTARTING)
        curve = self.curve.flat() if self.bypassed else self.curve
        target = self.curve

        def work():
            ok, err = enhance.preview(self.enh, curve)
            node = enhance.wait_for_node(self.enh.node_name) if ok else None
            return ok, err, node

        def done(result, error):
            ok, err, node = result if result else (False, str(error), None)
            self._restart_busy = False
            if self._closed:
                return
            if ok and node is not None:
                self._node_id = node.id
                self._live_types = _types(target)
                self._set_status(LIVE)
                if self._restart_again or self.curve != target or \
                        self.bypassed:
                    self._restart_again = False
                    self._sync(urgent=True)
            else:
                self._live_types = None
                self._set_status(FAILED)
        self._run_async(work, done)

    def _set_status(self, status):
        self.status = status
        if self.on_status:
            self.on_status(status)

    # ----------------------------------------------------------- the end --
    def params(self) -> dict:
        """The curve as an equalizer's params (merge into enh.params)."""
        return self.curve.to_params()

    def commit(self):
        """The edit is being saved: the caller writes the params and runs
        `enhance.apply()`, which finds the graph already playing them and
        writes instead of restarting.  Just stop previewing."""
        self._closed = True
        self.bypassed = False
        if self._writer is not None:
            self._writer.close()

    def cancel_work(self):
        """What Cancel must do to put the saved curve back, as a function to
        run off the UI thread (None when nothing was changed)."""
        self._closed = True
        writer, self._writer = self._writer, None
        if not self._touched or self.enh is None:
            if writer is not None:
                writer.close()
            return None
        enh = self.enh
        saved_types = _types(self.saved.clamped())
        same = self._live_types == saved_types and self.status == LIVE
        node_id = self._node_id

        def work():
            if same and writer is not None:
                writer.write(node_id, enhance.eq_controls(
                    self.saved.clamped()), urgent=True)
                writer.close()
                enhance.clear_live(enh)
                return True, ''
            if writer is not None:
                writer.close()
            return enhance.apply(enh)
        return work
