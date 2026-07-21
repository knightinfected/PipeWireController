"""Devices page: all sinks & sources with default selection, volume, mute."""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk  # noqa: E402

from ..backend import pw
from .widgets import async_call, group, page_scroller, pill


class DevicesPage:
    def __init__(self, window):
        self.window = window
        self.sinks = group('Outputs (sinks)',
                           'Star a device to make it the default output.')
        self.sources = group('Inputs (sources)')
        head = group('')
        refresh_row = Adw.ActionRow(
            title='Audio endpoints',
            subtitle='Includes virtual sinks created by filter chains')
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
        async_call(pw.list_audio_nodes, self._apply)

    def _apply(self, nodes, error):
        if error or nodes is None:
            return
        for row, parent in self._rows:
            parent.remove(row)
        self._rows = []
        for node in nodes:
            parent = self.sinks if node.is_sink else self.sources
            row = self._device_row(node)
            parent.add(row)
            self._rows.append((row, parent))

    def _device_row(self, node):
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
            pw.set_default(node.id)
            self.window.toast(f'Default set to {node.description}')
            self.refresh()
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
                pw.set_volume(node.id, s.get_value())
        scale.connect('value-changed', vol_changed)

        mute = Gtk.ToggleButton(icon_name='audio-volume-muted-symbolic',
                                tooltip_text='Mute')
        mute.add_css_class('flat')
        mute.set_valign(Gtk.Align.CENTER)
        mute.set_active(node.muted)
        mute.connect('toggled',
                     lambda b: updating['v'] or pw.set_mute(node.id, b.get_active()))

        row.add_suffix(scale)
        row.add_suffix(mute)
        row.add_suffix(star)
        return row
