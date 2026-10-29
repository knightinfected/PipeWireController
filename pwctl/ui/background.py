"""What the app does while it runs, with or without a window.

Two things need the app running (issue #28): Device Presets switching by
themselves when the default output changes, and the desktop notifications
for broken links and failed services.  They used to live on the window,
which builds all sixteen pages, so starting at login with no window
(`--background`) needs them here, on the application.  The window keeps
`notify_user()` and `mark_user_patch()` and passes them on, so pages call
the window as they always did.
"""

from __future__ import annotations

import time

import gi

gi.require_version('Gio', '2.0')
from gi.repository import GLib, Gio  # noqa: E402

from ..backend import graph, prefs, presets, pw, system, units
from .widgets import async_call

WATCHED_SERVICES = ('pipewire.service', 'wireplumber.service',
                    'pipewire-pulse.service')


class Watchers:
    def __init__(self, app):
        self.app = app
        self._last_default_sink = None
        self._last_patch_ts = 0.0
        self._watch_links = None       # set of (out_name, port, in_name, port)
        self._watch_states: dict[str, str] = {}
        self._watch_busy = False
        self._unit_watch = None
        self._timers = []
        # restarts a preset asked for while no window was open; the window
        # shows them as its banner when it opens
        self.pending_restarts: set[str] = set()

    def start(self):
        if self._timers:
            return
        # Service failures come from systemd's own signals when it is on the
        # session bus: at once, and including a failure that is restarted
        # before a poll would have looked.  The 5 s poll below covers them
        # only when it is not.  Links stay on the poll (DR-004 step 4).
        self._unit_watch = units.watch(WATCHED_SERVICES, self._service_state)
        self._timers = [GLib.timeout_add_seconds(5, self._autoload_tick),
                        GLib.timeout_add_seconds(5, self._watch_tick)]

    def stop(self):
        for t in self._timers:
            GLib.source_remove(t)
        self._timers = []
        if self._unit_watch is not None:
            units.unwatch(self._unit_watch)
            self._unit_watch = None

    # ------------------------------------------------------ notifications --
    def notify_user(self, title: str, body: str, buttons=()):
        """A desktop notification.  `buttons` are (label, 'app.action')."""
        note = Gio.Notification.new(title)
        note.set_body(body)
        note.set_priority(Gio.NotificationPriority.NORMAL)
        for label, action in buttons:
            note.add_button(label, action)
        self.app.send_notification(None, note)

    def mark_user_patch(self):
        """Patch changes we made ourselves shouldn't raise link warnings."""
        self._last_patch_ts = time.monotonic()

    def _service_state(self, unit, state):
        """A service's state, from the signal or from the poll."""
        prev = self._watch_states.get(unit)
        self._watch_states[unit] = state
        if (state == 'failed' and prev not in (None, 'failed')
                and prefs.get('notify_services')):
            self.notify_user('Audio service failed',
                             f'{unit} entered the failed state — '
                             'check the Monitor page.')

    def _watch_tick(self):
        want_links = bool(prefs.get('notify_links'))
        want_services = (bool(prefs.get('notify_services'))
                         and self._unit_watch is None)
        if (not want_links and not want_services) or self._watch_busy:
            return True
        self._watch_busy = True

        def collect():
            states = {}
            if want_services:
                for unit in WATCHED_SERVICES:
                    states[unit] = system.unit_state(unit)
            links, names = None, {}
            if want_links:
                g = graph.snapshot()
                ports = {p.id: p for n in g.nodes.values()
                         for p in n.inputs + n.outputs}
                links = set()
                for link in g.links:
                    op, ip = ports.get(link.out_port), ports.get(link.in_port)
                    if not op or not ip:
                        continue
                    key = (g.nodes[link.out_node].name, op.name,
                           g.nodes[link.in_node].name, ip.name)
                    links.add(key)
                names = {n.name: n.label for n in g.nodes.values()}
            return states, links, names

        def done(result, error):
            self._watch_busy = False
            if error or result is None:
                return
            states, links, names = result
            for unit, state in states.items():
                self._service_state(unit, state)
            if links is not None:
                prev_links = self._watch_links
                self._watch_links = links
                recent_patch = (time.monotonic() - self._last_patch_ts) < 15
                if prev_links is not None and not recent_patch:
                    gone = prev_links - links
                    # only links whose endpoints still exist: a closed app
                    # takes its node away and is not a broken link
                    broken = [(o, i) for o, _op, i, _ip in gone
                              if o in names and i in names]
                    if broken:
                        o, i = broken[0]
                        extra = (f' (and {len(broken) - 1} more)'
                                 if len(broken) > 1 else '')
                        self.notify_user(
                            'Audio link disconnected',
                            f'{names.get(o, o)} → {names.get(i, i)} was '
                            f'disconnected{extra}.')
        async_call(collect, done)
        return True

    # ------------------------------------------------------ device presets --
    def _autoload_tick(self):
        if not prefs.get('autoload_presets'):
            return True

        def check():
            return pw.read_default_names().get('default.audio.sink')

        def done(name, e):
            if e or not name:
                return
            prev, self._last_default_sink = self._last_default_sink, name
            if prev is None or name == prev:
                return
            preset = presets.preset_for(name)
            if preset:
                self.apply_preset(preset)
        async_call(check, done)
        return True

    def apply_preset(self, preset):
        """Apply a preset; report it in the window, or as a notification."""
        def done(actions, e):
            win = self.app.get_window()
            if e or actions is None:
                if win is not None:
                    win.toast('Preset failed')
                else:
                    self.notify_user('Device preset failed',
                                     f'Could not apply the preset for '
                                     f'{preset["description"]}.')
                return
            restart = any('channel-mix' in a for a in actions)
            text = (f'Applied preset for {preset["description"]}: '
                    + (', '.join(actions) if actions else 'nothing to do'))
            if win is not None:
                if restart:
                    win.flag_restart('pulse')
                win.toast(text)
                return
            if restart:
                self.pending_restarts.add('pulse')
            self.notify_user(
                'Device preset applied',
                text + ('. Open PipeWire Controller to restart '
                        'PipeWire-Pulse and finish.' if restart else '.'))
        async_call(lambda: presets.apply(preset), done)
