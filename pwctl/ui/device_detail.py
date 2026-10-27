"""The Devices page's detail pane: everything about the selected device.

A hero card says what the device is and does the frequent things (default,
test, volume, balance), then sections each say one thing about it, and a
section shows only when it applies (DR-012, "List and detail"):

* **Signal path**: what plays into it and where it goes, as chips.  An app
  chip moves that app (click, or drag it onto a row of the list); a device
  chip opens that device.
* **Connection** (a sound card) or **Bluetooth** (a headset): port, card
  profile with the Mixer's ⚠ and its one-click way back, codec, and the
  card's other endpoints.
* **Routing** (software): where it sends its sound, and the page that made it.
* **Name & visibility**, **Format & timing** (hardware): the WirePlumber
  rules, `device_settings`.
* **Technical details** (Advanced): the names and ids, each with a copy button.

Nothing here is rebuilt by a delivery: `paint()` updates the pane in place,
so an open popover, a name being typed or a slider being dragged survive
audio starting anywhere.  The page builds a new detail only when the
selection changes or the selected node is recreated (a new id).

A sound card that gives nothing in a direction gets a `CardOffDetail`, and
the Hidden devices row a `HiddenDetail`.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from ..backend import (balance, bluetooth, endpoints, pw, surround,  # noqa: E402
                       testsound)
from .device_settings import DeviceSettings, reset_device  # noqa: E402
from .volume import ChannelPanel, make_volume  # noqa: E402
from .widgets import (LOCAL_GRACE, async_call, device_icon,  # noqa: E402
                      drag_source, esc, micro, pill)

MAX_CHIPS = 4           # past this a group of chips says "+N"


def _now():
    return GLib.get_monotonic_time() / 1e6


def _app_icon(name):
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    if name and theme.has_icon(name):
        return name
    # Stands in for *an application*: never a glyph of ours, which would read
    # as "this goes through that".
    return 'application-x-executable-symbolic'


def _label(text='', css=(), xalign=0.0, chars=None, ellipsize=True):
    lbl = Gtk.Label(label=text, xalign=xalign)
    if ellipsize:
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
    if chars:
        lbl.set_max_width_chars(chars)
    for c in css:
        lbl.add_css_class(c)
    return lbl


def _clear(box):
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def _link_row(title, on_activate, subtitle=None, where=None):
    """A row that opens something: title, an optional word, and an arrow."""
    row = Adw.ActionRow(title=esc(title), activatable=True, title_lines=1)
    if subtitle:
        row.set_subtitle(esc(subtitle))
        row.set_subtitle_lines(1)
    if where:
        lbl = Gtk.Label(label=where, valign=Gtk.Align.CENTER)
        lbl.add_css_class('dim-label')
        row.add_suffix(lbl)
    row.add_suffix(Gtk.Image.new_from_icon_name('go-next-symbolic'))
    row.connect('activated', lambda _r: on_activate())
    return row


def _value_row(title, value, tip=None):
    """A row that only states something: 'Port   Speakers'."""
    row = Adw.ActionRow(title=esc(title), title_lines=1)
    lbl = Gtk.Label(label=value, valign=Gtk.Align.CENTER, xalign=1,
                    ellipsize=Pango.EllipsizeMode.END)
    lbl.add_css_class('dim-label')
    row.add_suffix(lbl)
    if tip:
        row.set_tooltip_text(tip)
    row.value = lbl
    return row


class Null:
    """Stands in for a control a list row does not have.

    `device_levels.sync_device_levels` sets volume and mute on every node it
    is given; only the selected node has real controls (in the detail), so
    the others answer with nothing.
    """

    def set_value(self, _value):
        pass

    def set_active(self, _value):
        pass


NULL = Null()


def blank_live(node):
    """The state `sync_device_levels` reads for one node of the list."""
    return {'volume': NULL, 'mute': NULL, 'star': lambda _d: None,
            'is_sink': node.is_sink, 'updating': False, 'local_ts': 0.0,
            'levels': None, 'panel': None}


class _Volume:
    """A make_volume control and its percentage, as one object.

    `device_levels.sync_device_levels` calls `set_value` on whatever sits in
    a node's `live['volume']`; going through this keeps the percentage in
    step with the slider on every delivery, not only on a drag.
    """

    def __init__(self, style, on_change):
        self._on_change = on_change
        self.ctl = make_volume(style, self._changed)
        self.ctl.widget.set_hexpand(True)
        # Not `numeric-value`: that is @accent_color, which a theme may make
        # grey, and dimmed on top it all but vanished (seen 2026-10-22).
        self.pct = Gtk.Label(width_chars=5, xalign=1)
        self.pct.add_css_class('dev-pct')

    @property
    def widget(self):
        return self.ctl.widget

    def _changed(self, value):
        self.pct.set_label(f'{value * 100:.0f}%')
        self._on_change(value)

    def set_value(self, value):
        self.ctl.set_value(value)
        self.pct.set_label(f'{value * 100:.0f}%')

    def set_meter(self, serial):
        self.ctl.set_meter(serial)

    def __getattr__(self, name):
        # Anything else is the control's own (its scale, its value), so this
        # stands in for a make_volume control wherever one is expected.
        return getattr(self.ctl, name)


class _ProfilePicker:
    """A card's profile row, its ⚠ and its one-click way back.

    Shared by an endpoint's detail and a card-is-off detail, so the rule for
    "stuck" is written once (`endpoints.profile_state`, the Mixer's rule).
    The card is addressed by **name** at click time and re-resolved from a
    fresh dump, because a PipeWire device id is reassigned when the device
    is recreated and `wpctl set-profile <stale id>` returns 0 and does
    nothing (CLAUDE.md gotcha).
    """

    def __init__(self, page, sink: bool, touch):
        self.page = page
        self.sink = sink
        self.touch = touch
        self.card = None
        self._key = None
        self._items = []
        self.updating = False
        self.problem = ''

        self.dropdown = Adw.ComboRow(title='Profile')
        self.dropdown.set_tooltip_text('Sound card profile: which outputs and '
                                       'inputs the card provides. It applies '
                                       'to the whole card.')
        self.dropdown.set_enable_search(True)
        self.dropdown.set_expression(Gtk.PropertyExpression.new(
            Gtk.StringObject, None, 'string'))
        self.dropdown.connect('notify::selected', self._on_selected)

        # Under it: either a warning (the active profile is unavailable) or
        # an offer (the card gives nothing in this direction now: Off, or a
        # playback-only Bluetooth profile seen from an input).  Text above
        # the button, never beside it: a profile name can be long enough to
        # squeeze the text to a word per line (seen 2026-10-22).
        self.warn = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                            margin_top=10, margin_bottom=10, margin_start=12,
                            margin_end=12)
        self.warn.add_css_class('dev-warn')
        top = Gtk.Box(spacing=8)
        self.warn_icon = Gtk.Image.new_from_icon_name(
            'dialog-warning-symbolic')
        self.warn_icon.set_valign(Gtk.Align.START)
        self.warn_text = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.warn_text.add_css_class('caption')
        top.append(self.warn_icon)
        top.append(self.warn_text)
        self.fix = Gtk.Button(halign=Gtk.Align.START)
        self.fix.add_css_class('dev-fix')
        self.fix.connect('clicked', self._on_fix)
        self.fix_label = Gtk.Label(wrap=True, xalign=0)
        self.fix.set_child(self.fix_label)
        self.warn.append(top)
        self.warn.append(self.fix)
        self.warn_row = Adw.PreferencesRow(activatable=False,
                                           child=self.warn)
        self.warn_row.add_css_class('devl-warn-row')
        self.warn_row.set_visible(False)
        self._top = top
        self._fix_index = None

    def update(self, card, in_grace=False):
        self.card = card
        self.updating = True
        try:
            items = [(idx, desc + (' (unavailable)' if avail == 'no' else ''))
                     for idx, desc, avail in card.profiles]
            self._items = items
            key = tuple(items)
            if key != self._key:
                self._key = key
                self.dropdown.set_model(
                    Gtk.StringList.new([d for _i, d in items]))
            if not in_grace:
                pos = next((i for i, (idx, _d) in enumerate(items)
                            if idx == card.active_profile), None)
                if pos is not None:
                    self.dropdown.set_selected(pos)
            state = endpoints.profile_state(card, self.sink)
            what = 'output' if self.sink else 'input'
            fix_name = (endpoints.profile_name(card, state.fix)
                        if state.fix is not None else '')
            bluetooth_card = card.name.startswith('bluez_card.')
            for css in ('dev-warn', 'dev-offer'):
                self.warn.remove_css_class(css)
            if state.unavailable:
                self.warn.add_css_class('dev-warn')
                self.warn_icon.set_visible(True)
                self.warn_text.set_label(
                    f'This profile is unavailable, so there is no {what} '
                    'until you switch.')
                self._top.set_visible(True)
                self.fix_label.set_label(f'Switch to {fix_name}')
            elif state.gives_nothing:
                # Not an error: the card is simply not giving this direction.
                self.warn.add_css_class('dev-offer')
                self.warn_icon.set_visible(False)
                note = ''
                if bluetooth_card and not self.sink:
                    # Measured nowhere here, but it is how Bluetooth works:
                    # HSP/HFP carries the mic at the cost of playback quality.
                    note = ('Bluetooth headsets play lower-quality sound '
                            'while their microphone is on.')
                self.warn_text.set_label(note)
                self._top.set_visible(bool(note))
                self.fix_label.set_label(f'Turn on the {what}: {fix_name}')
            self.warn_row.set_visible(
                (state.unavailable or state.gives_nothing)
                and (state.fix is not None or state.unavailable))
            self.fix.set_visible(state.fix is not None)
            self.fix.set_tooltip_text(
                f'Switch the card to {fix_name}, the best available profile '
                f'with an {what}' if fix_name else None)
            self._fix_index = state.fix
            # What the list row and the hero say instead.
            self.problem = (self.warn_text.get_label()
                            if state.unavailable else '')
        finally:
            self.updating = False

    def _on_selected(self, row, _pspec):
        if self.updating or self.card is None:
            return
        pos = row.get_selected()
        if pos == Gtk.INVALID_LIST_POSITION or pos >= len(self._items):
            return
        idx, label = self._items[pos]
        if idx == self.card.active_profile:
            return
        self._apply(idx, label.replace(' (unavailable)', ''))

    def _on_fix(self, _b):
        if self.card is not None and self._fix_index is not None:
            self._apply(self._fix_index,
                        endpoints.profile_name(self.card, self._fix_index))

    def _apply(self, index, label):
        self.touch()
        name, fallback = self.card.name, self.card.id
        window = self.page.window

        def work():
            card_id = fallback
            for card in surround.list_cards(outputs_only=False):
                if card.name == name:
                    card_id = card.id
                    break
            return surround.set_profile(card_id, index)

        async_call(work, lambda ok, e: (
            window.toast(f'Profile: {label}' if ok and not e
                         else 'Could not change the profile'),
            self.page.refresh()))


def _section(title, tip=None):
    # A group title is Pango markup (an '&' would blank it): escaped here.
    g = Adw.PreferencesGroup(title=esc(title))
    if tip:
        g.set_tooltip_text(tip)
    return g


def _pane():
    """A scrolling, clamped column: the frame every detail is built in."""
    column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                     margin_top=18, margin_bottom=40, margin_start=16,
                     margin_end=16)
    clamp = Adw.Clamp(maximum_size=680, tightening_threshold=520,
                      child=column)
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                            vexpand=True, child=clamp)
    sw.add_css_class('devl-detail')
    return sw, column


def _avatar(icon, css):
    img = Gtk.Image.new_from_icon_name(icon)
    img.set_pixel_size(22)
    img.add_css_class('dash-avatar')
    img.add_css_class('devl-avatar')
    img.add_css_class(css)
    img.set_valign(Gtk.Align.START)
    return img


class EndpointDetail:
    """One output or input, in full."""

    def __init__(self, page, node, rule, ctx, live, card_rule=None):
        self.page = page
        self.node = node
        self.key = (node.id, node.serial)
        self.sink = node.is_sink
        self.kind = endpoints.device_kind(node)
        self.is_hw = self.kind in ('alsa', 'bluez')
        self.style = page.volume_style
        self.rule = rule or {}
        self._chip_key = None
        self._sib_key = None
        self._sib_rows = []
        self._outs = []
        self._send_key = None
        self._codec_key = None
        self._codecs = []
        self._updating = False
        self._port_key = None
        self._ports = []
        self.live = None
        self.port_row = None
        self.sib_group = None

        self.widget, col = _pane()
        col.append(self._build_hero(node))
        col.append(self._build_path())
        card = endpoints.card_of(node, ctx['cards']) if self.is_hw else None
        self.card = card
        self.profile = None
        self.codec_row = None
        if self.kind == 'bluez':
            col.append(self._build_bluetooth(node, card))
        elif self.is_hw or card is not None:
            col.append(self._build_connection(node, card))
        if node.is_virtual:
            col.append(self._build_routing(node))
        self.settings = None
        if self.is_hw:
            self.settings = DeviceSettings(
                page, node, rule,
                (card.name, card.description) if card is not None else None,
                card_rule, bluetooth=self.kind == 'bluez')
            for g in self.settings.groups:
                col.append(g)
        col.append(self._build_details(node, card))
        self.bind_live(live)
        self.set_rule(rule)
        self.paint_star(node.is_default)
        self.paint(node, ctx)
        self.set_advanced(bool(getattr(page.window, 'advanced', False)))

    # -- live state the page's sync reads (device_levels) ------------------
    def bind_live(self, live):
        """Become the controls of this node's entry in `_live_controls`.

        Called again when the list is rebuilt around a detail that stays.
        """
        old = self.live
        self.live = live
        live['volume'] = self.vol
        live['mute'] = self.mute
        if old is not None:
            live['local_ts'] = max(live['local_ts'], old['local_ts'])
            live['levels'] = old['levels']
            live['panel'] = old['panel']
        else:
            live['levels'] = balance.Levels(self.node.channels,
                                            self.node.channel_map)
            live['panel'] = None

    def unbind(self):
        """Hand the controls back: the list row has none of its own.

        Only while they are still this detail's: a detail built again for
        the same node has already taken them."""
        if self.live is not None and self.live['volume'] is self.vol:
            self.live['volume'] = NULL
            self.live['mute'] = NULL
            self.live['levels'] = None
            self.live['panel'] = None

    def touch(self):
        self.live['local_ts'] = _now()

    @property
    def in_grace(self):
        return _now() - self.live['local_ts'] < LOCAL_GRACE

    # -- hero ----------------------------------------------------------------
    def _build_hero(self, node):
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        hero.add_css_class('devl-hero')
        hero.add_css_class('out' if self.sink else 'in')
        if node.is_virtual:
            hero.add_css_class('virtual')
        self.hero = hero

        top = Gtk.Box(spacing=12)
        top.append(_avatar(device_icon(node), 'out' if self.sink else 'in'))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3,
                        hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, wrap=True, lines=2,
                               ellipsize=Pango.EllipsizeMode.END)
        self.title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.title.add_css_class('devl-hero-name')
        names.append(self.title)
        self.node_name = Gtk.Label(xalign=0, selectable=True,
                                   ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.node_name.add_css_class('devl-node-name')
        names.append(self.node_name)
        top.append(names)
        more = Gtk.MenuButton(icon_name='view-more-symbolic',
                              valign=Gtk.Align.START, tooltip_text='More')
        more.add_css_class('flat')
        more.set_menu_model(self._menu(node))
        top.append(more)
        hero.append(top)

        pills = Adw.WrapBox(child_spacing=6, line_spacing=6)
        self.kind_pill = pill(endpoints.kind_text(node), 'dim')
        self.state_pill = pill('', 'dim')
        self.custom = pill('customized', 'warning')
        self.custom.set_tooltip_text('This device has settings of its own '
                                     '(Name & visibility, Format & timing)')
        self.hidden_pill = pill('hidden after restart', 'warning')
        self.hidden_pill.set_tooltip_text('Hidden by a rule: it disappears '
                                          'when WirePlumber restarts')
        self.problem_pill = pill('no sound: profile unavailable', 'warning')
        for p in (self.kind_pill, self.state_pill, self.custom,
                  self.hidden_pill, self.problem_pill):
            pills.append(p)
        self.problem_pill.set_visible(False)
        hero.append(pills)

        actions = Gtk.Box(spacing=8)
        self.star = Gtk.Button(valign=Gtk.Align.CENTER)
        self.star.connect('clicked', lambda _b: self.page.make_default(
            self.node))
        actions.append(self.star)
        self.test = None
        if self.sink:
            test = Gtk.Button(valign=Gtk.Align.CENTER)
            test.set_child(Adw.ButtonContent(
                icon_name='media-playback-start-symbolic', label='Test',
                can_shrink=True))
            test.add_css_class('flat')
            test.set_tooltip_text('Play a short chime here, left then right')
            test.connect('clicked', self._on_test)
            self.test = test
            actions.append(test)
        hero.append(actions)

        line = Gtk.Box(spacing=10)
        self.mute = Gtk.ToggleButton(icon_name='audio-volume-muted-symbolic',
                                     tooltip_text='Mute',
                                     valign=Gtk.Align.CENTER)
        self.mute.add_css_class('flat')
        self.mute.set_active(node.muted)
        self.mute.connect('toggled', self._on_mute)
        self.vol = _Volume(self.style, self._on_volume)
        self.vol.set_value(node.volume if node.volume is not None else 1.0)
        self.vol.set_meter(node.serial)
        line.append(self.mute)
        line.append(self.vol.widget)
        line.append(self.vol.pct)
        self.channels = None
        if len(node.channels) > 1:
            self.channels = Gtk.MenuButton(
                label='Channels', valign=Gtk.Align.CENTER,
                tooltip_text='Balance, and the level of each channel')
            self.channels.add_css_class('flat')
            self.channels.add_css_class('dev-small')
            pop = Gtk.Popover()
            self._chan_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                     spacing=8, margin_top=10,
                                     margin_bottom=10, margin_start=10,
                                     margin_end=10)
            self._chan_box.append(micro('Channels'))
            pop.set_child(self._chan_box)
            pop.connect('show', self._build_panel)
            self.channels.set_popover(pop)
            line.append(self.channels)
        hero.append(line)

        facts = Gtk.Box(spacing=8, homogeneous=True)
        facts.add_css_class('devl-facts')
        self.fact_values = []
        for key, _value in endpoints.facts(node):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            k = micro(key)
            k.add_css_class('devl-fact-key')
            v = _label('', css=('devl-fact-value',))
            box.append(k)
            box.append(v)
            facts.append(box)
            self.fact_values.append(v)
        facts.set_tooltip_text('The format the device is running at. Rate '
                               'and bit depth show while it runs.')
        hero.append(facts)
        return hero

    def _build_panel(self, *_a):
        # Built on first open, as the Mixer does: most are never asked, and
        # a panel is one volume control per channel.
        if self.live['panel'] is not None:
            if self.live['panel'].widget.get_parent() is None:
                self._chan_box.append(self.live['panel'].widget)
            return
        panel = ChannelPanel(self.live['levels'], self.style,
                             self._on_channels, compact=False)
        # 320, not the card's 440: it has to fit a 360 px window.
        panel.widget.set_size_request(320, -1)
        self.live['panel'] = panel
        self._chan_box.append(panel.widget)

    def _menu(self, node):
        group = Gio.SimpleActionGroup()

        def action(name, cb):
            act = Gio.SimpleAction.new(name, None)
            act.connect('activate', lambda *_a: cb())
            group.add_action(act)
            return act

        menu = Gio.Menu()
        owner = endpoints.owner(node, self.page.chain_templates)
        if owner is not None:
            page_name, page_title = owner
            action('owner', lambda: self.page.window.goto(page_name))
            menu.append(f'Edit in {page_title}', 'dev.owner')
        action('patchbay', lambda: self.page.window.goto('graph'))
        menu.append('Show in Patchbay', 'dev.patchbay')
        action('copy', self._copy_name)
        menu.append('Copy PipeWire name', 'dev.copy')
        if self.is_hw:
            self._reset_action = action(
                'reset', lambda: reset_device(self.page, self.node))
            section = Gio.Menu()
            section.append('Reset this device’s settings', 'dev.reset')
            menu.append_section(None, section)
        self.widget.insert_action_group('dev', group)
        return menu

    # -- sections -------------------------------------------------------------
    def _build_path(self):
        g = _section('Signal path', 'What plays into this device and where '
                     'its sound goes. Click an app to move it, or drag it '
                     'onto a device in the list; click a device to open it.')
        row = Adw.PreferencesRow(activatable=False)
        row.add_css_class('devl-path-row')
        self.path_box = Gtk.Box(margin_top=10, margin_bottom=10,
                                margin_start=12, margin_end=12)
        row.set_child(self.path_box)
        g.add(row)
        g.add(_link_row('Open in Patchbay',
                        lambda: self.page.window.goto('graph'),
                        where='Every link and port'))
        return g

    def _build_connection(self, node, card):
        g = _section('Connection')
        self._build_port(g, node)
        if card is not None:
            self._build_profile(g, card)
        self.sib_group = g
        return g

    def _build_bluetooth(self, node, card):
        g = _section('Bluetooth')
        if card is not None:
            self._build_profile(g, card)
        self.codec_row = Adw.ComboRow(title='Codec')
        self.codec_row.set_tooltip_text('How the sound is compressed on its '
                                        'way to the headset. Both ends must '
                                        'support it.')
        self.codec_row.connect('notify::selected', self._on_codec)
        self.codec_row.set_visible(False)
        g.add(self.codec_row)
        self.codec_text = _value_row('Codec', '', 'How the sound is '
                                     'compressed on its way to the headset')
        self.codec_text.set_visible(False)
        g.add(self.codec_text)
        self.sib_group = g
        return g

    def _build_port(self, g, node):
        self.port_row = None
        if len(node.ports) > 1:
            self.port_row = Adw.ComboRow(title='Port')
            self.port_row.set_tooltip_text('The connector this '
                                           f'{"output plays" if self.sink else "input records"} '
                                           'through')
            self.port_row.connect('notify::selected', self._on_port)
            g.add(self.port_row)
        elif node.ports:
            g.add(_value_row('Port', node.ports[0][1],
                             'The only connector on this device'))

    def _build_profile(self, g, card):
        if len(card.profiles) > 1:
            self.profile = _ProfilePicker(self.page, self.sink, self.touch)
            g.add(self.profile.dropdown)
            g.add(self.profile.warn_row)
        elif card.profiles:
            g.add(_value_row('Profile', card.profiles[0][1]))

    def _build_routing(self, node):
        g = _section('Routing')
        self.send_row = self.send_text = None
        if node.is_sink:
            self.send_row = Adw.ComboRow(title='Sends to')
            self.send_row.set_tooltip_text('Where this device plays what it '
                                           'is given')
            self.send_row.connect('notify::selected', self._on_send)
            self.send_row.set_visible(False)
            g.add(self.send_row)
            self.send_text = _value_row('Sends to', '')
            g.add(self.send_text)
        owner = endpoints.owner(node, self.page.chain_templates)
        if owner is not None:
            page_name, page_title = owner
            g.add(_link_row(f'Managed on {page_title}',
                            lambda: self.page.window.goto(page_name),
                            subtitle='Its settings live there'))
        else:
            g.add(_value_row('Made by', 'Another app or a config file',
                             'Not created on any page here: a hand-written '
                             'chain or another program’s device'))
        return g

    def _build_details(self, node, card):
        g = _section('Technical details')
        rows = [('node.name', node.name),
                ('Object id · serial', f'{node.id} · {node.serial}'),
                ('media.class', node.media_class),
                ('device.api', str((node.props or {}).get('device.api')
                                   or '(none)'))]
        if card is not None:
            rows.append(('Card', card.name))
        for title, value in rows:
            row = Adw.ActionRow(title=esc(title), subtitle=esc(value),
                                subtitle_selectable=True)
            row.add_css_class('property')
            copy = Gtk.Button(icon_name='edit-copy-symbolic',
                              valign=Gtk.Align.CENTER,
                              tooltip_text=f'Copy {title}')
            copy.add_css_class('flat')
            copy.connect('clicked', lambda _b, v=value: self._copy(v))
            row.add_suffix(copy)
            g.add(row)
        self.tech = g
        return g

    def set_advanced(self, on):
        self.tech.set_visible(on)
        if self.settings is not None:
            self.settings.set_advanced(on)

    # -- painting ------------------------------------------------------------
    def set_rule(self, rule):
        self.rule = rule or {}
        self.custom.set_visible(bool(self.rule))
        self.hidden_pill.set_visible(bool(self.rule.get('hide')))
        if hasattr(self, '_reset_action'):
            self._reset_action.set_enabled(bool(self.rule))

    def paint_star(self, is_default):
        """Called by the page, and through `live['star']` by the sync."""
        if getattr(self, '_star_state', None) == is_default:
            return
        self._star_state = is_default
        what = 'output' if self.sink else 'input'
        for css in ('dev-default', 'devl-make-default'):
            self.star.remove_css_class(css)
        if is_default:
            self.star.set_child(Adw.ButtonContent(
                icon_name='starred-symbolic', label=f'Default {what}',
                can_shrink=True))
            self.star.set_tooltip_text(f'The default {what}: apps '
                                       f'{"play" if self.sink else "record"} '
                                       'here unless told otherwise')
            self.star.add_css_class('dev-default')
            self.hero.add_css_class('is-default')
        else:
            self.star.set_child(Adw.ButtonContent(
                icon_name='non-starred-symbolic', label=f'Make default {what}',
                can_shrink=True))
            self.star.set_tooltip_text(f'Make this the default {what}')
            self.star.add_css_class('devl-make-default')
            self.hero.remove_css_class('is-default')

    def paint(self, node, ctx):
        """Everything a delivery can change besides level, mute and star."""
        self.node = node
        act = ctx['acts'].get(node.id) or endpoints.Activity()
        self.title.set_label(node.description)
        self.title.set_tooltip_text(node.description)
        self.node_name.set_label(node.name)
        self.node_name.set_tooltip_text(node.name)
        state = node.state
        text = endpoints.STATE_LABEL.get(state, '')
        for css in ('pill-success', 'pill-warning', 'pill-error', 'pill-dim'):
            self.state_pill.remove_css_class(css)
        self.state_pill.add_css_class({'running': 'pill-success',
                                       'creating': 'pill-warning',
                                       'error': 'pill-error'}.get(state,
                                                                  'pill-dim'))
        self.state_pill.set_label(text.lower())
        self.state_pill.set_visible(bool(text))
        for label, (_k, value) in zip(self.fact_values, endpoints.facts(node)):
            label.set_label(value)
        self.vol.set_meter(node.serial)
        in_grace = self.in_grace
        self._paint_port(node, in_grace)
        if self.profile is not None:
            card = endpoints.card_of(node, ctx['cards'])
            if card is not None:
                self.card = card
                self.profile.update(card, in_grace)
        problem = self.profile.problem if self.profile is not None else ''
        self.problem_pill.set_visible(bool(problem))
        self.problem_pill.set_tooltip_text(problem or None)
        if self.codec_row is not None:
            self._paint_codec(node)
        if node.is_virtual and node.is_sink:
            self._paint_sends(node, act, ctx)
        if self.page.dragging is None:      # never under the chip in flight
            self._paint_path(node, act, ctx)
        self._paint_siblings(node, ctx)

    def _paint_port(self, node, in_grace):
        if self.port_row is None:
            return
        key = tuple(node.ports)
        self._updating = True
        try:
            if key != self._port_key:
                self._port_key = key
                self._ports = [(idx, desc + (' (unplugged)'
                                             if avail == 'no' else ''))
                               for idx, desc, avail in node.ports]
                self.port_row.set_model(Gtk.StringList.new(
                    [d for _i, d in self._ports]))
            if not in_grace:
                pos = next((i for i, (idx, _d) in enumerate(self._ports)
                            if idx == node.active_port), None)
                if pos is not None:
                    self.port_row.set_selected(pos)
        finally:
            self._updating = False

    def _paint_siblings(self, node, ctx):
        """The card's other endpoints, as rows that open them."""
        group = self.sib_group
        if group is None or self.card is None:
            return
        sibs = [n for n in ctx['nodes']
                if n.id != node.id and endpoints.card_of(n, ctx['cards'])
                is not None and endpoints.card_of(n, ctx['cards']).id
                == self.card.id]
        sibs.sort(key=lambda n: (not n.is_sink, n.description))
        key = tuple((n.id, n.description, endpoints.port_name(n))
                    for n in sibs)
        if key == self._sib_key:
            return
        self._sib_key = key
        for row in self._sib_rows:
            group.remove(row)
        self._sib_rows = []
        for n in sibs:
            word = 'Output' if n.is_sink else 'Input'
            what = 'headset' if self.kind == 'bluez' else 'card'
            row = _link_row(f'{word} on this {what}',
                            lambda n=n: self.page.select_node(n),
                            subtitle=endpoints.port_name(n)
                            or endpoints.part_name(n, self.card))
            group.add(row)
            self._sib_rows.append(row)

    def _paint_codec(self, node):
        current = endpoints.codec(node)
        card = self.card
        if card is None:
            self.codec_text.value.set_label(current or '—')
            self.codec_text.set_visible(bool(current))
            return
        key = (card.name, card.active_profile)
        if key != self._codec_key:
            self._codec_key = key
            self.page.codecs(card.name, card.active_profile,
                             lambda codecs, k=key: self._codecs_loaded(k,
                                                                      codecs))
        self._select_codec(current)

    def _codecs_loaded(self, key, codecs):
        if key != self._codec_key:
            return                      # the profile moved on meanwhile
        self._codecs = list(codecs or [])
        self._updating = True
        try:
            self.codec_row.set_model(Gtk.StringList.new(
                [desc for _n, desc in self._codecs]))
        finally:
            self._updating = False
        self._select_codec(endpoints.codec(self.node))

    def _select_codec(self, current):
        names = [n.upper() for n, _d in self._codecs]
        has_list = len(self._codecs) > 1
        self.codec_row.set_visible(has_list)
        self.codec_text.value.set_label(current or '—')
        self.codec_text.set_visible(not has_list and bool(current))
        if has_list and current.upper() in names and not self.in_grace:
            self._updating = True
            try:
                self.codec_row.set_selected(names.index(current.upper()))
            finally:
                self._updating = False

    def _paint_sends(self, node, act, ctx):
        dests = act.sends
        movable = act.out_stream is not None
        outs = endpoints.output_choices(node, ctx['nodes'], acts=ctx['acts']) \
            if movable else []
        key = (tuple((d.id, d.description) if d is not None else None
                     for d in dests), act.out_stream, act.out_dest,
               tuple(n.id for n in outs))
        if key == self._send_key:
            return
        self._send_key = key
        self._act = act
        self._outs = outs
        self._updating = True
        try:
            if movable and outs:
                labels = [n.description for n in outs]
                if act.out_dest is None:
                    labels = ['Not connected'] + labels
                self.send_row.set_model(Gtk.StringList.new(labels))
                self._send_offset = 1 if act.out_dest is None else 0
                pos = next((i for i, n in enumerate(outs)
                            if n.id == act.out_dest), None)
                self.send_row.set_selected(
                    pos + self._send_offset if pos is not None else 0)
                self.send_row.set_visible(True)
                self.send_text.set_visible(False)
            else:
                names = [d.description for d in dests if d is not None]
                self.send_text.value.set_label(', '.join(names)
                                               or 'Not connected')
                self.send_text.set_visible(True)
                self.send_row.set_visible(False)
        finally:
            self._updating = False

    # -- the signal path ------------------------------------------------------
    def _paint_path(self, node, act, ctx):
        """Apps and feeding devices → this device → where it sends, and
        who records it.  Refilled only when what it shows has changed."""
        key = (node.description,
               tuple((s.id, s.name) for s in act.apps),
               tuple((n.id, n.description) for n in act.feeds),
               tuple((d.id, d.description) if d is not None else None
                     for d in act.sends),
               tuple((s.id, s.name) for s in act.monitors))
        if key == self._chip_key:
            return
        self._chip_key = key
        _clear(self.path_box)
        wrap = Adw.WrapBox(child_spacing=6, line_spacing=8, hexpand=True)
        self.path_wrap = wrap

        def arrow():
            img = Gtk.Image.new_from_icon_name('go-next-symbolic')
            img.add_css_class('devl-arrow')
            img.set_valign(Gtk.Align.CENTER)
            wrap.append(img)

        def some(items, empty):
            if not items:
                wrap.append(_label(empty, css=('caption', 'dim-label',
                                               'devl-path-empty')))
                return
            for widget in items[:MAX_CHIPS]:
                wrap.append(widget)
            if len(items) > MAX_CHIPS:
                rest = items[MAX_CHIPS:]
                more = _label(f'+{len(rest)}', css=('dev-chip', 'dev-more'))
                more.set_tooltip_text('\n'.join(w.get_tooltip_text() or ''
                                                for w in rest))
                wrap.append(more)

        if node.is_sink:
            before = [self._app_chip(s, False, ctx) for s in act.apps] + \
                     [self._device_chip(n, 'feed') for n in act.feeds]
            some(before, 'Nothing playing')
            arrow()
            wrap.append(self._self_chip(node))
            after = [self._device_chip(d, 'dest') if d is not None
                     else self._none_chip() for d in act.sends] + \
                    [self._app_chip(s, True, ctx, monitor=True)
                     for s in act.monitors]
            if after:
                arrow()
                some(after, '')
        else:
            if act.feeds:
                some([self._device_chip(n, 'feed') for n in act.feeds], '')
                arrow()
            wrap.append(self._self_chip(node))
            arrow()
            some([self._app_chip(s, True, ctx) for s in act.apps],
                 'Nothing recording')
        self.path_box.append(wrap)

    def _chip_content(self, icon, text, arrow=False, live=False):
        box = Gtk.Box(spacing=5)
        if live:
            dot = Gtk.Box(valign=Gtk.Align.CENTER)
            dot.add_css_class('dev-chip-dot')
            box.append(dot)
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(14)
        box.append(img)
        box.append(_label(text, chars=20))
        if arrow:
            box.append(Gtk.Image.new_from_icon_name('pan-down-symbolic'))
        return box

    def _self_chip(self, node):
        chip = Gtk.Box(valign=Gtk.Align.CENTER)
        chip.add_css_class('dev-chip')
        chip.add_css_class('devl-self')
        chip.add_css_class('out' if self.sink else 'in')
        chip.append(self._chip_content(device_icon(node), node.description))
        chip.set_tooltip_text('This device')
        return chip

    def _none_chip(self):
        chip = Gtk.Button(valign=Gtk.Align.CENTER, can_focus=False)
        chip.set_child(self._chip_content('action-unavailable-symbolic',
                                          'Not connected'))
        chip.add_css_class('dev-chip')
        chip.add_css_class('none')
        chip.set_tooltip_text('Not connected to anything')
        return chip

    def _device_chip(self, n, css):
        chip = Gtk.Button(valign=Gtk.Align.CENTER)
        chip.set_child(self._chip_content(device_icon(n), n.description))
        chip.add_css_class('dev-chip')
        chip.add_css_class(css)
        chip.set_tooltip_text(f'{n.description}: open it' if css == 'dest'
                              else f'{n.description} sends its sound here: '
                                   'open it')
        chip.connect('clicked', lambda _b: self.page.select_node(n))
        return chip

    def _app_chip(self, s, recording, ctx, monitor=False):
        chip = Gtk.MenuButton(valign=Gtk.Align.CENTER)
        chip.set_child(self._chip_content(_app_icon(s.icon), s.name,
                                          live=True))
        chip.add_css_class('dev-chip')
        chip.add_css_class('app')
        if monitor:
            chip.add_css_class('monitor')
            tip = f'{s.name} is recording what this output plays'
        else:
            tip = (f'{s.name}: {s.media}' if s.media and s.media != s.name
                   else s.name)
        chip.set_tooltip_text(tip + ' (click, or drag it onto a device in '
                              'the list, to move it)')
        chip.set_popover(self._move_popover(s, recording or monitor, ctx))
        drag_source(chip, lambda st=s: f'stream:{st.id}',
                    on_begin=lambda st=s: self.page.drag_started(st),
                    on_end=self.page.drag_ended,
                    dragging_css='dev-dragging')
        return chip

    def _list_popover(self, title, rows):
        """rows: [(label, icon, current, callback)]."""
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=8, margin_bottom=8, margin_start=8,
                      margin_end=8)
        box.append(micro(title))
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class('boxed-list')
        for text, icon, current, cb in rows:
            row = Adw.ActionRow(title=GLib.markup_escape_text(text),
                                activatable=not current, title_lines=1)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            if current:
                row.add_suffix(Gtk.Image.new_from_icon_name(
                    'object-select-symbolic'))
            else:
                row.connect('activated',
                            lambda _r, f=cb: (pop.popdown(), f()))
            lb.append(row)
        if not rows:
            box.append(_label('Nowhere else to send it', css=('dim-label',)))
        else:
            box.append(lb)
        pop.set_child(box)
        return pop

    def _move_popover(self, stream, recording, ctx):
        if recording:
            targets = [n for n in ctx['nodes'] if not n.is_sink] + \
                      [n for n in ctx['nodes'] if n.is_sink]
        else:
            targets = [n for n in ctx['nodes'] if n.is_sink]
        rows = []
        for n in targets:
            label = n.description
            if recording and n.is_sink:
                label = f'Monitor of {n.description}'
            rows.append((label, device_icon(n), n.id == stream.target_id,
                         lambda n=n: self.page.move_stream(stream, n)))
        return self._list_popover(f'Move {stream.name} to', rows)

    # -- handlers -------------------------------------------------------------
    def _write(self, values):
        self.touch()
        node = self.node
        nid, serial, mclass = node.id, node.serial, node.media_class
        async_call(lambda: pw.set_channel_volumes(
            nid, values, serial=serial, media_class=mclass))

    def _on_volume(self, value):
        # scales every channel, so a balance survives the master
        self._write(self.live['levels'].set_master(value))
        if self.live['panel'] is not None:
            self.live['panel'].refresh()

    def _on_channels(self, values):
        self._write(values)
        master = self.live['levels'].master
        if master is not None:
            self.vol.set_value(master)

    def _on_mute(self, btn):
        if self.live is None or self.live['updating']:
            return
        self.touch()
        active, nid = btn.get_active(), self.node.id
        async_call(lambda: pw.set_mute(nid, active))

    def _on_port(self, row, _pspec):
        if self._updating:
            return
        pos = row.get_selected()
        if pos == Gtk.INVALID_LIST_POSITION or pos >= len(self._ports):
            return
        idx, label = self._ports[pos]
        if idx == self.node.active_port:
            return
        self.touch()
        nid, window = self.node.id, self.page.window
        label = label.replace(' (unplugged)', '')
        async_call(lambda: pw.set_route(nid, idx),
                   lambda ok, e: window.toast(
                       f'{label}' if ok and not e
                       else 'Could not switch the port'))

    def _on_codec(self, row, _pspec):
        if self._updating or self.card is None:
            return
        pos = row.get_selected()
        if pos == Gtk.INVALID_LIST_POSITION or pos >= len(self._codecs):
            return
        name, desc = self._codecs[pos]
        if name.upper() == endpoints.codec(self.node).upper():
            return
        self.touch()
        card_name, window, page = self.card.name, self.page.window, self.page

        def done(result, error):
            ok, err = result if result else (False, str(error))
            window.toast(f'Codec: {desc}' if ok else
                         f'Could not switch the codec{": " + err if err else ""}')
            page.refresh()
        async_call(lambda: bluetooth.switch_codec(card_name, name), done)

    def _on_send(self, row, _pspec):
        if self._updating:
            return
        pos = row.get_selected() - getattr(self, '_send_offset', 0)
        if pos < 0 or pos >= len(self._outs):
            return
        target = self._outs[pos]
        act = self._act
        if target.id == act.out_dest or act.out_stream is None:
            return
        self.page.repoint(self.node, act.out_stream, target)

    def _on_test(self, btn):
        if not testsound.available():
            self.page.window.toast(testsound.MISSING, timeout=6)
            return
        if self.node.muted:
            self.page.window.toast('This output is muted: unmute it to '
                                   'hear the test')
        btn.set_sensitive(False)
        serial, window = self.node.serial, self.page.window

        def done(result, error):
            btn.set_sensitive(True)
            ok, message = result if result else (False, str(error))
            if not ok:
                window.toast(message, timeout=6)
        async_call(lambda: testsound.play(serial), done)

    def _copy(self, value):
        Gdk.Display.get_default().get_clipboard().set(value)
        self.page.window.toast(f'Copied {value}')

    def _copy_name(self):
        self._copy(self.node.name)


