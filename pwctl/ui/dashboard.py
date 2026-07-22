"""Dashboard: pavucontrol-style tabs (streams + devices) and system overview."""

from __future__ import annotations

import time

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from ..backend import chains, prefs, pw, system
from .volume import VOLUME_STYLES, make_volume
from .widgets import async_call, group, page_scroller, pill, state_style

SERVICES = [('pipewire.service', 'PipeWire'),
            ('wireplumber.service', 'WirePlumber'),
            ('pipewire-pulse.service', 'PipeWire-Pulse')]

# After the user touches a slider/toggle, ignore polled values for this long
# so the 3 s refresh doesn't yank the control back mid-drag.
LOCAL_GRACE = 2.0


def _app_icon(name):
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    if name and theme.has_icon(name):
        return name
    return 'audio-x-generic-symbolic'


def _pct_label():
    lbl = Gtk.Label(width_chars=5, xalign=1)
    lbl.add_css_class('numeric-value')
    lbl.add_css_class('dim-label')
    return lbl


def _mute_button():
    btn = Gtk.ToggleButton(icon_name='audio-volume-muted-symbolic',
                           tooltip_text='Mute')
    btn.add_css_class('flat')
    btn.set_valign(Gtk.Align.CENTER)
    return btn


def _solo_button():
    btn = Gtk.ToggleButton(label='S',
                           tooltip_text='Solo — mute everything else in '
                                        'this list (toggling off unmutes '
                                        'them again)')
    btn.add_css_class('flat')
    btn.add_css_class('solo-btn')
    btn.set_valign(Gtk.Align.CENTER)
    return btn


class _VolumeRowBase(Gtk.ListBoxRow):
    """Two-line row: header line + full-width volume control, pavucontrol style."""

    def __init__(self, style):
        super().__init__(activatable=False)
        self.updating = False
        self._local_ts = 0.0

        self.header = Gtk.Box(spacing=10)
        self.vol = make_volume(style, self._on_volume)
        self.pct = _pct_label()
        self.mute = _mute_button()
        self.solo = _solo_button()
        vol_line = Gtk.Box(spacing=10)
        vol_line.append(self.mute)
        vol_line.append(self.solo)
        vol_line.append(self.vol.widget)
        vol_line.append(self.pct)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=14, margin_bottom=14,
                      margin_start=16, margin_end=16)
        box.append(self.header)
        box.append(vol_line)
        self.set_child(box)

        self.mute.connect('toggled', self._on_mute)
        self.solo.connect('toggled', self._on_solo)

    # -- subclass provides the node id to control ------------------------
    node_id = None
    tab = None

    def _on_solo(self, btn):
        if self.updating or self.tab is None:
            return
        self.touch()
        self.tab.toggle_solo(self)

    def touch(self):
        self._local_ts = time.monotonic()

    @property
    def in_grace(self):
        return time.monotonic() - self._local_ts < LOCAL_GRACE

    def _on_volume(self, value):
        """User moved the volume control (never fires on programmatic set)."""
        self.pct.set_label(f'{value * 100:.0f}%')
        if self.node_id is None:
            return
        self.touch()
        async_call(lambda: pw.set_volume(self.node_id, value))

    def _on_mute(self, btn):
        if self.updating or self.node_id is None:
            return
        self.touch()
        active = btn.get_active()
        async_call(lambda: pw.set_mute(self.node_id, active))

    def set_levels(self, volume, muted):
        """Apply polled volume/mute unless the user just touched the row."""
        if self.in_grace:
            return
        if volume is not None:
            self.vol.set_value(volume)
            self.pct.set_label(f'{volume * 100:.0f}%')
        self.mute.set_active(muted)


