"""Virtual Devices page: null sinks/sources, aggregates and buses."""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend import pw, virtual
from ..backend.surround import LAYOUTS
from .widgets import async_call, confirm, group, icon_button, page_scroller, \
    pill, state_style

KIND_ICONS = {
    'null-sink': 'audio-speakers-symbolic',
    'null-source': 'audio-input-microphone-symbolic',
    'combine-sink': 'view-grid-symbolic',
    'combine-source': 'view-grid-symbolic',
    'bus': 'network-wired-symbolic',
}


class VirtualPage:
    def __init__(self, window):
        self.window = window

        head = group('Virtual devices',
                     'Every virtual device runs as its own tiny PipeWire '
                     'process, so creating or removing one never interrupts '
                     'playback. Temporary devices vanish on reboot; '
                     'persistent ones come back automatically.')
        new_row = Adw.ActionRow(
            title='Create a virtual device',
            subtitle='Null sink · virtual microphone · combined (aggregate) '
                     'device · bus / sub-mix')
        new_btn = Gtk.Button(icon_name='list-add-symbolic',
                             valign=Gtk.Align.CENTER)
        new_btn.add_css_class('suggested-action')
        new_btn.connect('clicked', lambda *_: self._open_dialog(None))
        new_row.add_suffix(new_btn)
        new_row.set_activatable_widget(new_btn)
        head.add(new_row)

        self.listing = group('Configured devices')
        self.widget = page_scroller(head, self.listing)
        self._rows = []
        self.widget.connect('map', lambda *_: self.refresh())

    def refresh(self):
        def collect():
            devs = virtual.list_devices()
            return [(d, virtual.status(d)) for d in devs]
        async_call(collect, self._apply)

    def _apply(self, items, error):
        if error or items is None:
            return
        for row in self._rows:
            self.listing.remove(row)
        self._rows = []
        if not items:
            row = Adw.ActionRow(
                title='No virtual devices yet',
                subtitle='Create one above — e.g. a combined output that '
                         'plays on two sound cards at once.')
            self.listing.add(row)
            self._rows.append(row)
            return
        for dev, state in items:
            row = self._device_row(dev, state)
            self.listing.add(row)
            self._rows.append(row)

    def _device_row(self, dev: virtual.VirtualDevice, state: str):
        row = Adw.ActionRow(title=dev.name,
                            subtitle=virtual.KINDS.get(dev.kind, dev.kind)
                            + ('' if dev.persistent else ' · temporary'),
                            title_lines=1, subtitle_lines=1)
        row.add_prefix(Gtk.Image.new_from_icon_name(
            KIND_ICONS.get(dev.kind, 'application-x-addon-symbolic')))
        if dev.enabled:
            row.add_suffix(pill(state, state_style(state)))

        sw = Gtk.Switch(valign=Gtk.Align.CENTER, active=dev.enabled,
                        tooltip_text='Started (visible to apps)')
        sw.connect('notify::active', self._toggled, dev)
        row.add_suffix(icon_button('document-edit-symbolic', 'Edit',
                                   lambda *_: self._open_dialog(dev)))
        row.add_suffix(icon_button(
            'user-trash-symbolic', 'Delete',
            lambda *_: confirm(
                self.window, f'Delete “{dev.name}”?',
                'The device is stopped and its configuration removed.',
                'Delete', lambda: self._delete(dev))))
        row.add_suffix(sw)
        return row

    def _toggled(self, sw, _p, dev):
        enabled = sw.get_active()
        if enabled == dev.enabled:
            return

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            if not ok:
                self.window.toast(f'Failed: {err}' if err else 'Failed')
            else:
                self.window.toast(
                    f'{dev.name} {"started" if enabled else "stopped"}')
            GLib.timeout_add(400, lambda: (self.refresh(), False)[1])
        async_call(lambda: virtual.set_enabled(dev, enabled), done)

    def _delete(self, dev):
        async_call(lambda: virtual.delete(dev),
                   lambda r, e: (self.window.toast('Deleted'), self.refresh()))

    def _open_dialog(self, dev):
        VirtualDialog(self.window, self, dev).present(self.window)


