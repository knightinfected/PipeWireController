"""Dashboard: Overview (status cards) + Mixer (streams and devices).

Two views behind one toggle in the header bar.

**Overview** answers "is my audio working, and what is it doing" — a status
hero, the two default endpoints as real controls, whatever is playing, and an
inventory of everything the app has running.  **Mixer** is the pavucontrol-style
lists, reached through two binary toggles (Output/Input x Apps/Devices) instead
of a five-wide tab strip, because "is this a device tab or a stream tab?" was
the ambiguity the tab strip created.

The old five tab names still work everywhere they were documented — `PWCTL_TAB`
and the saved `dashboard_tab` pref both accept them and resolve onto the new
(view, direction, kind) triple.  See `_LEGACY_TABS`.
"""

from __future__ import annotations

import os

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from ..backend import chains, enhance, paths, prefs, pw, surround, system, virtual
from .volume import VOLUME_STYLES, make_volume
from .widgets import (ColumnBox, GraceMixin, RowSync, async_call, esc, micro,
                      page_scroller, pill, state_style)

SERVICES = [('pipewire.service', 'PipeWire'),
            ('wireplumber.service', 'WirePlumber'),
            ('pipewire-pulse.service', 'PipeWire-Pulse')]

# The five tab names this page used to have.  They are in CLAUDE.md, in the
# screenshot agent and in every existing ui.json, so they keep working: each
# resolves to a view and, for the mixer, which of the four lists to show.
_LEGACY_TABS = {
    'overview':  ('overview', None, None),
    'playback':  ('mixer', 'output', 'apps'),
    'recording': ('mixer', 'input', 'apps'),
    'outputs':   ('mixer', 'output', 'devices'),
    'inputs':    ('mixer', 'input', 'devices'),
}

# The slow half of the poll.  Service states and every managed object's status
# shell out (systemctl per object), so they run every Nth tick instead of every
# tick — the fast half is one pw-dump, which now carries the volumes too.
SLOW_EVERY = 4


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


def dot(css: str = 'dim') -> Gtk.Label:
    d = Gtk.Label()
    d.add_css_class('dash-dot')
    d.add_css_class(css)
    d.set_valign(Gtk.Align.CENTER)
    return d


def card(title: str = '', icon: str = '', *, link: tuple | None = None):
    """A dashboard card.  Returns (card, body) — pack content into `body`.

    `link` is (label, callback) and renders as the card's top-right action, the
    way every card on the board offers a way through to the page that owns the
    thing it is describing.
    """
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.add_css_class('dash-card')
    if title:
        head = Gtk.Box(spacing=9)
        if icon:
            img = Gtk.Image.new_from_icon_name(icon)
            img.add_css_class('dash-card-icon')
            head.append(img)
        lbl = Gtk.Label(label=title, xalign=0, hexpand=True)
        lbl.add_css_class('dash-card-title')
        head.append(lbl)
        if link:
            text, cb = link
            btn = Gtk.Button(label=text)
            btn.add_css_class('flat')
            btn.add_css_class('dash-link')
            btn.set_valign(Gtk.Align.CENTER)
            btn.connect('clicked', lambda _b: cb())
            head.append(btn)
        box.append(head)
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.append(body)
    return box, body


def kv(key: str, value_widget) -> Gtk.Box:
    row = Gtk.Box(spacing=10)
    row.add_css_class('dash-kv')
    k = Gtk.Label(label=key, xalign=0, hexpand=True,
                  ellipsize=Pango.EllipsizeMode.END, max_width_chars=24)
    k.add_css_class('dim-label')
    row.append(k)
    row.append(value_widget)
    return row


# ---------------------------------------------------------------- mixer rows --

class _VolumeRowBase(Gtk.ListBoxRow, GraceMixin):
    """Two-line row: header line + full-width volume control, pavucontrol style."""

    def __init__(self, style):
        super().__init__(activatable=False)
        self.updating = False
        self.filter_text = ''

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
        self.vol.set_meter(stream.serial)

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
            # Rows are reused while the membership (node ids) holds, but ids
            # get recycled and serials do not — so re-point the meter every
            # time or a restarted node keeps a meter aimed at a dead serial.
            self.vol.set_meter(stream.serial)
            self.title.set_label(stream.name)
            sub = stream.media if stream.media != stream.name else ''
            self.subtitle.set_label(sub)
            self.subtitle.set_visible(bool(sub))
            self.filter_text = f'{stream.name} {sub}'.lower()

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
        self.vol.set_meter(node.serial)

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
            self.vol.set_meter(node.serial)      # see _StreamRow.update
            self.title.set_label(node.description)
            self.set_tooltip_text(node.name)
            self.filter_text = f'{node.description} {node.name}'.lower()

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


