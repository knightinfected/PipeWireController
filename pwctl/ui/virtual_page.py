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

POSITION_NAMES = virtual.POSITION_NAMES


def _wrap_list_factory():
    """A ComboRow popup factory whose items wrap instead of ellipsizing —
    so long type labels (with their parenthetical descriptions) stay legible."""
    factory = Gtk.SignalListItemFactory()

    def setup(_f, item):
        item.set_child(Gtk.Label(xalign=0, wrap=True, max_width_chars=34))

    def bind(_f, item):
        item.get_child().set_label(item.get_item().get_string())
    factory.connect('setup', setup)
    factory.connect('bind', bind)
    return factory

KIND_ICONS = {
    'null-sink': 'audio-speakers-symbolic',
    'null-source': 'audio-input-microphone-symbolic',
    'combine-sink': 'view-grid-symbolic',
    'combine-source': 'view-grid-symbolic',
    'bus': 'network-wired-symbolic',
    'pro-map-sink': 'audio-card-symbolic',
    'pro-map-source': 'audio-card-symbolic',
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
                     'device · bus / sub-mix · Pro Audio channel map')
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
        VirtualDialog(self.window, self, dev).present()


class VirtualDialog(Adw.Window):
    KIND_KEYS = list(virtual.KINDS)

    def __init__(self, window, page, dev: virtual.VirtualDevice | None):
        super().__init__(title='Edit virtual device' if dev
                         else 'New virtual device',
                         transient_for=window, modal=True, resizable=True,
                         default_width=640, default_height=760)
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
        self.kind_row.set_list_factory(_wrap_list_factory())
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

        # pro-audio channel map
        self.pro_targets = []
        self._aux_names = []
        self.map_entries = []
        self._pro_updating = False
        self.pro_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                  spacing=10)
        self.pro_banner.add_css_class('pwctl-note')
        _note_icon = Gtk.Image.new_from_icon_name('dialog-information-symbolic')
        _note_icon.set_valign(Gtk.Align.START)
        _note_lbl = Gtk.Label(
            label='Pro Audio channels don’t auto-route like normal sinks, so '
                  'this links straight to the chosen device. If that device is '
                  'unplugged or leaves Pro Audio mode, toggle this device off '
                  'and on to rebuild the mapping.',
            wrap=True, xalign=0, hexpand=True)
        self.pro_banner.append(_note_icon)
        self.pro_banner.append(_note_lbl)
        self.pro_banner.set_visible(False)
        self.pro_group = Adw.PreferencesGroup(
            title='Pro Audio device',
            description='Set a sound card to the "Pro Audio" profile to expose '
                        'its raw AUX channels here.')
        self.pro_target_row = Adw.ComboRow(title='Target device')
        self.pro_target_row.connect('notify::selected', self._target_changed)
        self.pro_group.add(self.pro_target_row)

        self.map_group = Adw.PreferencesGroup(
            title='Channel map',
            description='Each virtual channel links to one hardware AUX '
                        'channel (index passthrough, no remixing).')
        add_btn = Gtk.Button(icon_name='list-add-symbolic',
                             valign=Gtk.Align.CENTER, tooltip_text='Add channel')
        add_btn.connect('clicked', lambda *_: self._add_map_row())
        self.map_group.set_header_suffix(add_btn)

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
        box.append(self.pro_banner)
        box.append(self.pro_group)
        box.append(self.map_group)
        box.append(create)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=520)
        clamp.set_child(box)
        sw.set_child(clamp)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        self.set_content(view)

        self._kind_changed()
        async_call(pw.list_audio_nodes, self._nodes_loaded)

    def _current_kind(self):
        return self.KIND_KEYS[self.kind_row.get_selected()]

    def _kind_changed(self, *_a):
        kind = self._current_kind()
        is_pro = kind.startswith('pro-map')
        self.members_group.set_visible(kind.startswith('combine'))
        self.target_group.set_visible(kind == 'bus')
        self.pro_group.set_visible(is_pro)
        self.map_group.set_visible(is_pro)
        self.pro_banner.set_visible(is_pro)
        self.layout_row.set_visible(not is_pro)  # pro-map positions come
        #                                          from the channel map
        self._fill_members()
        if is_pro:
            self._load_pro_targets()

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

    # ---------------------------------------------------- pro-audio map --
    def _load_pro_targets(self):
        direction = 'sink' if self._current_kind() == 'pro-map-sink' \
            else 'source'
        async_call(lambda: virtual.list_pro_targets(direction),
                   self._pro_targets_loaded)

    def _pro_targets_loaded(self, targets, error):
        if error or targets is None:
            targets = []
        self.pro_targets = targets
        self._pro_updating = True
        try:
            if not targets:
                self.pro_target_row.set_model(Gtk.StringList.new(
                    ['No Pro Audio device found']))
                self.pro_target_row.set_sensitive(False)
                self._aux_names = []
                self._clear_map()
                return
            self.pro_target_row.set_sensitive(True)
            self.pro_target_row.set_model(
                Gtk.StringList.new([t[1] for t in targets]))
            sel = 0
            if self.dev and self.dev.target:
                sel = next((i for i, t in enumerate(targets)
                            if t[0] == self.dev.target), 0)
            self.pro_target_row.set_selected(sel)
            self._aux_names = list(targets[sel][2])
            # editing this device: restore its saved map; else default 1:1
            if self.dev and self.dev.target == targets[sel][0] \
                    and self.dev.target_positions:
                self._build_map(self.dev.positions,
                                self.dev.target_positions)
            else:
                self._default_map()
        finally:
            self._pro_updating = False

    def _target_changed(self, *_a):
        if self._pro_updating:
            return
        idx = self.pro_target_row.get_selected()
        if 0 <= idx < len(self.pro_targets):
            self._aux_names = list(self.pro_targets[idx][2])
            self._default_map()          # aux set changed → reset the map

    def _clear_map(self):
        for e in self.map_entries:
            self.map_group.remove(e['row'])
        self.map_entries = []

    def _build_map(self, positions, aux_positions):
        self._clear_map()
        for vpos, aux in zip(positions, aux_positions):
            self._add_map_row(vpos, aux)

    def _default_map(self):
        # pair the first N AUX channels with FL/FR/… in order
        n = min(2, len(self._aux_names)) or len(self._aux_names)
        defaults = ['FL', 'FR', 'FC', 'LFE', 'RL', 'RR', 'SL', 'SR']
        self._build_map(defaults[:n], self._aux_names[:n])

    def _add_map_row(self, vpos=None, aux_name=None):
        if not self._aux_names:
            return
        row = Adw.ActionRow(title='→')
        vdd = Gtk.DropDown.new_from_strings(POSITION_NAMES)
        vdd.set_valign(Gtk.Align.CENTER)
        if vpos in POSITION_NAMES:
            vdd.set_selected(POSITION_NAMES.index(vpos))
        add = Gtk.DropDown.new_from_strings(self._aux_names)
        add.set_valign(Gtk.Align.CENTER)
        if aux_name in self._aux_names:
            add.set_selected(self._aux_names.index(aux_name))
        elif len(self.map_entries) < len(self._aux_names):
            add.set_selected(len(self.map_entries))   # next unused AUX
        entry = {'row': row, 'vdd': vdd, 'add': add}
        rm = icon_button('list-remove-symbolic', 'Remove channel',
                         lambda *_: self._remove_map_row(entry))
        row.add_prefix(vdd)
        row.add_suffix(add)
        row.add_suffix(rm)
        self.map_group.add(row)
        self.map_entries.append(entry)

    def _remove_map_row(self, entry):
        self.map_group.remove(entry['row'])
        if entry in self.map_entries:
            self.map_entries.remove(entry)

    def _save(self, _b):
        name = self.name_row.get_text().strip()
        if not name:
            self.window.toast('Give the device a name')
            return
        kind = self._current_kind()
        positions = list(LAYOUTS[self.layout_row.get_selected()][2])
        members = [n for n, row in self.member_rows.items()
                   if row.get_active()]
        target = ''
        target_positions = []
        if kind.startswith('combine') and len(members) < 2:
            self.window.toast('Pick at least two member devices')
            return
        if kind == 'bus' and self.target_row.get_selected() > 0:
            sinks = [n for n in self._nodes if n.is_sink]
            idx = self.target_row.get_selected() - 1
            if 0 <= idx < len(sinks):
                target = sinks[idx].name
        if kind.startswith('pro-map'):
            tsel = self.pro_target_row.get_selected()
            if not self.pro_targets or not (0 <= tsel < len(self.pro_targets)):
                self.window.toast('Pick a target Pro Audio device')
                return
            target = self.pro_targets[tsel][0]
            positions, target_positions = [], []
            for e in self.map_entries:
                ai = e['add'].get_selected()
                if not (0 <= ai < len(self._aux_names)):
                    continue
                positions.append(POSITION_NAMES[e['vdd'].get_selected()])
                target_positions.append(self._aux_names[ai])
            if not positions:
                self.window.toast('Add at least one channel mapping')
                return
            if len(set(target_positions)) != len(target_positions):
                self.window.toast('Each AUX channel can be mapped only once')
                return

        if self.dev:
            dev = self.dev
            dev.name = name
            dev.positions = positions
            dev.members = members
            dev.target = target
            dev.target_positions = target_positions
            dev.persistent = self.persist_row.get_active()
        else:
            dev = virtual.new_device(
                name, kind, positions=positions, members=members,
                target=target, target_positions=target_positions,
                persistent=self.persist_row.get_active())
            dev.enabled = True

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(f'“{name}” ready' if ok
                              else f'Failed: {err or "unknown error"}')
            self.page.refresh()
        self.close()
        async_call(lambda: virtual.apply(dev), done)