class _StreamRow(_VolumeRowBase):
    """One application stream: icon, name, device selector, volume."""

    def __init__(self, tab, stream):
        super().__init__(tab.dash.volume_style)
        self.tab = tab
        self.node_id = stream.id
        self._dev_key = None
        self._devices = []

        self.icon = Gtk.Image.new_from_icon_name(_app_icon(stream.icon))
        self.title = Gtk.Label(xalign=0, hexpand=True,
                               ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class('heading')
        self.subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.subtitle.add_css_class('caption')
        self.subtitle.add_css_class('dim-label')
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        titles.append(self.title)
        titles.append(self.subtitle)

        self.dropdown = Gtk.DropDown(tooltip_text='Play on / record from')
        self.dropdown.set_valign(Gtk.Align.CENTER)
        self.dropdown.connect('notify::selected', self._on_device)

        self.header.append(self.icon)
        self.header.append(titles)
        self.header.append(self.dropdown)

    def _on_device(self, dd, _pspec):
        if self.updating:
            return
        idx = dd.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._devices):
            return
        serial, label, dev_node_id = self._devices[idx]
        self.touch()
        sid, window = self.node_id, self.tab.dash.window
        async_call(lambda: pw.move_stream(sid, serial),
                   lambda ok, e: (
                       window.toast(f'Moved to {label}' if ok and not e
                                    else 'Move failed'),
                       self.tab.dash.refresh_soon()))

    def update(self, stream, devices):
        """devices: list of (serial, label, device-node-id)."""
        self.updating = True
        try:
            self.title.set_label(stream.name)
            sub = stream.media if stream.media != stream.name else ''
            self.subtitle.set_label(sub)
            self.subtitle.set_visible(bool(sub))

            self._devices = devices
            key = tuple(d[:2] for d in devices)
            if key != self._dev_key:
                self._dev_key = key
                self.dropdown.set_model(
                    Gtk.StringList.new([d[1] for d in devices]))
            if not self.in_grace:
                idx = next((i for i, d in enumerate(devices)
                            if d[2] == stream.target_id), None)
                self.dropdown.set_selected(
                    idx if idx is not None else Gtk.INVALID_LIST_POSITION)
            self.set_levels(stream.volume, stream.muted)
        finally:
            self.updating = False


class _DeviceRow(_VolumeRowBase):
    """One sink/source: default star, port selector, volume."""

    def __init__(self, tab, node):
        super().__init__(tab.dash.volume_style)
        self.tab = tab
        self.node_id = node.id
        self._port_key = None
        self._ports = []

        icon = ('application-x-addon-symbolic' if node.is_virtual
                else 'audio-speakers-symbolic' if node.is_sink
                else 'audio-input-microphone-symbolic')
        self.icon = Gtk.Image.new_from_icon_name(icon)
        self.title = Gtk.Label(xalign=0, hexpand=True,
                               ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class('heading')

        self.port_dd = Gtk.DropDown(tooltip_text='Port')
        self.port_dd.set_valign(Gtk.Align.CENTER)
        self.port_dd.connect('notify::selected', self._on_port)

        self.star = Gtk.Button()
        self.star.add_css_class('flat')
        self.star.set_valign(Gtk.Align.CENTER)
        self.star.connect('clicked', self._on_default)

        self.header.append(self.icon)
        self.header.append(self.title)
        if node.is_virtual:
            self.header.append(pill('virtual', 'dim'))
        self.header.append(self.port_dd)
        self.header.append(self.star)

    def _on_port(self, dd, _pspec):
        if self.updating:
            return
        idx = dd.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._ports):
            return
        route_index, label = self._ports[idx]
        self.touch()
        nid, window = self.node_id, self.tab.dash.window
        async_call(lambda: pw.set_route(nid, route_index),
                   lambda ok, e: window.toast(
                       f'Port: {label}' if ok and not e else 'Port change failed'))

    def _on_default(self, _btn):
        nid, window = self.node_id, self.tab.dash.window
        async_call(lambda: pw.set_default(nid),
                   lambda ok, e: (window.toast('Default device changed'),
                                  self.tab.dash.refresh_soon()))

    def update(self, node):
        self.updating = True
        try:
            self.title.set_label(node.description)
            self.set_tooltip_text(node.name)

            ports = [(idx, desc + (' (unplugged)' if avail == 'no' else ''))
                     for idx, desc, avail in node.ports]
            self._ports = ports
            self.port_dd.set_visible(bool(ports))
            key = tuple(ports)
            if key != self._port_key:
                self._port_key = key
                self.port_dd.set_model(
                    Gtk.StringList.new([p[1] for p in ports]))
            if ports and not self.in_grace:
                idx = next((i for i, p in enumerate(ports)
                            if p[0] == node.active_port), None)
                self.port_dd.set_selected(
                    idx if idx is not None else Gtk.INVALID_LIST_POSITION)

            self.star.set_icon_name('starred-symbolic' if node.is_default
                                    else 'non-starred-symbolic')
            self.star.set_tooltip_text('Default device' if node.is_default
                                       else 'Make default')
            if node.is_default:
                self.star.add_css_class('star-active')
            else:
                self.star.remove_css_class('star-active')
            self.set_levels(node.volume, node.muted)
        finally:
            self.updating = False


