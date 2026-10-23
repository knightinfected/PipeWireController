"""One card per audio endpoint, for the Devices page.

A card shows the device itself rather than a list of control names (UI.md
§1): what it is and where the sound physically is, its state and running
format, its level, the port and profile that decide what you hear, what is
playing into it, and where a software device sends its sound.  The cheapest
gesture does the most frequent thing: the star makes it the default, a port
is one click, an app chip moves that app.

Two rules shape the layout, both learned on this page:

* **A card keeps its height while audio comes and goes.**  Cards sit in
  `ColumnBox` newspaper columns, so a card that grows re-balances the
  columns and can move the card you just clicked to the other column.  So
  the Playing row is always there ("Nothing playing" when idle), and the
  channel panel opens in a popover instead of unfolding inside the card.
* **Nothing here is rebuilt by a delivery.**  The page rebuilds cards only
  when the set of endpoints changes; every other change is painted in place
  by `paint()`, so a level meter is never respawned and an open popover is
  never closed by audio starting somewhere.

A card folds to its head and level (the chevron, or a click on its name):
everything under the volume line is in a revealer.  The head carries what
must stay reachable folded: the default star, the settings gear, the format,
and a ⚠ when the folded part holds a profile problem.  Folding is the one
height change a card makes on purpose, so the page's `ColumnBox` keeps each
card in its column (`keep_columns`) rather than re-balancing under the
pointer.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from ..backend import balance, endpoints, pw, surround, testsound  # noqa: E402
from .volume import ChannelPanel, make_volume  # noqa: E402
from .widgets import (LOCAL_GRACE, async_call, device_icon,  # noqa: E402
                      drag_source, drop_target, micro, pill)

MAX_CHIPS = 4           # past this a row says "+N" instead of wrapping


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


class _Volume:
    """A make_volume control and its percentage, as one object.

    `device_levels.sync_device_levels` calls `set_value` on whatever sits in
    a card's `live['volume']`; going through this keeps the percentage in
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
    """A card's profile dropdown, its ⚠ and its one-click way back.

    Shared by a device card and a card-is-off card, so the rule for "stuck"
    is written once (`endpoints.profile_state`, the Mixer's rule).  The card
    is addressed by **name** at click time and re-resolved from a fresh dump,
    because a PipeWire device id is reassigned when the device is recreated
    and `wpctl set-profile <stale id>` returns 0 and does nothing (CLAUDE.md
    gotcha).
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

        self.dropdown = Gtk.DropDown(valign=Gtk.Align.CENTER, hexpand=True,
                                     tooltip_text='Sound card profile: which '
                                     'outputs and inputs the card provides')
        self.dropdown.set_enable_search(True)
        self.dropdown.set_expression(Gtk.PropertyExpression.new(
            Gtk.StringObject, None, 'string'))
        self.dropdown.connect('notify::selected', self._on_selected)

        # Under the dropdown: either a warning (the active profile is
        # unavailable) or an offer (the card gives nothing in this direction
        # now: Off, or a playback-only Bluetooth profile seen from Inputs).
        # Text above the button, never beside it: a profile name can be long
        # enough to squeeze the text to a word per line (seen 2026-10-22).
        self.warn = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
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
        self.warn.set_visible(False)
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
            bluetooth = card.name.startswith('bluez_card.')
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
                if bluetooth and not self.sink:
                    # Measured nowhere here, but it is how Bluetooth works:
                    # HSP/HFP carries the mic at the cost of playback quality.
                    note = ('Bluetooth headsets play lower-quality sound '
                            'while their microphone is on.')
                self.warn_text.set_label(note)
                self._top.set_visible(bool(note))
                self.fix_label.set_label(f'Turn on the {what}: {fix_name}')
            self.warn.set_visible((state.unavailable or state.gives_nothing)
                                  and (state.fix is not None
                                       or state.unavailable))
            self.fix.set_visible(state.fix is not None)
            self.fix.set_tooltip_text(
                f'Switch the card to {fix_name}, the best available profile '
                f'with an {what}' if fix_name else None)
            self._fix_index = state.fix
            # What a folded card says in its head instead (_paint_alert).
            self.problem = (self.warn_text.get_label()
                            if state.unavailable else '')
        finally:
            self.updating = False

    def _on_selected(self, dd, _pspec):
        if self.updating or self.card is None:
            return
        pos = dd.get_selected()
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


class DeviceCard:
    """One output or input."""

    def __init__(self, page, node, rule, ctx, folded=False):
        self.page = page
        self.node = node
        self.key = (node.id, node.serial)
        self.sink = node.is_sink
        self.is_hw = node.name.startswith(('alsa_', 'bluez_'))
        self.style = page.volume_style
        self.filter_text = ''
        self.folded = False
        self._chip_keys = {}
        self._outs = []
        self._tags_key = None

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                              spacing=12, valign=Gtk.Align.START)
        self.widget.add_css_class('dev-card')
        self.widget.add_css_class('out' if self.sink else 'in')
        if node.is_virtual:
            self.widget.add_css_class('virtual')

        self._build_head(node)
        # The level, then everything that folds away under it.  One box with
        # no spacing, so a folded card has no gap left where the rest was:
        # the space above the details is the revealer child's own margin.
        lower = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.widget.append(lower)
        self._lower = lower
        self._build_volume(node)
        self._rest = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                             margin_top=12)
        self.reveal = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=200, child=self._rest)
        lower.append(self.reveal)
        self._build_details(node, ctx)
        self._build_foot(node)
        # Before the first frame, so a card built folded never animates.
        self.set_folded(folded)
        # An app chip dragged from another card lands here: the move the
        # chip's own menu makes, as a gesture.
        drop_target(self.widget, GObject.TYPE_STRING,
                    lambda value, _x, _y: self.page.drop_on(self, value),
                    on_motion=lambda value, _x, _y: self._hover(value),
                    on_leave=lambda: self.widget.remove_css_class(
                        'drop-into'))
        self.set_rule(rule)
        self.paint_star(node.is_default)
        self.paint(node, ctx)

    # -- live state the page's sync reads (device_levels) ------------------
    def touch(self):
        self.live['local_ts'] = _now()

    @property
    def in_grace(self):
        return _now() - self.live['local_ts'] < LOCAL_GRACE

    # -- building ----------------------------------------------------------
    def _build_head(self, node):
        head = Gtk.Box(spacing=12)
        avatar = Gtk.Image.new_from_icon_name(device_icon(node))
        avatar.add_css_class('dash-avatar')
        avatar.add_css_class('out' if self.sink else 'in')
        avatar.set_valign(Gtk.Align.CENTER)
        head.append(avatar)

        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3,
                        hexpand=True, valign=Gtk.Align.CENTER)
        self.title = _label(css=('dev-title',), chars=28)
        names.append(self.title)
        sub = Gtk.Box(spacing=6)
        self.subtitle = _label(css=('caption', 'dim-label'), chars=34)
        self.custom = pill('customized', 'warning')
        # Shrinks like the subtitle beside it, so it never sets the card's
        # minimum width (DR-013's 360 px window).
        self.custom.set_ellipsize(Pango.EllipsizeMode.END)
        self.custom.set_tooltip_text('This device has settings of its own '
                                     '(the gear)')
        # A folded card's stand-in for the profile warning it hides.
        self.alert = Gtk.Image.new_from_icon_name('dialog-warning-symbolic')
        self.alert.add_css_class('dev-alert')
        self.alert.set_visible(False)
        sub.append(self.subtitle)
        sub.append(self.custom)
        sub.append(self.alert)
        names.append(sub)

        # State and format: "● Running  48 kHz  24-bit  Stereo".  Always one
        # line (the tags ellipsize rather than wrap), so the head is the same
        # height whether or not the device is playing.
        spec = Gtk.Box(spacing=6)
        spec.add_css_class('dev-spec-line')
        # A box, not a label: a label is a whole text line tall, which drew
        # the dot as a bar.
        self.dot = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.dot.add_css_class('dev-dot')
        self.state = _label(css=('caption', 'dev-state'))
        self.tags = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        spec.append(self.dot)
        spec.append(self.state)
        spec.append(self.tags)
        names.append(spec)
        head.append(names)
        # A click on the name folds or unfolds, as on an expander row.
        click = Gtk.GestureClick()
        click.connect('released', lambda g, n, _x, _y: (
            self.page.fold_card(self, not self.folded) if n == 1 else None))
        names.add_controller(click)
        names.set_cursor_from_name('pointer')

        buttons = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
        self.star = Gtk.Button(valign=Gtk.Align.CENTER)
        self.star.connect('clicked', lambda _b: self.page.make_default(self))
        buttons.append(self.star)
        # Up here, not in the foot, so a folded card still has it.
        self.gear = None
        if self.is_hw:
            self.gear = Gtk.Button(icon_name='emblem-system-symbolic',
                                   valign=Gtk.Align.CENTER,
                                   tooltip_text='Device settings: name, '
                                                'format, buffering, hiding')
            self.gear.add_css_class('flat')
            self.gear.add_css_class('dev-icon')
            self.gear.connect('clicked',
                              lambda _b: self.page.open_settings(self))
            buttons.append(self.gear)
        self.chevron = Gtk.Button(valign=Gtk.Align.CENTER)
        self.chevron.add_css_class('flat')
        self.chevron.add_css_class('dev-icon')
        arrow = Gtk.Image.new_from_icon_name('pan-down-symbolic')
        arrow.add_css_class('dev-chevron')
        self.chevron.set_child(arrow)
        self.chevron.connect('clicked', lambda _b: self.page.fold_card(
            self, not self.folded))
        buttons.append(self.chevron)
        head.append(buttons)
        self.widget.append(head)

    def set_folded(self, folded):
        """Fold to the head and level, or unfold.  Animated only when the
        card is on screen; GTK skips the slide by itself with animations
        off or reduced motion (measured, GTK 4.24)."""
        self.folded = bool(folded)
        self.reveal.set_reveal_child(not self.folded)
        if self.folded:
            self.widget.add_css_class('folded')
        else:
            self.widget.remove_css_class('folded')
        self.chevron.set_tooltip_text('Expand' if self.folded else 'Collapse')
        self.chevron.update_state([Gtk.AccessibleState.EXPANDED],
                                  [int(not self.folded)])
        self._paint_alert()

    def _paint_alert(self):
        problem = self.profile.problem if self.profile is not None else ''
        self.alert.set_visible(self.folded and bool(problem))
        self.alert.set_tooltip_text(
            f'{problem} Expand the card to fix it.' if problem else None)

    def _build_volume(self, node):
        self.live = {
            'volume': None, 'mute': None, 'star': self.paint_star,
            'is_sink': self.sink, 'updating': False, 'local_ts': 0.0,
            'levels': balance.Levels(node.channels, node.channel_map),
            'panel': None,
        }
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
        self.live['volume'] = self.vol
        self.live['mute'] = self.mute
        self._lower.append(line)

    def _build_panel(self, *_a):
        # Built on first open, as the Mixer does: most cards are never asked,
        # and a panel is one volume control per channel.
        if self.live['panel'] is not None:
            return
        panel = ChannelPanel(self.live['levels'], self.style,
                             self._on_channels, compact=False)
        panel.widget.set_size_request(440, -1)
        self.live['panel'] = panel
        self._chan_box.append(panel.widget)

    def _row(self, title):
        """A labelled detail row; returns its value box."""
        lbl = micro(title)
        lbl.add_css_class('dev-row-label')
        lbl.set_valign(Gtk.Align.CENTER)
        self.page.label_group.add_widget(lbl)
        value = Gtk.Box(spacing=6, hexpand=True, valign=Gtk.Align.CENTER)
        row = Gtk.Box(spacing=12)
        row.append(lbl)
        row.append(value)
        self.details.append(row)
        return row, value

    def _build_details(self, node, ctx):
        self.details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                               spacing=8)
        self.details.add_css_class('dev-details')

        # Port: a choice only when there is more than one.  A single port is
        # already named in the subtitle.
        self.port_row = self.port_toggles = self.port_dd = None
        self._port_key = None
        self._ports = []
        if len(node.ports) > 1:
            self.port_row, value = self._row(
                'Output' if self.sink else 'Input')
            if len(node.ports) <= 3:
                self.port_toggles = Adw.ToggleGroup(can_shrink=True)
                self.port_toggles.add_css_class('dev-ports')
                self.port_toggles.connect('notify::active-name',
                                          self._on_port_toggle)
                value.append(self.port_toggles)
            else:
                self.port_dd = Gtk.DropDown(hexpand=True)
                self.port_dd.connect('notify::selected', self._on_port_dd)
                value.append(self.port_dd)

        # Profile: per sound card, when it has a choice.
        self.profile = None
        card = endpoints.card_of(node, ctx['cards']) if self.is_hw else None
        if card is not None and len(card.profiles) > 1:
            _row, value = self._row('Profile')
            self.profile = _ProfilePicker(self.page, self.sink, self.touch)
            value.append(self.profile.dropdown)
            self.details.append(self.profile.warn)

        # Where a software device's sound goes.
        self.sends_row = None
        if node.is_virtual and node.is_sink:
            self.sends_row, self.sends_box = self._row('Sends to')

        # What feeds it (software devices pointed here).
        self.from_row, self.from_box = self._row('From')

        # What is playing / recording.  Always present: see the module doc.
        self.apps_row, self.apps_box = self._row(
            'Playing' if self.sink else 'Recording')
        self._rest.append(self.details)

    def _build_foot(self, node):
        foot = Gtk.Box(spacing=6)
        foot.add_css_class('dev-foot')
        # The format moved up into the head (the spec line); the foot is
        # the card's actions, at the end.
        foot.append(Gtk.Box(hexpand=True))

        if self.sink:
            test = Gtk.Button(valign=Gtk.Align.CENTER)
            test.set_child(Adw.ButtonContent(
                icon_name='media-playback-start-symbolic', label='Test',
                can_shrink=True))
            test.add_css_class('flat')
            test.add_css_class('dev-small')
            test.set_tooltip_text('Play a short chime here, left then right')
            test.connect('clicked', self._on_test)
            self.test = test
            foot.append(test)

        more = Gtk.MenuButton(icon_name='view-more-symbolic',
                              valign=Gtk.Align.CENTER,
                              tooltip_text='More')
        more.add_css_class('flat')
        more.set_menu_model(self._menu(node))
        foot.append(more)
        self._rest.append(foot)

    def _menu(self, node):
        group = Gio.SimpleActionGroup()

        def action(name, cb):
            act = Gio.SimpleAction.new(name, None)
            act.connect('activate', lambda *_a: cb())
            group.add_action(act)

        menu = Gio.Menu()
        owner = endpoints.owner(node, self.page.chain_templates)
        if owner is not None:
            page_name, page_title = owner
            action('owner', lambda: self.page.window.goto(page_name))
            menu.append(f'Edit in {page_title}', 'dev.owner')
        if self.is_hw:
            action('settings', lambda: self.page.open_settings(self))
            menu.append('Rename or hide…', 'dev.settings')
        action('patchbay', lambda: self.page.window.goto('graph'))
        menu.append('Show in Patchbay', 'dev.patchbay')
        action('copy', self._copy_name)
        menu.append('Copy PipeWire name', 'dev.copy')
        self.widget.insert_action_group('dev', group)
        return menu

    def _hover(self, value):
        ok = self.page.accepts(self, value)
        if ok:
            self.widget.add_css_class('drop-into')
        return ok

    # -- painting ----------------------------------------------------------
    def set_rule(self, rule):
        self.rule = rule or {}
        self.custom.set_visible(bool(self.rule))

    def paint_star(self, is_default):
        """Called by the page and by `sync_device_levels`."""
        if getattr(self, '_star_state', None) == is_default:
            return
        self._star_state = is_default
        what = 'output' if self.sink else 'input'
        if is_default:
            self.star.set_child(Adw.ButtonContent(
                icon_name='starred-symbolic', label='Default',
                can_shrink=True))
            self.star.set_tooltip_text(f'The default {what}: apps play here '
                                       'unless told otherwise')
            self.star.remove_css_class('flat')
            self.star.remove_css_class('dev-icon')
            self.star.add_css_class('dev-default')
            self.widget.add_css_class('is-default')
        else:
            self.star.set_child(None)
            self.star.set_icon_name('non-starred-symbolic')
            self.star.set_tooltip_text(f'Make this the default {what}')
            self.star.remove_css_class('dev-default')
            self.star.add_css_class('flat')
            self.star.add_css_class('dev-icon')
            self.widget.remove_css_class('is-default')

    def paint(self, node, ctx):
        """Everything a delivery can change besides level, mute and star."""
        self.node = node
        acts = ctx['acts']
        act = acts.get(node.id) or endpoints.Activity()
        self.title.set_label(node.description)
        self.title.set_tooltip_text(node.description)
        self.subtitle.set_label(endpoints.subtitle(node))
        for css in ('ok', 'warn', 'err', 'dim'):
            self.dot.remove_css_class(css)
        self.dot.add_css_class(endpoints.state_dot(node))
        self.state.set_label(endpoints.STATE_LABEL.get(node.state, ''))
        self._paint_tags(endpoints.format_tags(node), node.state == 'running')
        self.vol.set_meter(node.serial)
        # The star is not painted here: `sync_device_levels` owns it, and
        # holds it after a click until the graph has caught up.
        in_grace = self.in_grace
        self._paint_ports(node, in_grace)
        if self.profile is not None:
            card = endpoints.card_of(node, ctx['cards'])
            if card is not None:
                self.profile.update(card, in_grace)
        self._paint_alert()
        if self.sends_row is not None:
            self._paint_sends(node, act, ctx)
        self._paint_from(act)
        if self.page.dragging is None:      # never under the chip in flight
            self._paint_apps(act, ctx)
        self.filter_text = ' '.join((
            node.description, node.name, endpoints.subtitle(node))).lower()

    def _paint_tags(self, tags, running):
        """The format tags, refilled only when they change."""
        key = (tuple(tags), running)
        if key == self._tags_key:
            return
        self._tags_key = key
        _clear(self.tags)
        for text in tags:
            tag = _label(text, css=('dev-spec',))
            if running:
                tag.add_css_class('running')
            self.tags.append(tag)
        self.tags.set_tooltip_text(
            'The format the device is running at' if running else
            'Channel layout. Rate and bit depth show while the device runs')

    def spec_text(self):
        """'Running · 48 kHz · 24-bit · Stereo', as drawn (for harnesses)."""
        parts = [self.state.get_label()]
        child = self.tags.get_first_child()
        while child is not None:
            parts.append(child.get_label())
            child = child.get_next_sibling()
        return ' · '.join(p for p in parts if p)

    def _paint_ports(self, node, in_grace):
        if self.port_row is None:
            return
        key = tuple(node.ports)
        self._updating_port = True
        try:
            if self.port_toggles is not None:
                if key != self._port_key:
                    self._port_key = key
                    self.port_toggles.remove_all()
                    for idx, desc, avail in node.ports:
                        t = Adw.Toggle(name=str(idx), label=desc)
                        if avail == 'no':
                            t.set_tooltip('Unplugged')
                            t.set_enabled(idx == node.active_port)
                        self.port_toggles.add(t)
                if not in_grace and node.active_port is not None:
                    self.port_toggles.set_active_name(str(node.active_port))
            else:
                if key != self._port_key:
                    self._port_key = key
                    self._ports = [(idx, desc + (' (unplugged)'
                                                 if avail == 'no' else ''))
                                   for idx, desc, avail in node.ports]
                    self.port_dd.set_model(Gtk.StringList.new(
                        [d for _i, d in self._ports]))
                if not in_grace:
                    pos = next((i for i, (idx, _d) in enumerate(self._ports)
                                if idx == node.active_port), None)
                    if pos is not None:
                        self.port_dd.set_selected(pos)
        finally:
            self._updating_port = False

    def _chips(self, box, key, items, empty):
        """Refill a row's chips only when what they show has changed."""
        if self._chip_keys.get(id(box)) == key:
            return
        self._chip_keys[id(box)] = key
        _clear(box)
        wrap = Adw.WrapBox(child_spacing=6, line_spacing=6, hexpand=True)
        if not items:
            wrap.append(_label(empty, css=('caption', 'dim-label')))
        for widget in items[:MAX_CHIPS]:
            wrap.append(widget)
        if len(items) > MAX_CHIPS:
            rest = items[MAX_CHIPS:]
            more = _label(f'+{len(rest)}', css=('dev-chip', 'dev-more'))
            more.set_tooltip_text('\n'.join(w.get_tooltip_text() or ''
                                            for w in rest))
            wrap.append(more)
        box.append(wrap)

    def _paint_sends(self, node, act, ctx):
        dests = act.sends
        key = (tuple((d.id, d.description) if d is not None else None
                     for d in dests), act.out_stream)
        if self._chip_keys.get(id(self.sends_box)) == key:
            return
        movable = act.out_stream is not None
        self._outs = endpoints.output_choices(node, ctx['nodes'], acts=ctx['acts']) \
            if movable else []
        chips = []
        for dest in dests or [None]:
            text = dest.description if dest is not None else 'Not connected'
            icon = device_icon(dest) if dest is not None else 'action-unavailable-symbolic'
            if movable:
                chip = Gtk.MenuButton(valign=Gtk.Align.CENTER)
                chip.set_child(self._chip_content(icon, text, arrow=True))
                chip.set_popover(self._output_popover(act))
                chip.set_tooltip_text(f'{text}: click to send it somewhere '
                                      'else')
            else:
                chip = Gtk.Button(valign=Gtk.Align.CENTER, can_focus=False)
                chip.set_child(self._chip_content(icon, text))
                chip.set_tooltip_text(text)
            chip.add_css_class('dev-chip')
            chip.add_css_class('dest' if dest is not None else 'none')
            chips.append(chip)
        self._chips(self.sends_box, key, chips, 'Not connected')

    def _paint_from(self, act):
        feeds = act.feeds
        self.from_row.set_visible(bool(feeds))
        key = tuple((n.id, n.description) for n in feeds)
        chips = []
        for n in feeds:
            chip = Gtk.Button(valign=Gtk.Align.CENTER)
            chip.set_child(self._chip_content(device_icon(n), n.description))
            chip.add_css_class('dev-chip')
            chip.add_css_class('feed')
            chip.set_tooltip_text(f'{n.description} sends its sound here')
            chips.append(chip)
        self._chips(self.from_box, key, chips, '')

    def _paint_apps(self, act, ctx):
        streams = list(act.apps) + list(act.monitors)
        key = tuple((s.id, s.name, s in act.monitors) for s in streams)
        if self._chip_keys.get(id(self.apps_box)) == key:
            return
        chips = []
        for s in streams:
            recording = s in act.monitors or not s.is_playback
            chip = Gtk.MenuButton(valign=Gtk.Align.CENTER)
            chip.set_child(self._chip_content(_app_icon(s.icon), s.name,
                                              live=True))
            chip.add_css_class('dev-chip')
            chip.add_css_class('app')
            if s in act.monitors:
                chip.add_css_class('monitor')
                tip = f'{s.name} is recording what this output plays'
            else:
                tip = (f'{s.name}: {s.media}' if s.media and s.media != s.name
                       else s.name)
            chip.set_tooltip_text(tip + ' (click, or drag it onto another '
                                  'device, to move it)')
            chip.set_popover(self._move_popover(s, recording, ctx))
            drag_source(chip, lambda st=s: f'stream:{st.id}',
                        on_begin=lambda st=s: self.page.drag_started(st),
                        on_end=self.page.drag_ended,
                        dragging_css='dev-dragging')
            chips.append(chip)
        self._chips(self.apps_box, key, chips,
                    'Nothing playing' if self.sink else 'Nothing recording')

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

    # -- popovers ----------------------------------------------------------
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

    def _output_popover(self, act):
        rows = [(n.description, device_icon(n), n.id == act.out_dest,
                 lambda n=n: self.page.repoint(self, act.out_stream, n))
                for n in self._outs]
        return self._list_popover('Send to', rows)

    # -- handlers ----------------------------------------------------------
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
        if self.live['updating']:
            return
        self.touch()
        active, nid = btn.get_active(), self.node.id
        async_call(lambda: pw.set_mute(nid, active))

    def _on_port_toggle(self, tg, _pspec):
        if getattr(self, '_updating_port', False):
            return
        name = tg.get_active_name()
        if name is None or not name.isdigit():
            return
        idx = int(name)
        if idx == self.node.active_port:
            return
        label = next((d for i, d, _a in self.node.ports if i == idx), '')
        self._set_port(idx, label)

    def _on_port_dd(self, dd, _pspec):
        if getattr(self, '_updating_port', False):
            return
        pos = dd.get_selected()
        if pos == Gtk.INVALID_LIST_POSITION or pos >= len(self._ports):
            return
        idx, label = self._ports[pos]
        if idx != self.node.active_port:
            self._set_port(idx, label)

    def _set_port(self, idx, label):
        self.touch()
        nid, window = self.node.id, self.page.window
        async_call(lambda: pw.set_route(nid, idx),
                   lambda ok, e: window.toast(
                       f'{label}' if ok and not e
                       else 'Could not switch the port'))

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

    def _copy_name(self):
        Gdk.Display.get_default().get_clipboard().set(self.node.name)
        self.page.window.toast(f'Copied {self.node.name}')


