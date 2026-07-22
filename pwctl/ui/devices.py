"""Devices page: endpoints with default selection, volume and, for hardware
devices, persistent per-device settings (rename, hide, rate, bit depth,
period size, headroom, preferred quantum, suspend timeout).

Per-device settings become WirePlumber node rules through backend.rules and
need a WirePlumber restart (the banner appears automatically).
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk  # noqa: E402

from ..backend import pw, rules
from .widgets import async_call, group, page_scroller, pill


class DevicesPage:
    def __init__(self, window):
        self.window = window
        self.sinks = group('Outputs (sinks)',
                           'Star a device to make it the default output. '
                           'Expand a hardware device for per-device settings.')
        self.sources = group('Inputs (sources)')
        head = group('')
        refresh_row = Adw.ActionRow(
            title='Audio endpoints',
            subtitle='Includes virtual sinks created by filter chains and '
                     'virtual devices')
        btn = Gtk.Button(icon_name='view-refresh-symbolic',
                         tooltip_text='Refresh')
        btn.add_css_class('flat')
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect('clicked', lambda *_: self.refresh())
        refresh_row.add_suffix(btn)
        head.add(refresh_row)
        self.widget = page_scroller(head, self.sinks, self.sources)
        self._rows = []
        self.widget.connect('map', lambda *_: self.refresh())

    def refresh(self):
        def collect():
            return pw.list_audio_nodes(), rules.load()
        async_call(collect, self._apply)

    def _apply(self, result, error):
        if error or result is None:
            return
        nodes, rule_data = result
        for row, parent in self._rows:
            parent.remove(row)
        self._rows = []
        for node in nodes:
            parent = self.sinks if node.is_sink else self.sources
            row = self._device_row(node, rule_data)
            parent.add(row)
            self._rows.append((row, parent))

    # ------------------------------------------------------------- row -----
    def _device_row(self, node, rule_data):
        is_hw = node.name.startswith(('alsa_', 'bluez_'))
        rule = rule_data['nodes'].get(node.name, {})

        if is_hw:
            row = Adw.ExpanderRow(title=node.description, subtitle=node.name)
            if rule:
                row.add_suffix(pill('customized', 'warning'))
        else:
            row = Adw.ActionRow(title=node.description, subtitle=node.name,
                                title_lines=1, subtitle_lines=1)
        icon = ('application-x-addon-symbolic' if node.is_virtual
                else 'audio-speakers-symbolic' if node.is_sink
                else 'audio-input-microphone-symbolic')
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        if node.is_virtual:
            row.add_suffix(pill('virtual', 'dim'))

        star = Gtk.Button(
            icon_name='starred-symbolic' if node.is_default
            else 'non-starred-symbolic',
            tooltip_text='Default device' if node.is_default
            else 'Make default')
        star.add_css_class('flat')
        if node.is_default:
            star.add_css_class('star-active')
        star.set_valign(Gtk.Align.CENTER)

        def make_default(_b):
            async_call(lambda: pw.set_default(node.id),
                       lambda ok, e: (self.window.toast(
                           f'Default set to {node.description}' if ok and not e
                           else 'Could not set default'), self.refresh()))
        star.connect('clicked', make_default)

        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1.5, 0.01)
        scale.set_size_request(150, -1)
        scale.set_valign(Gtk.Align.CENTER)
        scale.add_mark(1.0, Gtk.PositionType.BOTTOM, None)
        updating = {'v': False}
        if node.volume is not None:
            updating['v'] = True
            scale.set_value(node.volume)
            updating['v'] = False

        def vol_changed(s):
            if not updating['v']:
                value = s.get_value()
                async_call(lambda: pw.set_volume(node.id, value))
        scale.connect('value-changed', vol_changed)

        mute = Gtk.ToggleButton(icon_name='audio-volume-muted-symbolic',
                                tooltip_text='Mute')
        mute.add_css_class('flat')
        mute.set_valign(Gtk.Align.CENTER)
        mute.set_active(node.muted)

        def mute_toggled(b):
            if not updating['v']:
                active = b.get_active()
                async_call(lambda: pw.set_mute(node.id, active))
        mute.connect('toggled', mute_toggled)

        row.add_suffix(scale)
        row.add_suffix(mute)
        row.add_suffix(star)

        if is_hw:
            self._add_settings_rows(row, node, rule)
        return row

    # ------------------------------------------------- per-device settings --
    def _add_settings_rows(self, row, node, rule):
        props = rule.get('props', {})
        updating = {'v': True}

        rename = Adw.EntryRow(title='Rename (empty = original name)')
        rename.set_text(rule.get('rename', ''))
        rename.connect('apply', lambda r: self._save(
            node, rename=r.get_text().strip()))
        rename.set_show_apply_button(True)
        row.add_row(rename)

        hide = Adw.SwitchRow(
            title='Hide device',
            subtitle='Disables the node — it disappears from all apps.')
        hide.set_active(bool(rule.get('hide')))
        hide.connect('notify::active', lambda r, _p: (
            None if updating['v'] else self._save(node,
                                                  hide=r.get_active())))
        row.add_row(hide)

        for key, title, subtitle, kind, extra in rules.DEVICE_PROP_SCHEMA:
            if key in ('priority.session', 'priority.driver'):
                continue          # exposed on the Policy page instead
            current = props.get(key)
            if kind == 'enum':
                labels = [('Auto' if c in (0, 'auto') else str(c))
                          for c in extra]
                sub_row = Adw.ComboRow(title=title, subtitle=subtitle,
                                       model=Gtk.StringList.new(labels))
                try:
                    sub_row.set_selected(
                        extra.index(current) if current is not None else 0)
                except ValueError:
                    pass
                sub_row.connect(
                    'notify::selected',
                    lambda r, _p, k=key, ch=extra: (
                        None if updating['v'] else self._save(
                            node, props={k: ch[r.get_selected()]})))
            elif kind == 'latency':
                sub_row = Adw.EntryRow(title=f'{title} — {subtitle}')
                sub_row.set_text(str(current or ''))
                sub_row.set_show_apply_button(True)
                sub_row.connect('apply', lambda r, k=key: self._save(
                    node, props={k: r.get_text().strip()}))
            else:  # int
                lo, hi = extra
                sub_row = Adw.SpinRow.new_with_range(lo, hi, 1)
                sub_row.set_title(title)
                sub_row.set_subtitle(subtitle)
                try:
                    sub_row.set_value(float(
                        current if current is not None
                        else node.props.get(key, 0) or 0))
                except (TypeError, ValueError):
                    pass
                sub_row.connect(
                    'notify::value',
                    lambda r, _p, k=key: (
                        None if updating['v'] else self._save(
                            node, props={k: int(r.get_value())})))
            row.add_row(sub_row)
        updating['v'] = False

    def _save(self, node, rename=None, hide=None, props=None):
        def work():
            rules.set_node_rule(node.name, rename=rename, hide=hide,
                                props=props)
            return True
        async_call(work, lambda r, e: (
            self.window.toast(f'Failed: {e}') if e
            else self.window.flag_restart('wireplumber')))