class _ListTab:
    """A scrollable boxed list with an empty-state label."""

    def __init__(self, dash, empty_text):
        self.dash = dash
        self.rows = {}          # key -> row
        self.soloed = set()     # node ids currently soloed in this list
        self.listbox = Gtk.ListBox(css_classes=['boxed-list', 'vol-list'],
                                   selection_mode=Gtk.SelectionMode.NONE)
        self.empty = Gtk.Label(label=empty_text, margin_top=48)
        self.empty.add_css_class('dim-label')
        self.widget = page_scroller(self.listbox, self.empty, width=860)

    def clear(self):
        """Drop all rows so the next update rebuilds them (style change)."""
        for row in self.rows.values():
            self.listbox.remove(row)
        self.rows = {}
        self.soloed = set()

    def toggle_solo(self, row):
        """Solo: everything else in this list is muted while any solo is on."""
        if row.solo.get_active():
            self.soloed.add(row.node_id)
        else:
            self.soloed.discard(row.node_id)
        live = {r.node_id for r in self.rows.values()}
        self.soloed &= live
        soloing = bool(self.soloed)
        for r in self.rows.values():
            want_mute = soloing and r.node_id not in self.soloed
            r.touch()
            r.updating = True
            try:
                r.mute.set_active(want_mute)
            finally:
                r.updating = False
            nid = r.node_id
            async_call(lambda n=nid, m=want_mute: pw.set_mute(n, m))

    def _sync_rows(self, items, make_row):
        """items: list of (key, obj). Rebuild only when membership changes."""
        keys = [k for k, _ in items]
        if keys != list(self.rows):
            for row in self.rows.values():
                self.listbox.remove(row)
            self.rows = {}
            self.soloed = set()
            for key, obj in items:
                row = make_row(obj)
                self.listbox.append(row)
                self.rows[key] = row
        self.listbox.set_visible(bool(items))
        self.empty.set_visible(not items)
        return [(self.rows[k], obj) for k, obj in items]


class StreamsTab(_ListTab):
    def __init__(self, dash, playback: bool):
        super().__init__(dash, 'No applications are currently playing audio.'
                         if playback else
                         'No applications are currently recording audio.')
        self.playback = playback

    def update(self, streams, nodes):
        streams = [s for s in streams if s.is_playback == self.playback]
        if self.playback:
            devices = [(n.serial, n.description, n.id)
                       for n in nodes if n.is_sink]
        else:
            devices = ([(n.serial, n.description, n.id)
                        for n in nodes if not n.is_sink] +
                       [(n.serial, f'Monitor of {n.description}', n.id)
                        for n in nodes if n.is_sink])
        pairs = self._sync_rows([(s.id, s) for s in streams],
                                lambda s: _StreamRow(self, s))
        for row, stream in pairs:
            row.update(stream, devices)


class DevicesTab(_ListTab):
    def __init__(self, dash, sinks: bool):
        super().__init__(dash, 'No output devices found.' if sinks
                         else 'No input devices found.')
        self.sinks = sinks

    def update(self, nodes):
        nodes = sorted((n for n in nodes if n.is_sink == self.sinks),
                       key=lambda n: (n.is_virtual, n.description.lower()))
        pairs = self._sync_rows([(n.id, n) for n in nodes],
                                lambda n: _DeviceRow(self, n))
        for row, node in pairs:
            row.update(node)


