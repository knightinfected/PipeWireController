"""Dashboard: live status, clock, defaults, quick volume."""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend import chains, pw, system
from .widgets import async_call, group, page_scroller, pill, state_style

SERVICES = [('pipewire.service', 'PipeWire'),
            ('wireplumber.service', 'WirePlumber'),
            ('pipewire-pulse.service', 'PipeWire-Pulse')]


class Dashboard:
    def __init__(self, window):
        self.window = window
        self._timer = None
        self._busy = False

        # services
        svc = group('Services')
        self.svc_rows = {}
        for unit, label in SERVICES:
            row = Adw.ActionRow(title=label, subtitle=unit)
            p = pill('…', 'dim')
            row.add_suffix(p)
            restart = Gtk.Button(icon_name='view-refresh-symbolic',
                                 tooltip_text=f'Restart {label}')
            restart.add_css_class('flat')
            restart.set_valign(Gtk.Align.CENTER)
            restart.connect('clicked', self._restart_service, unit, label)
            row.add_suffix(restart)
            svc.add(row)
            self.svc_rows[unit] = (row, p)

        # clock
        clock = group('Graph clock')
        self.rate_row = Adw.ActionRow(title='Sample rate')
        self.quantum_row = Adw.ActionRow(title='Quantum (buffer)')
        self.latency_row = Adw.ActionRow(
            title='Theoretical latency',
            subtitle='quantum ÷ rate — one processing cycle')
        for r in (self.rate_row, self.quantum_row, self.latency_row):
            lbl = Gtk.Label()
            lbl.add_css_class('numeric-value')
            lbl.set_valign(Gtk.Align.CENTER)
            r.add_suffix(lbl)
            r.value_label = lbl
            clock.add(r)

        # defaults + volume
        vol = group('Default endpoints')
        self.sink_row = self._volume_row('Output', 'audio-speakers-symbolic')
        self.source_row = self._volume_row('Input', 'audio-input-microphone-symbolic')
        vol.add(self.sink_row['row'])
        vol.add(self.source_row['row'])

        # chains summary
        ch = group('Filter chains')
        self.chains_row = Adw.ActionRow(title='Active chains')
        go = Gtk.Button(label='Manage')
        go.set_valign(Gtk.Align.CENTER)
        go.connect('clicked', lambda *_: window.goto('chains'))
        self.chains_row.add_suffix(go)
        ch.add(self.chains_row)

        self.widget = page_scroller(svc, clock, vol, ch)
        self.widget.connect('map', self._on_map)
        self.widget.connect('unmap', self._on_unmap)

    # ------------------------------------------------------------- helpers --
    def _volume_row(self, title, icon):
        row = Adw.ActionRow(title=title, subtitle='—', title_lines=1, subtitle_lines=1)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1.5, 0.01)
        scale.set_size_request(170, -1)
        scale.set_valign(Gtk.Align.CENTER)
        scale.add_mark(1.0, Gtk.PositionType.BOTTOM, None)
        mute = Gtk.ToggleButton(icon_name='audio-volume-muted-symbolic',
                                tooltip_text='Mute')
        mute.add_css_class('flat')
        mute.set_valign(Gtk.Align.CENTER)
        row.add_suffix(scale)
        row.add_suffix(mute)
        info = {'row': row, 'scale': scale, 'mute': mute, 'id': None,
                'updating': False}

        def vol_changed(s):
            if not info['updating'] and info['id'] is not None:
                pw.set_volume(info['id'], s.get_value())

        def mute_toggled(b):
            if not info['updating'] and info['id'] is not None:
                pw.set_mute(info['id'], b.get_active())
        scale.connect('value-changed', vol_changed)
        mute.connect('toggled', mute_toggled)
        return info

    def _restart_service(self, _b, unit, label):
        self.window.toast(f'Restarting {label}…')
        async_call(lambda: system.restart_unit(unit),
                   lambda r, e: self.window.toast(f'{label} restarted'))

    # -------------------------------------------------------------- refresh --
    def _on_map(self, *_a):
        self.refresh()
        if not self._timer:
            self._timer = GLib.timeout_add_seconds(3, self._tick)

    def _on_unmap(self, *_a):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None

    def _tick(self):
        self.refresh()
        return True

    def refresh(self):
        if self._busy:
            return
        self._busy = True

        def collect():
            data = {}
            data['states'] = {u: system.unit_state(u) for u, _ in SERVICES}
            data['settings'] = pw.read_settings()
            dump = pw.pw_dump()
            data['driver'] = pw.driver_clock(dump)
            data['nodes'] = pw.list_audio_nodes(dump)
            metas = chains.list_chains()
            data['chains_total'] = len(metas)
            data['chains_active'] = sum(
                1 for m in metas if m.enabled and chains.status(m) == 'active')
            return data
        async_call(collect, self._apply)

    def _apply(self, data, error):
        self._busy = False
        if error or not data:
            return
        for unit, (_row, p) in self.svc_rows.items():
            state = data['states'].get(unit, 'unknown')
            p.set_label(state)
            for c in list(p.get_css_classes()):
                if c.startswith('pill-'):
                    p.remove_css_class(c)
            p.add_css_class(f'pill-{state_style(state)}')

        s = data['settings']
        drv = data['driver']
        rate = drv.get('rate') or int(s.get('clock.rate', '0') or 0)
        quantum = drv.get('quantum') or int(s.get('clock.quantum', '0') or 0)
        forced_r = int(s.get('clock.force-rate', '0') or 0)
        forced_q = int(s.get('clock.force-quantum', '0') or 0)
        self.rate_row.value_label.set_label(
            f'{rate} Hz' + (' (forced)' if forced_r else ''))
        self.rate_row.set_subtitle(
            'allowed: ' + s.get('clock.allowed-rates', '[]'))
        self.quantum_row.value_label.set_label(
            f'{quantum}' + (' (forced)' if forced_q else '') +
            f'  ·  min {s.get("clock.min-quantum", "?")} / '
            f'max {s.get("clock.max-quantum", "?")}')
        if rate and quantum:
            self.latency_row.value_label.set_label(
                f'{quantum / rate * 1000:.2f} ms')

        for info, want_sink in ((self.sink_row, True), (self.source_row, False)):
            node = next((n for n in data['nodes']
                         if n.is_sink == want_sink and n.is_default), None)
            info['updating'] = True
            try:
                if node:
                    info['id'] = node.id
                    info['row'].set_subtitle(node.description)
                    if node.volume is not None:
                        info['scale'].set_value(node.volume)
                    info['mute'].set_active(node.muted)
                else:
                    info['id'] = None
                    info['row'].set_subtitle('none')
            finally:
                info['updating'] = False

        self.chains_row.set_subtitle(
            f'{data["chains_active"]} running of {data["chains_total"]} configured')
