"""systemd unit state over D-Bus, with `systemctl --user` as the fallback.

Two things the app used to start a `systemctl` process for:

* **Reading a unit's state.** `active_state()` / `main_pid()` ask the user
  manager on the session bus with one Properties.Get: ~0.7 ms in-process
  against ~9 ms and a process for `systemctl --user is-active`.  Measured
  2026-10-16 on every unit the app asks about, plus names that do not exist
  and transient units in each state: the same answer every time.
* **Noticing a change.** `watch()` delivers systemd's own PropertiesChanged
  signals, so a page or the failure notifier hears about a restart or a
  failure within ~20-40 ms instead of on its next poll. A poll also never
  sees a unit that fails and is started again in between; signals do.
  Nothing arrives while nothing changes (0 signals at idle, measured).

Every function answers None when the bus cannot be asked (no session bus,
systemd not on it), and callers keep their `systemctl` path for that case:
"cannot tell" is never read as "inactive".  Nothing is cached; each call asks.

Imports Gio, not Gtk: the backend stays usable without a display.
Probe: docs.local/probes/2026-10-16-units-dbus/.
"""

from __future__ import annotations

from gi.repository import Gio, GLib

SD = 'org.freedesktop.systemd1'
SD_PATH = '/org/freedesktop/systemd1'
UNIT_IFACE = 'org.freedesktop.systemd1.Unit'
SERVICE_IFACE = 'org.freedesktop.systemd1.Service'
PROPS_IFACE = 'org.freedesktop.DBus.Properties'
TIMEOUT_MS = 2000
# Never ask the bus to start systemd for us: if it is not there, fall back.
_FLAGS = Gio.DBusCallFlags.NO_AUTO_START


def unit_path(name: str) -> str:
    """The unit's object path, escaped as systemd's bus_label_escape() does.

    systemd answers on this path even for a unit it has not loaded (it loads
    it, as `systemctl is-active` makes it do), so no LoadUnit call is needed.
    Checked against LoadUnit for 46 names on 2026-10-16.
    """
    out = []
    for i, ch in enumerate(name):
        if ch.isascii() and (ch.isalpha() or (i > 0 and ch.isdigit())):
            out.append(ch)
        else:
            out.append('_%02x' % ord(ch))
    return f'{SD_PATH}/unit/' + (''.join(out) or '_')


def _bus():
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        return None


def _get(unit: str, iface: str, prop: str):
    """One property of a unit, or None when the bus cannot answer."""
    bus = _bus()
    if bus is None:
        return None
    try:
        reply = bus.call_sync(SD, unit_path(unit), PROPS_IFACE, 'Get',
                              GLib.Variant('(ss)', (iface, prop)),
                              GLib.VariantType('(v)'), _FLAGS, TIMEOUT_MS,
                              None)
    except GLib.Error:
        return None
    return reply.unpack()[0]


def active_state(unit: str) -> str | None:
    """What `systemctl --user is-active UNIT` prints, or None (fall back)."""
    state = _get(unit, UNIT_IFACE, 'ActiveState')
    return state if isinstance(state, str) and state else None


def main_pid(unit: str) -> int | None:
    """The service's main PID (0 when not running), or None (fall back)."""
    pid = _get(unit, SERVICE_IFACE, 'MainPID')
    return pid if isinstance(pid, int) else None


# ------------------------------------------------------------------ watch --

class _Watch:
    def __init__(self, units, callback):
        self.units = list(units)
        self.callback = callback
        self.subs = []
        self.last = {}           # unit -> last state delivered
        self.live = True


_watches: list[_Watch] = []
_subscribed = [False]
_name_watch = [0, False]         # bus_watch_name id, and "seen it vanish"


def _deliver(w: _Watch, unit: str, state):
    if not w.live or not isinstance(state, str) or not state:
        return
    if w.last.get(unit) == state:
        return                   # systemd repeats a state several times
    w.last[unit] = state
    w.callback(unit, state)


def _read_async(bus, w: _Watch, unit: str, only_if_unseen: bool):
    def done(conn, result):
        try:
            state = conn.call_finish(result).unpack()[0]
        except GLib.Error:
            return
        # A signal that landed while this read was in flight is newer.
        if only_if_unseen and unit in w.last:
            return
        _deliver(w, unit, state)
    bus.call(SD, unit_path(unit), PROPS_IFACE, 'Get',
             GLib.Variant('(ss)', (UNIT_IFACE, 'ActiveState')),
             GLib.VariantType('(v)'), _FLAGS, TIMEOUT_MS, None, done)


def _subscribe(bus) -> bool:
    """Manager.Subscribe(): systemd sends unit signals only once asked.

    Done once per process; systemd forgets a client when it disconnects, so
    there is nothing to undo.  An "already subscribed" answer is success.
    """
    try:
        bus.call_sync(SD, SD_PATH, 'org.freedesktop.systemd1.Manager',
                      'Subscribe', None, None, _FLAGS, TIMEOUT_MS, None)
    except GLib.Error as e:
        if 'AlreadySubscribed' not in (e.message or '') and \
                'already subscribed' not in (e.message or '').lower():
            return False
    _subscribed[0] = True
    return True


def _on_name_appeared(bus, _name, _owner):
    # The first call is just the name being there when we start watching.
    # After a vanish (systemd re-executed or restarted), subscribe again and
    # re-read every watched unit, since signals were missed meanwhile.
    if not _name_watch[1]:
        return
    _name_watch[1] = False
    _subscribed[0] = False
    if _subscribe(bus):
        for w in list(_watches):
            for unit in w.units:
                _read_async(bus, w, unit, only_if_unseen=False)


def _on_name_vanished(_bus, _name):
    _name_watch[1] = True


def watch(units, callback):
    """Call callback(unit, state) on the main loop when a unit's state moves.

    The current state of each unit is delivered first (read asynchronously),
    then every change.  Returns a token for unwatch(), or None when systemd
    cannot be reached on the session bus: the caller keeps polling then.
    Call it from the main thread; the callbacks run on its main context.
    """
    bus = _bus()
    if bus is None:
        return None
    if not _subscribed[0] and not _subscribe(bus):
        return None
    if not _name_watch[0]:
        _name_watch[0] = Gio.bus_watch_name_on_connection(
            bus, SD, Gio.BusNameWatcherFlags.NONE,
            _on_name_appeared, _on_name_vanished)
    w = _Watch(units, callback)

    def make_handler(unit):
        def on_changed(_conn, _sender, _path, _iface, _member, params):
            _ifc, changed, invalidated = params.unpack()
            if 'ActiveState' in changed:
                _deliver(w, unit, changed['ActiveState'])
            elif 'ActiveState' in invalidated:
                _read_async(bus, w, unit, only_if_unseen=False)
        return on_changed

    for unit in w.units:
        w.subs.append(bus.signal_subscribe(
            SD, PROPS_IFACE, 'PropertiesChanged', unit_path(unit), UNIT_IFACE,
            Gio.DBusSignalFlags.NONE, make_handler(unit)))
        _read_async(bus, w, unit, only_if_unseen=True)
    _watches.append(w)
    return w


def unwatch(token):
    if token is None or not token.live:
        return
    token.live = False
    bus = _bus()
    if bus is not None:
        for sid in token.subs:
            bus.signal_unsubscribe(sid)
    token.subs = []
    if token in _watches:
        _watches.remove(token)