class _CardConfigRow(Gtk.ListBoxRow, GraceMixin):
    """Per-card configuration (ALSA/Bluetooth card profile), shown once per
    card independent of how many sink/source nodes the profile exposes.

    Kept separate from the node rows so it never moves or disappears when a
    profile change swaps the card's nodes (e.g. Pro Audio exposes several
    sinks at once) — the switcher, and the way back, are always reachable.
    """

    node_id = None      # not an audio node: skipped by solo/volume logic

    def __init__(self, tab, card_obj):
        super().__init__(activatable=False)
        self.tab = tab
        self.updating = False
        self.filter_text = ''
        self.card_id = card_obj.id
        self._card = card_obj
        self._profiles = []
        self._profile_key = None

        icon = Gtk.Image.new_from_icon_name('audio-card-symbolic')
        self.title = Gtk.Label(xalign=0, hexpand=True,
                               ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class('heading')
        caption = Gtk.Label(xalign=0, label='Configuration')
        caption.add_css_class('caption')
        caption.add_css_class('dim-label')
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        titles.append(self.title)
        titles.append(caption)

        self.warn = Gtk.Image.new_from_icon_name('dialog-warning-symbolic')
        self.warn.add_css_class('warning')
        self.warn.set_valign(Gtk.Align.CENTER)
        self.warn.set_visible(False)

        # Escape hatch for the WirePlumber saved-profile trap: one click back
        # to the best available working profile. Only shown when stuck.
        self.reset = Gtk.Button(label='Reset',
                                tooltip_text='Switch to a working profile')
        self.reset.add_css_class('flat')
        self.reset.set_valign(Gtk.Align.CENTER)
        self.reset.set_visible(False)
        self.reset.connect('clicked', self._on_reset)

        self.config_dd = Gtk.DropDown(tooltip_text='Card profile')
        self.config_dd.set_valign(Gtk.Align.CENTER)
        self.config_dd.connect('notify::selected', self._on_profile)

        header = Gtk.Box(spacing=10, margin_top=14, margin_bottom=14,
                         margin_start=16, margin_end=16)
        header.append(icon)
        header.append(titles)
        header.append(self.warn)
        header.append(self.reset)
        header.append(self.config_dd)
        self.set_child(header)

    def _on_profile(self, dd, _pspec):
        if self.updating:
            return
        idx = dd.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._profiles):
            return
        prof_index, label = self._profiles[idx]
        self._apply(prof_index, label)

    def _on_reset(self, _btn):
        idx = surround.working_profile(self._card, sink=self.tab.sinks)
        if idx is None:
            self.tab.dash.window.toast('No working profile available')
            return
        label = next((d for i, d, _a in self._card.profiles if i == idx), '')
        self._apply(idx, label)

    def _apply(self, prof_index, label):
        cid, dash = self.card_id, self.tab.dash
        self.touch()
        async_call(lambda: surround.set_profile(cid, prof_index),
                   lambda ok, e: (dash.window.toast(
                       f'Configuration: {label}' if ok and not e
                       else 'Configuration change failed'),
                       dash.refresh_soon()))

    def update(self, card_obj):
        self.updating = True
        try:
            self._card = card_obj
            self.title.set_label(card_obj.description)
            self.filter_text = card_obj.description.lower()
            profiles = [(pidx, desc + (' (unavailable)' if avail == 'no'
                                       else ''))
                        for pidx, desc, avail in card_obj.profiles]
            self._profiles = profiles
            pkey = tuple(profiles)
            if pkey != self._profile_key:
                self._profile_key = pkey
                self.config_dd.set_model(
                    Gtk.StringList.new([p[1] for p in profiles]))
            if profiles and not self.in_grace:
                aidx = next((i for i, p in enumerate(profiles)
                             if p[0] == card_obj.active_profile), None)
                self.config_dd.set_selected(
                    aidx if aidx is not None else Gtk.INVALID_LIST_POSITION)
            # Two independent signals. The ⚠ means the active profile is
            # *explicitly unavailable* (avail == 'no', e.g. HDMI with nothing
            # plugged in) — a real problem worth flagging. 'unknown' (e.g. Pro
            # Audio, which ALSA can't probe) is playable, so it gets no alarm.
            # Reset appears whenever the card produces no usable output/input
            # in this list's direction (unavailable OR Off/wrong-direction) and
            # a working profile exists to fall back to.
            want_sink = self.tab.sinks
            avail = next((a for pidx, _d, a in card_obj.profiles
                          if pidx == card_obj.active_profile), 'unknown')
            has_sink, has_src = card_obj.dirs.get(card_obj.active_profile,
                                                  (False, False))
            unavailable = avail == 'no'
            no_output = not (has_sink if want_sink else has_src)
            can_fix = surround.working_profile(
                card_obj, sink=want_sink) is not None
            self.warn.set_visible(unavailable)
            self.warn.set_tooltip_text(
                'This profile is unavailable — no output until you switch.'
                if want_sink else
                'This profile is unavailable — no input until you switch.')
            self.reset.set_visible((unavailable or no_output) and can_fix)
        finally:
            self.updating = False


class _ListTab:
    """A boxed list with an empty-state label and a search filter."""

    def __init__(self, dash, empty_text):
        self.dash = dash
        self.soloed = set()     # node ids currently soloed in this list
        self.listbox = Gtk.ListBox(css_classes=['boxed-list', 'vol-list'],
                                   selection_mode=Gtk.SelectionMode.NONE)
        self.sync = RowSync(self.listbox)
        self.listbox.set_filter_func(self._filter)
        self.empty = Gtk.Label(label=empty_text, margin_top=48)
        self.empty.add_css_class('dim-label')
        # 860 left a maximised window mostly empty; rows stay readable well
        # past this, and the mixer is the view people leave open.
        self.widget = page_scroller(self.listbox, self.empty, width=1100)

    @property
    def rows(self):
        return self.sync.rows

    def _filter(self, row):
        needle = self.dash.filter_text
        if not needle:
            return True
        return needle in getattr(row, 'filter_text', '')

    def refilter(self):
        self.listbox.invalidate_filter()

    def clear(self):
        """Drop all rows so the next update rebuilds them (style change)."""
        self.sync.clear()
        self.soloed = set()

    def toggle_solo(self, row):
        """Solo: everything else in this list is muted while any solo is on."""
        if row.solo.get_active():
            self.soloed.add(row.node_id)
        else:
            self.soloed.discard(row.node_id)
        audio_rows = [r for r in self.rows.values() if r.node_id is not None]
        live = {r.node_id for r in audio_rows}
        self.soloed &= live
        soloing = bool(self.soloed)
        for r in audio_rows:
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
        before = list(self.sync.rows)
        pairs = self.sync.sync(items, make_row)
        if list(self.sync.rows) != before:
            self.soloed = set()
        self.listbox.set_visible(bool(items))
        self.empty.set_visible(not items)
        return pairs


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
        self.refilter()


class DevicesTab(_ListTab):
    def __init__(self, dash, sinks: bool):
        super().__init__(dash, 'No output devices found.' if sinks
                         else 'No input devices found.')
        self.sinks = sinks

    def update(self, nodes, cards=()):
        cardmap = {c.id: c for c in cards}

        def card_of(n):
            try:
                return cardmap.get(int(n.props.get('device.id')))
            except (TypeError, ValueError):
                return None

        # A card gets a Configuration row in this list if it offers more than
        # one profile AND can produce this list's direction (sink/source). The
        # row is driven by the CARD list, not the nodes, so it stays put even
        # when the active profile exposes zero nodes (e.g. Off) — otherwise
        # the switcher would vanish with no way back.
        cfg_cards = {c.id: c for c in cards if len(c.profiles) > 1
                     and (c.has_sink if self.sinks else c.has_source)}

        grouped: dict[int, list] = {cid: [] for cid in cfg_cards}
        loose = []
        for n in (n for n in nodes if n.is_sink == self.sinks):
            c = card_of(n)
            if c and c.id in cfg_cards:
                grouped[c.id].append(n)
            else:
                loose.append(n)

        items = []
        for c in sorted(cfg_cards.values(),
                        key=lambda c: c.description.lower()):
            items.append((('cfg', c.id), c))
            for n in sorted(grouped[c.id],
                            key=lambda n: n.description.lower()):
                items.append((n.id, n))
        # Virtual devices (no card) and single-profile-card nodes trail after.
        for n in sorted(loose, key=lambda n: (n.is_virtual,
                                              n.description.lower())):
            items.append((n.id, n))

        pairs = self._sync_rows(
            items,
            lambda o: (_CardConfigRow(self, o)
                       if isinstance(o, surround.Card) else _DeviceRow(self, o)))
        for row, obj in pairs:
            row.update(obj)
        self.refilter()