class CardOffDetail:
    """A sound card that could give this direction but gives nothing now.

    Off, or an output-only profile seen from an input.  Without this the
    card would simply be missing from the page, and with it the only way
    back.
    """

    def __init__(self, page, card, sink: bool):
        self.page = page
        self.sink = sink
        self.key = ('card', card.id, sink)
        self.live = {'local_ts': 0.0}
        what = 'output' if sink else 'input'

        self.widget, col = _pane()
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        hero.add_css_class('devl-hero')
        hero.add_css_class('off')
        top = Gtk.Box(spacing=12)
        top.append(_avatar('pwctl-devices-symbolic', 'off'))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3,
                        hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, wrap=True, lines=2,
                               ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class('devl-hero-name')
        names.append(self.title)
        names.append(_label(f'No {what} on this profile',
                            css=('caption', 'dim-label')))
        top.append(names)
        hero.append(top)
        pills = Gtk.Box(spacing=6)
        pills.append(pill('off', 'dim'))
        hero.append(pills)
        col.append(hero)

        g = _section('Connection')
        self.profile = _ProfilePicker(page, sink, self.touch)
        g.add(self.profile.dropdown)
        g.add(self.profile.warn_row)
        col.append(g)
        self.paint(card)

    def touch(self):
        self.live['local_ts'] = _now()

    def paint(self, card, _ctx=None):
        self.title.set_label(card.description)
        self.profile.update(card, _now() - self.live['local_ts'] < LOCAL_GRACE)

    def unbind(self):
        pass

    def set_advanced(self, _on):
        pass