class Dashboard:
    def __init__(self, window):
        self.window = window
        self._timer = None
        self._busy = False
        self._soon = None
        self._calc_init = False
        self.volume_style = prefs.get('volume_style')

        overview = self._build_overview()
        self.playback = StreamsTab(self, playback=True)
        self.recording = StreamsTab(self, playback=False)
        self.outputs = DevicesTab(self, sinks=True)
        self.inputs = DevicesTab(self, sinks=False)

        self.tabs = Adw.ViewStack()
        for name, title, icon, widget in [
                ('overview', 'Overview',
                 'utilities-system-monitor-symbolic', overview),
                ('playback', 'Playback',
                 'media-playback-start-symbolic', self.playback.widget),
                ('recording', 'Recording',
                 'media-record-symbolic', self.recording.widget),
                ('outputs', 'Output Devices',
                 'audio-speakers-symbolic', self.outputs.widget),
                ('inputs', 'Input Devices',
                 'audio-input-microphone-symbolic', self.inputs.widget)]:
            self.tabs.add_titled_with_icon(widget, name, title, icon)
        self.tabs.set_vexpand(True)

        switcher = Adw.ViewSwitcher(stack=self.tabs,
                                    policy=Adw.ViewSwitcherPolicy.WIDE)
        bar = Gtk.CenterBox(margin_top=10, margin_bottom=10)
        bar.set_center_widget(switcher)

        overlay = Gtk.Overlay(vexpand=True)
        overlay.set_child(self.tabs)
        overlay.add_overlay(self._build_style_picker())

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(bar)
        root.append(Gtk.Separator())
        root.append(overlay)
        import os
        debug_tab = os.environ.get('PWCTL_TAB')   # screenshot testing
        want = debug_tab or prefs.get('dashboard_tab')
        if want and self.tabs.get_child_by_name(want):
            self.tabs.set_visible_child_name(want)
        if not debug_tab:
            self.tabs.connect(
                'notify::visible-child-name',
                lambda *_: prefs.save(
                    dashboard_tab=self.tabs.get_visible_child_name()))
        self.widget = root
        self.widget.connect('map', self._on_map)
        self.widget.connect('unmap', self._on_unmap)

    # -------------------------------------------------------- style picker --
    def _build_style_picker(self):
        active = next((s for s in VOLUME_STYLES if s[0] == self.volume_style),
                      VOLUME_STYLES[0])
        self._style_content = Adw.ButtonContent(
            icon_name='preferences-system-symbolic', label=active[1])

        btn = Gtk.MenuButton(halign=Gtk.Align.END, valign=Gtk.Align.END,
                             margin_end=24, margin_bottom=24,
                             tooltip_text='Volume slider style')
        btn.add_css_class('style-picker')
        btn.set_child(self._style_content)

        header = Gtk.Label(label='Volume slider style', margin_bottom=4)
        header.add_css_class('heading')
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                              css_classes=['style-picker-list'])
        listbox.set_activate_on_single_click(True)
        self._style_checks = {}
        for key, title, subtitle, icon in VOLUME_STYLES:
            row = Gtk.ListBoxRow()
            row.style_key = key
            h = Gtk.Box(spacing=12)
            h.append(Gtk.Image.new_from_icon_name(icon))
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            t = Gtk.Label(label=title, xalign=0)
            t.add_css_class('heading')
            s = Gtk.Label(label=subtitle, xalign=0, wrap=True,
                          max_width_chars=34)
            s.add_css_class('caption')
            s.add_css_class('dim-label')
            labels.append(t)
            labels.append(s)
            h.append(labels)
            check = Gtk.Image.new_from_icon_name('object-select-symbolic')
            check.set_opacity(1.0 if key == self.volume_style else 0.0)
            self._style_checks[key] = check
            h.append(check)
            row.set_child(h)
            listbox.append(row)

        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                          margin_top=10, margin_bottom=10,
                          margin_start=10, margin_end=10)
        pop_box.append(header)
        pop_box.append(listbox)
        popover = Gtk.Popover()
        popover.set_child(pop_box)
        btn.set_popover(popover)
        listbox.connect('row-activated',
                        lambda _lb, row: (popover.popdown(),
                                          self._set_style(row.style_key)))
        return btn

    def _set_style(self, key):
        if key == self.volume_style:
            return
        self.volume_style = key
        prefs.save(volume_style=key)
        for k, check in self._style_checks.items():
            check.set_opacity(1.0 if k == key else 0.0)
        self._style_content.set_label(
            next(s[1] for s in VOLUME_STYLES if s[0] == key))
        for tab in (self.playback, self.recording, self.outputs, self.inputs):
            tab.clear()
        for info in (self.sink_row, self.source_row):
            self._rebuild_endpoint_vol(info)
        self.refresh()

    # ------------------------------------------------------------ overview --
    def _build_overview(self):
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
        clock.add(self._build_latency_calc())

        vol = group('Default endpoints')
        self.sink_row = self._volume_row('Output', 'audio-speakers-symbolic')
        self.source_row = self._volume_row('Input',
                                           'audio-input-microphone-symbolic')
        vol.add(self.sink_row['row'])
        vol.add(self.source_row['row'])

        act = group('Activity')
        self.activity_row = Adw.ActionRow(title='Application streams',
                                          subtitle='—')
        for label, tab in (('Playback', 'playback'),
                           ('Recording', 'recording')):
            b = Gtk.Button(label=label)
            b.add_css_class('flat')
            b.set_valign(Gtk.Align.CENTER)
            b.connect('clicked',
                      lambda _b, t=tab: self.tabs.set_visible_child_name(t))
            self.activity_row.add_suffix(b)
        act.add(self.activity_row)

        ch = group('Filter chains')
        self.chains_row = Adw.ActionRow(title='Active chains')
        go = Gtk.Button(label='Manage')
        go.set_valign(Gtk.Align.CENTER)
        go.connect('clicked', lambda *_: self.window.goto('chains'))
        self.chains_row.add_suffix(go)
        ch.add(self.chains_row)

        return page_scroller(svc, clock, vol, act, ch)

    # --------------------------------------------------- latency calculator --
    CALC_RATES = [44100, 48000, 88200, 96000, 176400, 192000]

    def _build_latency_calc(self):
        calc = Adw.ExpanderRow(
            title='Latency calculator',
            subtitle='Estimate latency for any quantum and sample rate')

        self.calc_quantum = Adw.SpinRow.new_with_range(16, 8192, 16)
        self.calc_quantum.set_title('Quantum (frames)')
        self.calc_quantum.set_value(256)
        self.calc_quantum.connect('notify::value',
                                  lambda *_: self._calc_update())

        self.calc_rate = Adw.ComboRow(
            title='Sample rate',
            model=Gtk.StringList.new([f'{r} Hz' for r in self.CALC_RATES]))
        self.calc_rate.set_selected(1)   # 48000
        self.calc_rate.connect('notify::selected',
                               lambda *_: self._calc_update())

        result = Adw.ActionRow(
            title='One processing cycle',
            subtitle='Round trip ≈ 2 cycles (in + out)')
        self.calc_result = Gtk.Label()
        self.calc_result.add_css_class('numeric-value')
        self.calc_result.set_valign(Gtk.Align.CENTER)
        result.add_suffix(self.calc_result)
        apply_btn = Gtk.Button(
            label='Test live',
            tooltip_text='Force this quantum and rate now (runtime only — '
                         'resets on restart; manage on the Server page)')
        apply_btn.add_css_class('flat')
        apply_btn.set_valign(Gtk.Align.CENTER)
        apply_btn.connect('clicked', self._calc_apply)
        result.add_suffix(apply_btn)

        for r in (self.calc_quantum, self.calc_rate, result):
            calc.add_row(r)
        self._calc_update()
        return calc

    def _calc_values(self):
        q = int(self.calc_quantum.get_value())
        rate = self.CALC_RATES[self.calc_rate.get_selected()]
        return q, rate

    def _calc_update(self):
        q, rate = self._calc_values()
        ms = q / rate * 1000
        self.calc_result.set_label(f'{ms:.2f} ms  ·  {2 * ms:.2f} ms RT')

    def _calc_apply(self, _b):
        q, rate = self._calc_values()

        def work():
            pw.set_setting('clock.force-quantum', q)
            pw.set_setting('clock.force-rate', rate)
            return True
        async_call(work, lambda r, e: (
            self.window.toast(f'Forced {q} frames @ {rate} Hz (runtime only)'
                              if not e else f'Failed: {e}'),
            self.refresh_soon()))

    def _volume_row(self, title, icon):
        row = Adw.ActionRow(title=title, subtitle='—',
                            title_lines=1, subtitle_lines=1)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        mute = _mute_button()
        info = {'row': row, 'vol': None, 'mute': mute, 'id': None,
                'updating': False}

        def vol_changed(value):
            if info['id'] is not None:
                async_call(lambda: pw.set_volume(info['id'], value))

        def mute_toggled(b):
            if not info['updating'] and info['id'] is not None:
                active = b.get_active()
                async_call(lambda: pw.set_mute(info['id'], active))
        info['on_change'] = vol_changed
        mute.connect('toggled', mute_toggled)
        self._rebuild_endpoint_vol(info)
        return info

    def _rebuild_endpoint_vol(self, info):
        """(Re)create the compact volume control on an overview endpoint row."""
        row, mute = info['row'], info['mute']
        if info['vol'] is not None:
            row.remove(info['vol'].widget)
            row.remove(mute)   # re-added after the new control to keep order
        info['vol'] = make_volume(self.volume_style, info['on_change'],
                                  compact=True)
        row.add_suffix(info['vol'].widget)
        row.add_suffix(mute)

    def _restart_service(self, _b, unit, label):
        self.window.toast(f'Restarting {label}…')

        def done(result, error):
            rc = result[0] if result else 1
            if error or rc != 0:
                detail = (result[2].strip() if result else str(error or ''))
                self.window.toast(f'{label} restart failed'
                                  + (f': {detail}' if detail else ''))
            else:
                self.window.toast(f'{label} restarted')
            self.refresh_soon()
        async_call(lambda: system.restart_unit(unit), done)

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

    def refresh_soon(self):
        """One quick refresh shortly after an action (move/default/port)."""
        if self._soon:
            return

        def fire():
            self._soon = None
            self.refresh()
            return False
        self._soon = GLib.timeout_add(500, fire)

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
            data['streams'] = pw.list_streams(dump)
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
        if not self._calc_init and rate and quantum:
            self._calc_init = True
            self.calc_quantum.set_value(quantum)
            if rate in self.CALC_RATES:
                self.calc_rate.set_selected(self.CALC_RATES.index(rate))

        for info, want_sink in ((self.sink_row, True),
                                (self.source_row, False)):
            node = next((n for n in data['nodes']
                         if n.is_sink == want_sink and n.is_default), None)
            info['updating'] = True
            try:
                if node:
                    info['id'] = node.id
                    info['row'].set_subtitle(node.description)
                    if node.volume is not None:
                        info['vol'].set_value(node.volume)
                    info['mute'].set_active(node.muted)
                else:
                    info['id'] = None
                    info['row'].set_subtitle('none')
            finally:
                info['updating'] = False

        playing = sum(1 for s in data['streams'] if s.is_playback)
        recording = len(data['streams']) - playing
        self.activity_row.set_subtitle(
            f'{playing} playing  ·  {recording} recording')

        self.chains_row.set_subtitle(
            f'{data["chains_active"]} running of {data["chains_total"]} configured')

        self.playback.update(data['streams'], data['nodes'])
        self.recording.update(data['streams'], data['nodes'])
        self.outputs.update(data['nodes'])
        self.inputs.update(data['nodes'])