# -------------------------------------------------------- overview endpoint --

class _EndpointCard:
    """The default output or input, as a card you can actually operate.

    This is the fragmentation fix: the default endpoint used to be readable in
    four places with four different capability sets, and the one on this page
    was the weakest of them (volume and mute only).  It now carries the port
    selector and a way to change the default too, so "switch to headphones and
    turn it down" does not need another page.
    """

    def __init__(self, dash, sink: bool):
        self.dash = dash
        self.sink = sink
        self.node_id = None
        self.serial = None
        self.updating = False
        self._port_key = None
        self._ports = []
        self._nodes = []

        title = 'Default output' if sink else 'Default input'
        icon = ('audio-speakers-symbolic' if sink
                else 'audio-input-microphone-symbolic')
        self.card, body = card(title, icon,
                               link=('Change', self._choose))

        self.avatar = Gtk.Image.new_from_icon_name(icon)
        self.avatar.add_css_class('dash-avatar')
        self.avatar.add_css_class('out' if sink else 'in')
        self.name = Gtk.Label(xalign=0, hexpand=True, max_width_chars=22,
                              ellipsize=Pango.EllipsizeMode.END)
        self.name.add_css_class('dash-endpoint-name')
        self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                             max_width_chars=26)
        self.sub.add_css_class('caption')
        self.sub.add_css_class('dim-label')
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        names.append(self.name)
        names.append(self.sub)

        self.port_dd = Gtk.DropDown(tooltip_text='Port')
        self.port_dd.set_valign(Gtk.Align.CENTER)
        self.port_dd.connect('notify::selected', self._on_port)

        line = Gtk.Box(spacing=12)
        line.append(self.avatar)
        line.append(names)
        line.append(self.port_dd)
        body.append(line)

        self.mute = _mute_button()
        self.mute.connect('toggled', self._on_mute)
        self.pct = _pct_label()
        self.vol = None
        self.vol_line = Gtk.Box(spacing=10)
        body.append(self.vol_line)
        self.rebuild_volume()

    # the volume control is swapped when the style preference changes
    def rebuild_volume(self):
        child = self.vol_line.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.vol_line.remove(child)
            child = nxt
        self.vol = make_volume(self.dash.volume_style, self._on_volume)
        self.vol_line.append(self.mute)
        self.vol_line.append(self.vol.widget)
        self.vol_line.append(self.pct)
        if self.serial is not None:
            self.vol.set_meter(self.serial)

    def _on_volume(self, value):
        self.pct.set_label(f'{value * 100:.0f}%')
        if self.node_id is not None:
            self.dash.touch_local()
            async_call(lambda: pw.set_volume(self.node_id, value))

    def _on_mute(self, btn):
        if self.updating or self.node_id is None:
            return
        self.dash.touch_local()
        active = btn.get_active()
        async_call(lambda: pw.set_mute(self.node_id, active))

    def _on_port(self, dd, _pspec):
        if self.updating:
            return
        idx = dd.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._ports):
            return
        route_index, label = self._ports[idx]
        self.dash.touch_local()
        nid, window = self.node_id, self.dash.window
        async_call(lambda: pw.set_route(nid, route_index),
                   lambda ok, e: window.toast(
                       f'Port: {label}' if ok and not e
                       else 'Port change failed'))

    def _choose(self):
        """Pick a different default endpoint."""
        options = [n for n in self._nodes if n.is_sink == self.sink]
        if not options:
            self.dash.window.toast('No devices available')
            return
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                         css_classes=['boxed-list'])
        dlg = Adw.Dialog(title='Default output' if self.sink
                         else 'Default input',
                         content_width=460, content_height=440)
        for n in sorted(options, key=lambda n: n.description.lower()):
            row = Adw.ActionRow(title=esc(n.description),
                                subtitle=esc(n.name), activatable=True)
            if n.is_default:
                row.add_suffix(Gtk.Image.new_from_icon_name('object-select-symbolic'))
            row.connect('activated', self._pick, n.id, dlg)
            lb.append(row)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vexpand=True)
        sw.set_child(lb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        box.append(sw)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(box)
        dlg.set_child(view)
        dlg.present(self.dash.window)

    def _pick(self, _row, node_id, dlg):
        dlg.close()
        window = self.dash.window
        async_call(lambda: pw.set_default(node_id),
                   lambda ok, e: (window.toast('Default device changed'),
                                  self.dash.refresh_soon()))

    def update(self, node, nodes, in_grace):
        self._nodes = nodes
        self.updating = True
        try:
            if node is None:
                self.node_id = self.serial = None
                self.name.set_label('None')
                self.sub.set_label('no default device')
                self.port_dd.set_visible(False)
                # Hide rather than dim: a disabled LED meter still paints a
                # full bar of segments, which reads as a live level.
                self.vol_line.set_visible(False)
                return
            self.vol_line.set_visible(True)
            self.node_id = node.id
            if node.serial != self.serial:
                self.serial = node.serial
                self.vol.set_meter(node.serial)
            self.name.set_label(node.description)
            self.sub.set_label(node.name)
            self.card.set_tooltip_text(node.name)

            ports = [(idx, desc + (' (unplugged)' if avail == 'no' else ''))
                     for idx, desc, avail in node.ports]
            self._ports = ports
            self.port_dd.set_visible(bool(ports))
            key = tuple(ports)
            if key != self._port_key:
                self._port_key = key
                self.port_dd.set_model(
                    Gtk.StringList.new([p[1] for p in ports]))
            if ports and not in_grace:
                idx = next((i for i, p in enumerate(ports)
                            if p[0] == node.active_port), None)
                self.port_dd.set_selected(
                    idx if idx is not None else Gtk.INVALID_LIST_POSITION)

            if not in_grace:
                if node.volume is not None:
                    self.vol.set_value(node.volume)
                    self.pct.set_label(f'{node.volume * 100:.0f}%')
                self.mute.set_active(node.muted)
        finally:
            self.updating = False


# ------------------------------------------------------------ favourites --

class _FavRow(Gtk.Box, GraceMixin):
    """One pinned device, on a single line: name, mute, volume, remove.

    One line rather than the two the mixer rows use, because the point of this
    card is that you can keep several on the board at once — a two-line row
    turns four favourites into a card taller than the rest of the column.
    """

    def __init__(self, fav, node_name):
        super().__init__(spacing=8)
        self.add_css_class('dash-kv')
        self.fav = fav
        self.node_name = node_name
        self.node_id = None
        self.serial = None
        self.updating = False

        self.icon = Gtk.Image.new_from_icon_name('audio-speakers-symbolic')
        self.icon.add_css_class('dash-card-icon')
        self.label = Gtk.Label(xalign=0, hexpand=True, max_width_chars=18,
                               ellipsize=Pango.EllipsizeMode.END)
        self.missing = Gtk.Label(label='not connected')
        self.missing.add_css_class('caption')
        self.missing.add_css_class('dim-label')
        self.missing.set_visible(False)

        self.mute = _mute_button()
        self.mute.connect('toggled', self._on_mute)
        self.vol = make_volume(fav.dash.volume_style, self._on_volume,
                               compact=True)
        self.pct = _pct_label()

        self.remove = Gtk.Button(icon_name='window-close-symbolic',
                                 tooltip_text='Remove from favorites')
        self.remove.add_css_class('flat')
        self.remove.set_valign(Gtk.Align.CENTER)
        self.remove.connect('clicked', lambda _b: fav.remove(self.node_name))

        for w in (self.icon, self.label, self.missing, self.mute,
                  self.vol.widget, self.pct, self.remove):
            self.append(w)

    def _on_volume(self, value):
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

    def update(self, node):
        """node is None when the device is not on this machine right now."""
        self.updating = True
        try:
            live = node is not None
            # Hide rather than dim: a disabled LED meter still paints a full
            # bar of segments, which reads as a live level.
            for w in (self.mute, self.vol.widget, self.pct):
                w.set_visible(live)
            self.missing.set_visible(not live)
            if not live:
                self.node_id = self.serial = None
                self.vol.set_meter(None)
                self.label.set_label(self.fav.label_for(self.node_name))
                self.label.add_css_class('dim-label')
                self.set_tooltip_text(
                    f'{self.node_name} is not available right now — it stays '
                    'here and comes back with the device.')
                return
            self.label.remove_css_class('dim-label')
            self.node_id = node.id
            if node.serial != self.serial:
                self.serial = node.serial
                self.vol.set_meter(node.serial)
            self.icon.set_from_icon_name(
                'application-x-addon-symbolic' if node.is_virtual
                else 'audio-speakers-symbolic' if node.is_sink
                else 'audio-input-microphone-symbolic')
            self.label.set_label(node.description)
            self.set_tooltip_text(node.name)
            if not self.in_grace:
                if node.volume is not None:
                    self.vol.set_value(node.volume)
                    self.pct.set_label(f'{node.volume * 100:.0f}%')
                self.mute.set_active(node.muted)
        finally:
            self.updating = False


class _FavouritesCard:
    """Pin any output or input — hardware or virtual — and keep its volume
    on the Overview.

    The two cards above this one cover the *defaults*; this covers "the two
    other things I actually touch".  It is deliberately name-keyed and
    forgiving: a device that is unplugged keeps its place instead of being
    dropped, because silently forgetting a favourite the first time a dock
    comes off is the behaviour nobody wants.
    """

    def __init__(self, dash):
        self.dash = dash
        self.names = list(prefs.get('favorite_devices') or [])
        # Remembered descriptions, so an unplugged favourite reads as its name
        # and not as `alsa_output.usb-Focusrite_Scarlett-00.analog-stereo`.
        self._labels: dict[str, str] = dict(prefs.get('favorite_labels') or {})
        self._saved_labels = dict(self._labels)
        self._nodes: list = []
        self._keys: list = []
        self._rows: dict[str, _FavRow] = {}

        self.card, body = card('Favorites', 'starred-symbolic',
                               link=('Add', self.choose))
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.empty = Gtk.Label(
            label='Nothing pinned yet.  Add an output or input and its '
                  'volume stays on this board.',
            xalign=0, wrap=True, max_width_chars=34)
        self.empty.add_css_class('dim-label')
        body.append(self.list)
        body.append(self.empty)

    # -- the pinned set ---------------------------------------------------
    def label_for(self, name):
        return self._labels.get(name, name)

    def _save(self):
        prefs.save(favorite_devices=self.names)

    def remove(self, name):
        if name in self.names:
            self.names.remove(name)
            self._save()
            self.update(self._nodes)

    def choose(self):
        """Tick the devices to keep on the board."""
        if not self._nodes:
            self.dash.window.toast('No devices available yet')
            return
        dlg = Adw.AlertDialog(
            heading='Favorite devices',
            body='Pinned devices get a volume control on the Overview. '
                 'Outputs, inputs and virtual devices all work.')
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                              css_classes=['boxed-list'])
        checks = []
        for n in sorted(self._nodes,
                        key=lambda n: (not n.is_sink, n.description.lower())):
            row = Adw.ActionRow(
                title=esc(n.description),
                subtitle=esc(('Output' if n.is_sink else 'Input')
                             + (' · virtual' if n.is_virtual else '')
                             + f' · {n.name}'),
                title_lines=1, subtitle_lines=1)
            chk = Gtk.CheckButton(valign=Gtk.Align.CENTER,
                                  active=n.name in self.names)
            row.add_prefix(chk)
            row.set_activatable_widget(chk)
            listbox.append(row)
            checks.append((chk, n.name))
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                min_content_height=280,
                                propagate_natural_height=True)
        sw.set_child(listbox)
        dlg.set_extra_child(sw)
        dlg.add_response('cancel', 'Cancel')
        dlg.add_response('ok', 'Save')
        dlg.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response('ok')

        def on_resp(_d, resp):
            if resp != 'ok':
                return
            picked = [name for chk, name in checks if chk.get_active()]
            # Keep the order the user already had, append what is new: the
            # card is a shortlist they arrange, not a sorted directory.
            self.names = ([n for n in self.names if n in picked]
                          + [n for n in picked if n not in self.names])
            self._save()
            self.update(self._nodes)
        dlg.connect('response', on_resp)
        dlg.present(self.dash.window)

    # -- refresh -----------------------------------------------------------
    def rebuild_volume(self):
        """Force new rows, so they pick up a changed volume-control style."""
        self._keys = []
        self.update(self._nodes)

    def update(self, nodes):
        self._nodes = nodes
        by_name = {n.name: n for n in nodes}
        self._labels.update({n.name: n.description for n in nodes})
        # Written only when what we know about a *pinned* device changes, so a
        # three-second poll is not a three-second write.
        known = {n: self._labels[n] for n in self.names if n in self._labels}
        if known != self._saved_labels:
            self._saved_labels = known
            prefs.save(favorite_labels=known)

        # Same rule as every other live list on this page: rebuild only when
        # the membership changes, or a poll would restart the meters and drop
        # a drag every three seconds.
        if self._keys != self.names:
            self._keys = list(self.names)
            child = self.list.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self.list.remove(child)
                child = nxt
            self._rows = {}
            for name in self.names:
                row = _FavRow(self, name)
                self.list.append(row)
                self._rows[name] = row
        for name, row in self._rows.items():
            row.update(by_name.get(name))
        self.empty.set_visible(not self.names)