class HiddenDetail:
    """Every hidden endpoint and card, with a way back for each."""

    def __init__(self, page, entries, live_nodes, live_cards):
        self.page = page
        self.key = 'hidden'
        self.widget, col = _pane()
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        title = _label('Hidden devices', css=('devl-hero-name',))
        head.append(title)
        note = Gtk.Label(
            xalign=0, wrap=True,
            label='A hidden device is gone from the audio graph, so this is '
                  'the only place it is listed. Unhiding takes effect after '
                  'WirePlumber restarts.')
        note.add_css_class('dim-label')
        head.append(note)
        col.append(head)

        g = Adw.PreferencesGroup()
        if not entries:
            g.add(Adw.ActionRow(title='Nothing is hidden'))
        for entry in entries:
            live = entry['key'] in (live_cards if entry['kind'] == 'device'
                                    else live_nodes)
            row = Adw.ActionRow(title=esc(entry['label']),
                                subtitle=esc(entry['key']),
                                title_lines=1, subtitle_lines=1)
            row.add_prefix(Gtk.Image.new_from_icon_name(
                'view-conceal-symbolic'))
            if entry['kind'] == 'device':
                row.add_suffix(pill('whole card', 'dim'))
            if live:
                # Still in the graph while marked hidden: the generated
                # WirePlumber drop-in was removed or edited by hand.
                row.add_suffix(pill('not in effect', 'warning'))
            btn = Gtk.Button(label='Unhide', valign=Gtk.Align.CENTER)
            btn.connect('clicked', lambda _b, e=entry: page.unhide(e))
            row.add_suffix(btn)
            g.add(row)
        col.append(g)

    def paint(self, *_a):
        pass

    def unbind(self):
        pass

    def set_advanced(self, _on):
        pass