class CardOffCard:
    """A sound card that could give this direction but gives nothing now.

    Off, or an output-only profile seen from Inputs.  Without this the card
    would simply be missing from the page, and with it the only way back.
    """

    def __init__(self, page, card, sink: bool):
        self.page = page
        self.sink = sink
        self.key = ('card', card.id, sink)
        self.filter_text = card.description.lower()
        self.live = {'local_ts': 0.0}
        what = 'output' if sink else 'input'

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                              spacing=12, valign=Gtk.Align.START)
        self.widget.add_css_class('dev-card')
        self.widget.add_css_class('off')
        head = Gtk.Box(spacing=12)
        avatar = Gtk.Image.new_from_icon_name('pwctl-devices-symbolic')
        avatar.add_css_class('dash-avatar')
        avatar.add_css_class('off')
        avatar.set_valign(Gtk.Align.CENTER)
        head.append(avatar)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                        hexpand=True, valign=Gtk.Align.CENTER)
        self.title = _label(css=('dev-title',), chars=28)
        names.append(self.title)
        names.append(_label(f'Sound card · no {what} on this profile',
                            css=('caption', 'dim-label'), chars=34))
        head.append(names)
        head.append(pill('off', 'dim'))
        self.widget.append(head)

        self.profile = _ProfilePicker(page, sink, self.touch)
        row = Gtk.Box(spacing=12)
        lbl = micro('Profile')
        lbl.add_css_class('dev-row-label')
        lbl.set_valign(Gtk.Align.CENTER)
        page.label_group.add_widget(lbl)
        row.append(lbl)
        row.append(self.profile.dropdown)
        self.widget.append(row)
        self.widget.append(self.profile.warn)
        self.paint(card)

    def touch(self):
        self.live['local_ts'] = _now()

    def paint(self, card, _ctx=None):
        self.title.set_label(card.description)
        self.profile.update(card, _now() - self.live['local_ts'] < LOCAL_GRACE)