class VirtualDialog(Adw.Dialog):
    KIND_KEYS = list(virtual.KINDS)

    def __init__(self, window, page, dev: virtual.VirtualDevice | None):
        super().__init__(title='Edit virtual device' if dev
                         else 'New virtual device',
                         content_width=520, content_height=620)
        self.window = window
        self.page = page
        self.dev = dev
        self._nodes = []

        g = Adw.PreferencesGroup()
        self.name_row = Adw.EntryRow(title='Name')
        self.name_row.set_text(dev.name if dev else '')
        g.add(self.name_row)

        self.kind_row = Adw.ComboRow(
            title='Type',
            model=Gtk.StringList.new([virtual.KINDS[k]
                                      for k in self.KIND_KEYS]))
        if dev:
            self.kind_row.set_selected(self.KIND_KEYS.index(dev.kind))
            self.kind_row.set_sensitive(False)
        self.kind_row.connect('notify::selected', self._kind_changed)
        g.add(self.kind_row)

        self.layout_row = Adw.ComboRow(
            title='Channels',
            model=Gtk.StringList.new([l[1] for l in LAYOUTS]))
        if dev:
            idx = next((i for i, l in enumerate(LAYOUTS)
                        if l[2] == dev.positions), 0)
            self.layout_row.set_selected(idx)
        g.add(self.layout_row)

        self.persist_row = Adw.SwitchRow(
            title='Persistent',
            subtitle='Recreate this device automatically after reboot.')
        self.persist_row.set_active(dev.persistent if dev else True)
        g.add(self.persist_row)

        # combine members
        self.members_group = Adw.PreferencesGroup(
            title='Member devices',
            description='The combined device plays on / records from all '
                        'checked devices simultaneously.')
        self.member_rows = {}

        # bus target
        self.target_group = Adw.PreferencesGroup(
            title='Bus output',
            description='Where the bus feeds its mix. “Follow default” '
                        'keeps it routable from the Playback tab like any '
                        'app stream.')
        self.target_row = Adw.ComboRow(title='Target device')
        self.target_group.add(self.target_row)

        create = Gtk.Button(label='Save' if dev else 'Create',
                            halign=Gtk.Align.END, margin_top=12)
        create.add_css_class('suggested-action')
        create.connect('clicked', self._save)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24,
                      margin_start=18, margin_end=18)
        box.append(g)
        box.append(self.members_group)
        box.append(self.target_group)
        box.append(create)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=520)
        clamp.set_child(box)
        sw.set_child(clamp)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        self.set_child(view)

        self._kind_changed()
        async_call(pw.list_audio_nodes, self._nodes_loaded)

    def _current_kind(self):
        return self.KIND_KEYS[self.kind_row.get_selected()]

    def _kind_changed(self, *_a):
        kind = self._current_kind()
        self.members_group.set_visible(kind.startswith('combine'))
        self.target_group.set_visible(kind == 'bus')
        self._fill_members()

    def _nodes_loaded(self, nodes, error):
        if error or nodes is None:
            return
        self._nodes = nodes
        self._fill_members()
        names = ['Follow default / route manually'] + \
                [n.description for n in nodes if n.is_sink]
        self.target_row.set_model(Gtk.StringList.new(names))
        if self.dev and self.dev.target:
            sinks = [n for n in self._nodes if n.is_sink]
            idx = next((i + 1 for i, n in enumerate(sinks)
                        if n.name == self.dev.target), 0)
            self.target_row.set_selected(idx)

    def _fill_members(self):
        for row in self.member_rows.values():
            self.members_group.remove(row)
        self.member_rows = {}
        if not self._current_kind().startswith('combine'):
            return
        want_sinks = self._current_kind() == 'combine-sink'
        selected = set(self.dev.members) if self.dev else set()
        for n in self._nodes:
            if n.is_sink != want_sinks:
                continue
            if n.name.startswith('pwctl.'):
                continue          # avoid self-referencing loops
            row = Adw.SwitchRow(title=n.description, subtitle=n.name)
            row.set_active(n.name in selected)
            self.members_group.add(row)
            self.member_rows[n.name] = row

    def _save(self, _b):
        name = self.name_row.get_text().strip()
        if not name:
            self.window.toast('Give the device a name')
            return
        kind = self._current_kind()
        positions = list(LAYOUTS[self.layout_row.get_selected()][2])
        members = [n for n, row in self.member_rows.items()
                   if row.get_active()]
        if kind.startswith('combine') and len(members) < 2:
            self.window.toast('Pick at least two member devices')
            return
        target = ''
        if kind == 'bus' and self.target_row.get_selected() > 0:
            sinks = [n for n in self._nodes if n.is_sink]
            idx = self.target_row.get_selected() - 1
            if 0 <= idx < len(sinks):
                target = sinks[idx].name

        if self.dev:
            dev = self.dev
            dev.name = name
            dev.positions = positions
            dev.members = members
            dev.target = target if kind == 'bus' else ''
            dev.persistent = self.persist_row.get_active()
        else:
            dev = virtual.new_device(name, kind, positions=positions,
                                     members=members, target=target,
                                     persistent=self.persist_row.get_active())
            dev.enabled = True

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(f'“{name}” ready' if ok
                              else f'Failed: {err or "unknown error"}')
            self.page.refresh()
        self.close()
        async_call(lambda: virtual.apply(dev), done)