# ------------------------------------------------------------------ the page --

class Dashboard:
    def __init__(self, window):
        self.window = window
        self._timer = None
        self._busy = False
        self._soon = None
        self._calc_init = False
        self._tick_n = 0
        self._slow = {}          # last slow-tier result, reused between ticks
        self._local_ts = 0.0
        self.filter_text = ''
        self.volume_style = prefs.get('volume_style')

        self.playback = StreamsTab(self, playback=True)
        self.recording = StreamsTab(self, playback=False)
        self.outputs = DevicesTab(self, sinks=True)
        self.inputs = DevicesTab(self, sinks=False)

        overview = self._build_overview()
        mixer = self._build_mixer()

        self.views = Adw.ViewStack()
        self.views.add_titled_with_icon(overview, 'overview', 'Overview',
                                        'view-grid-symbolic')
        self.views.add_titled_with_icon(mixer, 'mixer', 'Mixer',
                                        'audio-volume-high-symbolic')
        self.views.set_vexpand(True)

        # Two things this page owns but the window's header bar carries, so
        # they cost no page height and stay reachable from every page:
        # the Overview/Mixer switcher and the volume-style picker.  `app.py`
        # packs them; see Window.__init__.
        self.view_switcher = self._build_view_switcher()
        self.style_button = self._build_style_picker()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self.views)

        # Restore where the user was, accepting the five legacy tab names.
        debug_tab = os.environ.get('PWCTL_TAB')      # screenshot testing
        want = debug_tab or prefs.get('dashboard_tab') or 'overview'
        view, direction, kind = _LEGACY_TABS.get(want, ('overview', None, None))
        if direction:
            self.direction = direction
            self.kind = kind
        self.views.set_visible_child_name(view)
        self._sync_switcher()
        self._sync_mixer_list()
        if not debug_tab:
            self.views.connect('notify::visible-child-name',
                               lambda *_: self._save_tab())

        self.widget = root
        self.widget.connect('map', self._on_map)
        self.widget.connect('unmap', self._on_unmap)

    # ------------------------------------------------------------ local grace --
    def touch_local(self):
        self._local_ts = GLib.get_monotonic_time() / 1e6

    @property
    def local_grace(self) -> bool:
        return GLib.get_monotonic_time() / 1e6 - self._local_ts < 2.0

    # ------------------------------------------------------------- navigation --
    direction = 'output'     # output | input
    kind = 'apps'            # apps | devices
    _switching = False       # suppress the switcher's own navigation

    def _build_view_switcher(self):
        tg = Adw.ToggleGroup(can_shrink=True)
        tg.add(Adw.Toggle(name='overview', label='Overview',
                          icon_name='view-grid-symbolic'))
        tg.add(Adw.Toggle(name='mixer', label='Mixer',
                          icon_name='audio-volume-high-symbolic'))
        tg.set_active_name('overview')
        tg.connect('notify::active-name', self._on_view_toggle)
        self._view_tg = tg
        self._switching = False
        return tg

    def _on_view_toggle(self, tg, _p):
        """The switcher is in the window header, so it is live on every page.

        On another page neither segment is lit (see `set_on_page`); clicking
        one then means "take me to the Dashboard, showing this" — which is the
        whole reason it is up there rather than inside the page.
        """
        if self._switching:
            return
        name = tg.get_active_name()
        if not name:
            return
        if name != self.views.get_visible_child_name():
            self.views.set_visible_child_name(name)
        self.window.goto('dashboard')

    def set_on_page(self, on_page: bool):
        """Called by the window when the selected page changes."""
        self._switching = True
        try:
            self._view_tg.set_active_name(
                self.views.get_visible_child_name() if on_page else None)
        finally:
            self._switching = False

    def _sync_switcher(self):
        name = self.views.get_visible_child_name()
        if name and self._view_tg.get_active_name() != name:
            self._switching = True
            try:
                self._view_tg.set_active_name(name)
            finally:
                self._switching = False

    def _save_tab(self):
        """Persist the view under the legacy tab vocabulary, so an older build
        reading the same ui.json still lands somewhere sensible."""
        self._sync_switcher()
        if self.views.get_visible_child_name() == 'overview':
            prefs.save(dashboard_tab='overview')
        else:
            prefs.save(dashboard_tab={
                ('output', 'apps'): 'playback',
                ('input', 'apps'): 'recording',
                ('output', 'devices'): 'outputs',
                ('input', 'devices'): 'inputs',
            }[(self.direction, self.kind)])

    # ------------------------------------------------------------------ mixer --
    def _build_mixer(self):
        self.dir_tg = Adw.ToggleGroup()
        self.dir_tg.add(Adw.Toggle(name='output', label='Output',
                                   icon_name='audio-speakers-symbolic'))
        self.dir_tg.add(Adw.Toggle(name='input', label='Input',
                                   icon_name='audio-input-microphone-symbolic'))
        self.dir_tg.set_active_name(self.direction)
        self.dir_tg.connect('notify::active-name', self._on_axis)

        self.kind_tg = Adw.ToggleGroup()
        self.kind_tg.add(Adw.Toggle(name='apps', label='Apps',
                                    icon_name='view-app-grid-symbolic'))
        self.kind_tg.add(Adw.Toggle(name='devices', label='Devices',
                                    icon_name='audio-card-symbolic'))
        self.kind_tg.set_active_name(self.kind)
        self.kind_tg.connect('notify::active-name', self._on_axis)

        self.search = Gtk.SearchEntry(placeholder_text='Filter…',
                                      max_width_chars=18)
        self.search.connect('search-changed', self._on_search)

        self._dir_label = micro('Direction')
        self._kind_label = micro('Show')
        bar = Gtk.Box(spacing=10, margin_top=10, margin_bottom=10,
                      margin_start=12, margin_end=12)
        bar.append(self._dir_label)
        bar.append(self.dir_tg)
        bar.append(self._kind_label)
        bar.append(self.kind_tg)
        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        bar.append(self.search)

        self.lists = Adw.ViewStack()
        self.lists.add_named(self.playback.widget, 'output-apps')
        self.lists.add_named(self.recording.widget, 'input-apps')
        self.lists.add_named(self.outputs.widget, 'output-devices')
        self.lists.add_named(self.inputs.widget, 'input-devices')
        self.lists.set_vexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(bar)
        box.append(Gtk.Separator())
        box.append(self.lists)

        # This toolbar sets the floor for the whole app: every page shares one
        # Gtk.Stack, so the stack's minimum is the widest page's.  Unshrunk it
        # measured 482px (72 + 111 + 40 + 111 + 74 plus gaps), which would have
        # raised the app minimum from 372 on its own.  The two axis labels are
        # the first thing to go — the toggles still say Output/Input and
        # Apps/Devices, so nothing becomes unreadable.
        #
        # The bin wraps the scrollers rather than sitting inside one: an
        # AdwBreakpointBin reports a minimum size of zero, so a scroller
        # holding one believes the content fits and clips it instead of
        # scrolling.  Its width-request *is* the page minimum — keep it at 352.
        bin_ = Adw.BreakpointBin(width_request=352, height_request=200)
        bin_.set_child(box)
        bp = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse('max-width: 620px'))
        bp.add_setter(self._dir_label, 'visible', False)
        bp.add_setter(self._kind_label, 'visible', False)
        bin_.add_breakpoint(bp)
        return bin_

    def _on_axis(self, *_a):
        self.direction = self.dir_tg.get_active_name() or 'output'
        self.kind = self.kind_tg.get_active_name() or 'apps'
        self._sync_mixer_list()
        self._save_tab()

    def _sync_mixer_list(self):
        self.lists.set_visible_child_name(f'{self.direction}-{self.kind}')

    def _on_search(self, entry):
        self.filter_text = entry.get_text().strip().lower()
        for tab in (self.playback, self.recording, self.outputs, self.inputs):
            tab.refilter()

    @property
    def current_tab(self):
        return {('output', 'apps'): self.playback,
                ('input', 'apps'): self.recording,
                ('output', 'devices'): self.outputs,
                ('input', 'devices'): self.inputs}[(self.direction, self.kind)]

    # -------------------------------------------------------- style picker --
    def _build_style_picker(self):
        """The volume-control style, as a header-bar button.

        It used to float over the bottom-right of the page.  It is a
        preference, not a page control — it changes the sliders on Devices,
        the Equalizer and Signal Paths too — so it belongs beside the other
        window-level button rather than hovering over one page's content.
        `can_shrink` lets the label ellipsize away to its icon when the header
        is tight, which is what keeps it from setting a floor under the app.
        """
        active = next((s for s in VOLUME_STYLES if s[0] == self.volume_style),
                      VOLUME_STYLES[0])
        self._style_content = Adw.ButtonContent(
            icon_name='preferences-system-symbolic', label=active[1],
            can_shrink=True)

        btn = Gtk.MenuButton(tooltip_text='Volume slider style')
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
        for ep in (self.out_card, self.in_card, self.fav):
            ep.rebuild_volume()
        self.refresh()

    # --------------------------------------------------------------- overview --
    def _build_overview(self):
        # 1. status hero — one dot for the whole stack, the clock beside it,
        #    the three service rows and their restart buttons one click away.
        self.hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.hero.add_css_class('dash-card')
        self.hero.add_css_class('dash-hero')

        # Every label on this strip is fed from live data and none of it is
        # short, so all of them ellipsize.  Unbounded, the service line and
        # the clock line together asked for 616px, which made the hero the
        # widest thing in the app and raised the *whole app's* minimum width
        # to 686 — every page shares one Gtk.Stack, so a page that cannot
        # shrink is charged to all sixteen.
        self.hero_dot = dot('dim')
        self.hero_title = Gtk.Label(xalign=0, label='Checking…',
                                    ellipsize=Pango.EllipsizeMode.END)
        self.hero_title.add_css_class('dash-hero-title')
        self.hero_sub = Gtk.Label(xalign=0, label='',
                                  ellipsize=Pango.EllipsizeMode.END)
        self.hero_sub.add_css_class('caption')
        self.hero_sub.add_css_class('dim-label')
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        titles.append(self.hero_title)
        titles.append(self.hero_sub)

        self.clock_big = Gtk.Label(xalign=1, ellipsize=Pango.EllipsizeMode.END)
        self.clock_big.add_css_class('dash-big')
        self.clock_sub = Gtk.Label(xalign=1, ellipsize=Pango.EllipsizeMode.END)
        self.clock_sub.add_css_class('caption')
        self.clock_sub.add_css_class('dim-label')
        clock_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        clock_box.append(self.clock_big)
        clock_box.append(self.clock_sub)

        top = Gtk.Box(spacing=14)
        top.append(self.hero_dot)
        top.append(titles)
        top.append(clock_box)
        self.hero.append(top)

        svc_expander = Gtk.Expander(label='Services')
        svc_list = Gtk.ListBox(css_classes=['boxed-list'],
                               selection_mode=Gtk.SelectionMode.NONE,
                               margin_top=10)
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
            svc_list.append(row)
            self.svc_rows[unit] = (row, p)
        svc_expander.set_child(svc_list)
        self.hero.append(svc_expander)

        # 2. the alert card, only present when something is actually wrong
        self.alert = Gtk.Box(spacing=11)
        self.alert.add_css_class('dash-card')
        self.alert.add_css_class('dash-alert')
        self.alert_icon = Gtk.Image.new_from_icon_name('dialog-warning-symbolic')
        self.alert_icon.set_valign(Gtk.Align.CENTER)
        self.alert_text = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.alert_btn = Gtk.Button(label='Open Monitor')
        self.alert_btn.set_valign(Gtk.Align.CENTER)
        self.alert_btn.connect('clicked', lambda *_: self.window.goto('monitor'))
        self.alert.append(self.alert_icon)
        self.alert.append(self.alert_text)
        self.alert.append(self.alert_btn)
        self.alert.set_visible(False)

        # 3. the two default endpoints, as operable controls, and beside them
        #    whatever else the user has pinned
        self.out_card = _EndpointCard(self, sink=True)
        self.in_card = _EndpointCard(self, sink=False)
        self.fav = _FavouritesCard(self)

        # 4. what is playing right now
        play_card, play_body = card(
            'Playing now', 'emblem-music-symbolic',
            link=('Open mixer', lambda: self._goto_mixer('output', 'apps')))
        self.play_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.play_empty = Gtk.Label(label='Nothing is playing.', xalign=0)
        self.play_empty.add_css_class('dim-label')
        play_body.append(self.play_list)
        play_body.append(self.play_empty)

        # 5. everything this app has running, in one place.  These are five
        #    different pages' objects and all of them are pwctl-chain@ units;
        #    nowhere else in the app can you see the whole set at once.
        run_card, run_body = card('Running audio objects',
                                  'system-run-symbolic')
        self.run_rows = {}
        for key, label, page in (('chains', 'Filter chains', 'chains'),
                                 ('enh', 'Equalizers and mic cleanup', 'enhance'),
                                 ('virtual', 'Virtual devices', 'virtual'),
                                 ('paths', 'Signal paths', 'paths')):
            value = Gtk.Label(label='—')
            value.add_css_class('numeric-value')
            go = Gtk.Button(icon_name='go-next-symbolic')
            go.add_css_class('flat')
            go.set_valign(Gtk.Align.CENTER)
            go.set_tooltip_text(f'Open {label}')
            go.connect('clicked', lambda _b, p=page: self.window.goto(p))
            line = Gtk.Box(spacing=8)
            line.append(value)
            line.append(go)
            run_body.append(kv(label, line))
            self.run_rows[key] = value

        # 6. quick actions
        qa_card, qa_body = card('Quick actions', 'starred-symbolic')
        grid = Gtk.Box(spacing=8, homogeneous=True)
        grid2 = Gtk.Box(spacing=8, homogeneous=True)
        for parent, (label, icon, cb) in (
                (grid, ('Restart audio', 'view-refresh-symbolic',
                        self._restart_all)),
                (grid, ('Patchbay', 'network-workgroup-symbolic',
                        lambda: self.window.goto('graph'))),
                (grid2, ('Signal Paths', 'network-transmit-receive-symbolic',
                         lambda: self.window.goto('paths'))),
                (grid2, ('Equalizer', 'audio-x-generic-symbolic',
                         lambda: self.window.goto('enhance')))):
            b = Gtk.Button()
            b.set_child(Adw.ButtonContent(icon_name=icon, label=label,
                                          can_shrink=True))
            b.add_css_class('dash-qa')
            b.connect('clicked', lambda _b, c=cb: c())
            parent.append(b)
        qa_body.append(grid)
        qa_body.append(grid2)

        # 7. the latency calculator — kept, because it is the only place in the
        #    app that can force a quantum/rate live (Tools' copy is read-only).
        calc_card, calc_body = card('Latency calculator',
                                    'preferences-system-time-symbolic')
        calc_body.append(self._build_latency_calc())

        # The same column layout every other page uses.  A Gtk.FlowBox was
        # tried first and lost twice: it packs by natural width, so inside a
        # clamp the whole board asked for ~940px and sat in the middle of a
        # 2012px content area with 430px dead on each side; and it aligns a
        # row's children to the row, so every card shorter than its tallest
        # neighbour grew a band of dead space under it.  ColumnBox balances by
        # height instead, which is what makes the cards come out level.
        #
        # Reading order, top to bottom: what you listen through, then whether
        # it is working, then everything else.  The two defaults and the
        # favourites lead because they are the controls people came for; the
        # status strip that used to sit above them was the app telling you
        # about itself before answering the question you opened it with.
        #
        # The alert and the status strip are `span=True` bands — full width
        # across every column, with the column packing resuming underneath.
        # They were siblings in an outer box before, which lined them up with
        # the scroller instead of with the columns.
        cards = ColumnBox(spacing=16, max_columns=3)
        cards.set_margin_top(20)
        cards.set_margin_bottom(28)
        cards.set_margin_start(16)
        cards.set_margin_end(16)
        for w in (self.out_card.card, self.in_card.card, self.fav.card):
            w.set_valign(Gtk.Align.START)
            cards.append(w)
        cards.append(self.alert, span=True)
        cards.append(self.hero, span=True)
        for w in (play_card, run_card, qa_card, calc_card):
            w.set_valign(Gtk.Align.START)
            cards.append(w)

        # No clamp: ColumnBox already refuses to stretch a card past MAX_COL,
        # so the width is spent on more columns and the bands span the same
        # block the cards do.
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vexpand=True)
        sw.set_child(cards)
        return sw

    def _goto_mixer(self, direction, kind):
        self.direction, self.kind = direction, kind
        self.dir_tg.set_active_name(direction)
        self.kind_tg.set_active_name(kind)
        self._sync_mixer_list()
        self.views.set_visible_child_name('mixer')
        self._sync_switcher()

    def _restart_all(self):
        self.window.toast('Restarting audio…')
        async_call(system.restart_pipewire,
                   lambda r, e: (self.window.toast(
                       'Audio restarted' if not e else f'Restart failed: {e}'),
                       self.refresh_soon()))

    # --------------------------------------------------- latency calculator --
    CALC_RATES = [44100, 48000, 88200, 96000, 176400, 192000]

    def _build_latency_calc(self):
        lb = Gtk.ListBox(css_classes=['boxed-list'],
                         selection_mode=Gtk.SelectionMode.NONE)
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

        # An Adw.ActionRow title collapses to vertical letter-wrap once suffix
        # widgets eat the width — so the row keeps one suffix and the action
        # moves below the list instead of riding in the row beside it.
        result = Adw.ActionRow(title='One cycle', subtitle='Round trip ≈ 2×',
                               title_lines=1, subtitle_lines=1)
        self.calc_result = Gtk.Label()
        self.calc_result.add_css_class('numeric-value')
        self.calc_result.set_valign(Gtk.Align.CENTER)
        result.add_suffix(self.calc_result)

        for r in (self.calc_quantum, self.calc_rate, result):
            lb.append(r)

        apply_btn = Gtk.Button(
            tooltip_text='Force this quantum and rate now (runtime only — '
                         'resets on restart; manage on the Server page)')
        apply_btn.set_child(Adw.ButtonContent(
            icon_name='media-playback-start-symbolic', label='Test live',
            can_shrink=True))
        apply_btn.connect('clicked', self._calc_apply)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(lb)
        box.append(apply_btn)
        self._calc_update()
        return box

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
        # The slow tier shells out once per service and once per managed
        # object; the fast tier is a single pw-dump that now carries the
        # volumes with it.  Running both every tick was most of the old cost.
        want_slow = (self._tick_n % SLOW_EVERY == 0) or not self._slow
        self._tick_n += 1

        def collect():
            data = {}
            data['settings'] = pw.read_settings()
            dump = pw.pw_dump()
            data['driver'] = pw.driver_clock(dump)
            data['nodes'] = pw.list_audio_nodes(dump)
            data['streams'] = pw.list_streams(dump)
            data['cards'] = surround.list_cards(dump, outputs_only=False)
            if want_slow:
                data['states'] = {u: system.unit_state(u) for u, _ in SERVICES}
                inv = {}
                for key, items, status in (
                        ('chains', chains.list_chains(), chains.status),
                        ('enh', enhance.list_enhancements(), enhance.status),
                        ('virtual', virtual.list_devices(), virtual.status),
                        ('paths', paths.list_strips(), paths.status)):
                    total = len(items)
                    active = sum(1 for m in items
                                 if getattr(m, 'enabled', True)
                                 and status(m) == 'active')
                    inv[key] = (active, total)
                data['inventory'] = inv
            return data
        async_call(collect, self._apply)

    def _apply(self, data, error):
        self._busy = False
        if error or not data:
            return
        if 'states' in data:
            self._slow = {'states': data['states'],
                          'inventory': data['inventory']}
        states = self._slow.get('states', {})
        inventory = self._slow.get('inventory', {})

        for unit, (_row, p) in self.svc_rows.items():
            state = states.get(unit, 'unknown')
            p.set_label(state)
            for c in list(p.get_css_classes()):
                if c.startswith('pill-'):
                    p.remove_css_class(c)
            p.add_css_class(f'pill-{state_style(state)}')

        # -- status hero -------------------------------------------------
        bad = [label for unit, label in SERVICES
               if states.get(unit) not in ('active', None)]
        unknown = not states
        if unknown:
            tone, headline = 'dim', 'Checking services…'
        elif bad:
            tone = 'err'
            headline = (f'{bad[0]} is not running' if len(bad) == 1
                        else f'{len(bad)} audio services are not running')
        else:
            tone, headline = 'ok', 'Audio is running'
        for c in ('ok', 'warn', 'err', 'dim'):
            self.hero_dot.remove_css_class(c)
        self.hero_dot.add_css_class(tone)
        self.hero_title.set_label(headline)
        self.hero_sub.set_label(' · '.join(
            f'{label} {states.get(unit, "?")}' for unit, label in SERVICES))

        s = data['settings']
        drv = data['driver']
        rate = drv.get('rate') or int(s.get('clock.rate', '0') or 0)
        quantum = drv.get('quantum') or int(s.get('clock.quantum', '0') or 0)
        forced_r = int(s.get('clock.force-rate', '0') or 0)
        forced_q = int(s.get('clock.force-quantum', '0') or 0)
        if rate:
            self.clock_big.set_label(f'{rate / 1000:g} kHz · {quantum}')
        cycle = f'{quantum / rate * 1000:.2f} ms cycle' if rate and quantum else '—'
        forced = []
        if forced_r:
            forced.append('rate forced')
        if forced_q:
            forced.append('quantum forced')
        self.clock_sub.set_label(
            cycle + (('  ·  ' + ', '.join(forced)) if forced else '')
            + f'  ·  min {s.get("clock.min-quantum", "?")} / '
            f'max {s.get("clock.max-quantum", "?")}')

        if not self._calc_init and rate and quantum:
            self._calc_init = True
            self.calc_quantum.set_value(quantum)
            if rate in self.CALC_RATES:
                self.calc_rate.set_selected(self.CALC_RATES.index(rate))

        # -- alert -------------------------------------------------------
        if bad:
            self.alert_text.set_label(
                f'{", ".join(bad)} not running — audio will not work until '
                'the service is back.')
            self.alert_btn.set_label('Open Monitor')
            self.alert.set_visible(True)
        else:
            self.alert.set_visible(False)

        # -- default endpoints -------------------------------------------
        nodes = data['nodes']
        in_grace = self.local_grace
        for ep, want_sink in ((self.out_card, True), (self.in_card, False)):
            node = next((n for n in nodes
                         if n.is_sink == want_sink and n.is_default), None)
            ep.update(node, nodes, in_grace)
        self.fav.update(nodes)

        # -- playing now --------------------------------------------------
        # The app's own helper streams (a signal path's output, a chain's tap)
        # are real playback streams, but nobody thinks of them as "an app that
        # is playing" — the Running audio objects card is where they belong.
        # The mixer lists still show them: that is where you go to see
        # everything, and hiding them there would lose a control.
        def is_app(s2):
            return not (s2.props.get('node.name') or '').startswith('pwctl.')
        playing = [s2 for s2 in data['streams'] if s2.is_playback and is_app(s2)]
        recording = [s2 for s2 in data['streams']
                     if not s2.is_playback and is_app(s2)]
        self._fill_playing(playing, recording)

        # -- inventory ----------------------------------------------------
        for key, label in (('chains', 'chain'), ('enh', 'equalizer'),
                           ('virtual', 'device'), ('paths', 'path')):
            active, total = inventory.get(key, (0, 0))
            self.run_rows[key].set_label(f'{active} / {total}')
            self.run_rows[key].set_tooltip_text(
                f'{active} running of {total} configured')

        # -- the four mixer lists -----------------------------------------
        self.playback.update(data['streams'], nodes)
        self.recording.update(data['streams'], nodes)
        self.outputs.update(nodes, data['cards'])
        self.inputs.update(nodes, data['cards'])

    def _fill_playing(self, playing, recording):
        """Up to five current streams, newest membership wins.

        Rebuilt only when the set of stream ids changes — the same rule the
        mixer lists follow, because rebuilding under the pointer is how a page
        loses a click.
        """
        keys = [s.id for s in playing[:5]]
        if getattr(self, '_play_keys', None) != keys:
            self._play_keys = keys
            child = self.play_list.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self.play_list.remove(child)
                child = nxt
            self._play_widgets = {}
            for s in playing[:5]:
                line = Gtk.Box(spacing=10)
                line.add_css_class('dash-kv')
                icon = Gtk.Image.new_from_icon_name(_app_icon(s.icon))
                name = Gtk.Label(xalign=0, hexpand=True, max_width_chars=28,
                                 ellipsize=Pango.EllipsizeMode.END)
                value = Gtk.Label()
                value.add_css_class('numeric-value')
                value.add_css_class('dim-label')
                line.append(icon)
                line.append(name)
                line.append(value)
                self.play_list.append(line)
                self._play_widgets[s.id] = (name, value)
        for s in playing[:5]:
            widgets = self._play_widgets.get(s.id)
            if not widgets:
                continue
            name, value = widgets
            label = s.name if s.media in ('', s.name) else f'{s.name} — {s.media}'
            name.set_label(label)
            name.set_tooltip_text(label)
            value.set_label('muted' if s.muted else
                            f'{(s.volume or 0) * 100:.0f}%')
        extra = len(playing) - 5
        self.play_empty.set_visible(not playing)
        if not playing:
            self.play_empty.set_label(
                f'Nothing is playing.  {len(recording)} recording.'
                if recording else 'Nothing is playing.')
        elif extra > 0:
            self.play_empty.set_visible(True)
            self.play_empty.set_label(f'and {extra} more…')
