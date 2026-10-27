"""The Devices page's list pane: every device, grouped by kind.

Sound cards, then Bluetooth, then virtual and processing devices
(`endpoints.KINDS`), each as a micro heading.  A sound card with more than
one row (an output and an input, or a direction that is off) gets a heading
of its own with its active profile, and its rows sit under it; a device
with one row is listed on its own.  A running row carries a thin live level,
and the default output and input a star.  Hidden devices close the list.

The list never selects anything itself: selection is off, and the page draws
the current row with the selected look (`highlight`), as the app's sidebar
does.  With selection on, focus coming back to the list (after the narrow
layout's detail page, say) moved the selection to the next row.

Each row is also where an app chip from the detail can be dropped: the move
its "Move to" menu makes, as a gesture.
"""

from __future__ import annotations

import math

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from ..backend import endpoints, levels  # noqa: E402
from .device_detail import blank_live  # noqa: E402
from .volume import level_color  # noqa: E402
from .widgets import device_icon, drop_target, micro  # noqa: E402

KIND_TITLE = dict(endpoints.KINDS)

DIRECTIONS = (('all', 'All'), ('out', 'Outputs'), ('in', 'Inputs'))


def _label(text='', css=()):
    lbl = Gtk.Label(label=text, xalign=0, ellipsize=Pango.EllipsizeMode.END)
    for c in css:
        lbl.add_css_class(c)
    return lbl


class MiniMeter:
    """A thin live level under a running row.

    Meters only while the node runs and the row is on screen: a suspended
    device carries no sound, and each meter is a `pw-record` (levels.py).
    The strip keeps its height either way, so a row does not change size
    when its device starts or stops.
    """

    HEIGHT = 3

    def __init__(self):
        self.area = Gtk.DrawingArea(hexpand=True)
        self.area.set_content_height(self.HEIGHT)
        self.area.add_css_class('devl-meter')
        self.area.set_draw_func(self._draw)
        self.serial = None
        self.running = False
        self._sub = False
        self._tick = None
        self._level = 0.0
        self.area.connect('map', lambda *_a: self._attach())
        self.area.connect('unmap', lambda *_a: self._detach())
        self.area.connect('destroy', lambda *_a: self._detach())

    def set(self, serial, running):
        want = serial if running else None
        self.running = bool(running)
        if want == self.serial:
            return
        self._detach()
        self.serial = want
        if want is not None and self.area.get_mapped():
            self._attach()
        self.area.queue_draw()

    def _attach(self):
        if self._sub or self.serial is None:
            return
        if not levels.subscribe(self.serial):
            return                  # no slot free: an empty strip, not a lie
        self._sub = True
        if self._tick is None:
            self._tick = self.area.add_tick_callback(self._on_tick)

    def _detach(self):
        if self._tick is not None:
            self.area.remove_tick_callback(self._tick)
            self._tick = None
        if self._sub:
            levels.unsubscribe(self.serial)
            self._sub = False
        if self._level:
            self._level = 0.0
            self.area.queue_draw()

    def _on_tick(self, _area, _clock):
        if not self._sub:
            return GLib.SOURCE_REMOVE
        level, _hold = levels.level(self.serial)
        if abs(level - self._level) > 0.01:
            self._level = level
            self.area.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw(self, _area, cr, w, h, *_a):
        if not self.running or w <= 0 or h <= 0:
            return
        r = h / 2

        def bar(width):
            cr.new_sub_path()
            cr.arc(width - r, r, r, -math.pi / 2, math.pi / 2)
            cr.arc(r, r, r, math.pi / 2, 3 * math.pi / 2)
            cr.close_path()
        bar(w)
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.18)
        cr.fill()
        if self._level <= 0.0:
            return
        red, green, blue = level_color(self._level)
        bar(max(h, w * self._level))
        cr.set_source_rgba(red, green, blue, 1.0)
        cr.fill()


class Entry:
    """One row of the list: what it shows and how it is matched."""

    kind = ''           # endpoints.KINDS key, or 'hidden'
    sink = None         # True / False, None for Hidden
    group = None        # endpoints.ListGroup
    search = ''

    def _row(self, page, child):
        self.page = page
        self.row = Gtk.ListBoxRow()
        self.row.entry = self
        self.row.set_child(child)
        self.row.add_css_class('devl-row')

    def _lead(self, icon, css, in_card):
        if in_card:
            img = Gtk.Image.new_from_icon_name(icon)
            img.add_css_class('devl-dir')
            img.set_valign(Gtk.Align.CENTER)
            return img
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(17)
        img.add_css_class('dash-avatar')
        img.add_css_class('devl-tile')
        img.add_css_class(css)
        img.set_valign(Gtk.Align.CENTER)
        return img

    def matches(self, direction, text):
        if direction == 'out' and self.sink is not True:
            return False
        if direction == 'in' and self.sink is not False:
            return False
        return not text or text in self.search

    def paint(self, *_a):
        pass


class NodeEntry(Entry):
    """An output or input."""

    def __init__(self, page, node, group, in_card):
        self.node = node
        self.key = (node.id, node.serial)
        self.ident = ('node', node.is_sink, node.name)
        self.sink = node.is_sink
        self.kind = group.kind
        self.group = group
        self.in_card = in_card
        self.live = blank_live(node)
        self.live['star'] = lambda d: page.paint_star(self, d)
        self._star_state = None

        box = Gtk.Box(spacing=10)
        box.add_css_class('devl-row-box')
        icon = (('audio-speakers-symbolic' if node.is_sink
                 else 'audio-input-microphone-symbolic') if in_card
                else device_icon(node))
        box.append(self._lead(icon, 'out' if node.is_sink else 'in', in_card))
        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                       hexpand=True, valign=Gtk.Align.CENTER)
        self.title = _label(css=('devl-row-title',))
        self.subtitle = _label(css=('caption', 'dim-label'))
        self.meter = MiniMeter()
        self.meter.area.set_margin_top(3)
        meta.append(self.title)
        meta.append(self.subtitle)
        meta.append(self.meter.area)
        box.append(meta)
        self.alert = Gtk.Image.new_from_icon_name('dialog-warning-symbolic')
        self.alert.add_css_class('dev-alert')
        self.alert.set_visible(False)
        box.append(self.alert)
        self.star = Gtk.Image.new_from_icon_name('starred-symbolic')
        self.star.add_css_class('devl-star')
        self.star.set_visible(False)
        box.append(self.star)
        self._row(page, box)
        if in_card:
            self.row.add_css_class('child')
        drop_target(self.row, GObject.TYPE_STRING,
                    lambda value, _x, _y: page.drop_on(self, value),
                    on_motion=lambda value, _x, _y: self._hover(value),
                    on_leave=lambda: self.row.remove_css_class('drop-into'))

    def _hover(self, value):
        ok = self.page.accepts(self, value)
        if ok:
            self.row.add_css_class('drop-into')
        return ok

    def paint(self, node, ctx):
        self.node = node
        card = self.group.card or endpoints.card_of(node, ctx['cards'])
        if self.in_card:
            same = [n for n in self.group.nodes if n.is_sink == node.is_sink]
            title = endpoints.direction(node)
            if len(same) > 1:
                title = endpoints.part_name(node, card)
            sub = (endpoints.row_subtitle(node, in_card=True)
                   or endpoints.part_name(node, card))
        else:
            title = node.description
            sub = endpoints.row_subtitle(node)
        self.title.set_label(title)
        self.subtitle.set_label(sub)
        self.row.set_tooltip_text(f'{node.description}\n{node.name}')
        self.meter.set(node.serial, node.state == 'running')
        problem = False
        hw_card = endpoints.card_of(node, ctx['cards'])
        if hw_card is not None and len(hw_card.profiles) > 1:
            problem = endpoints.profile_state(hw_card, node.is_sink).unavailable
        self.alert.set_visible(problem)
        self.alert.set_tooltip_text(
            'This profile is unavailable: there is no sound until you switch'
            if problem else None)
        self.search = ' '.join((endpoints.search_text(node),
                                (card.description if card else '').lower()))

    def paint_star(self, is_default):
        if self._star_state == is_default:
            return
        self._star_state = is_default
        self.star.set_visible(bool(is_default))
        self.star.set_tooltip_text(
            f'The default {"output" if self.sink else "input"}'
            if is_default else None)


class OffEntry(Entry):
    """A sound card's direction that gives nothing on its profile."""

    def __init__(self, page, card, sink, group, in_card):
        self.card = card
        self.key = ('card', card.id, sink)
        self.ident = ('off', card.name, sink)
        self.sink = sink
        self.kind = group.kind
        self.group = group
        self.in_card = in_card
        self.live = {'local_ts': 0.0}
        box = Gtk.Box(spacing=10)
        box.add_css_class('devl-row-box')
        icon = (('audio-speakers-symbolic' if sink
                 else 'audio-input-microphone-symbolic') if in_card
                else 'pwctl-devices-symbolic')
        box.append(self._lead(icon, 'off', in_card))
        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                       hexpand=True, valign=Gtk.Align.CENTER)
        self.title = _label(css=('devl-row-title',))
        what = 'output' if sink else 'input'
        meta.append(self.title)
        meta.append(_label(f'No {what} on this profile' if in_card
                           else f'Off · no {what} on this profile',
                           css=('caption', 'dim-label')))
        box.append(meta)
        self._row(page, box)
        self.row.add_css_class('off')
        if in_card:
            self.row.add_css_class('child')
        self.paint(card)

    def paint(self, card, _ctx=None):
        self.card = card
        self.title.set_label(('Output' if self.sink else 'Input')
                             if self.in_card else card.description)
        self.search = card.description.lower()


class HiddenEntry(Entry):
    kind = 'hidden'
    key = 'hidden'
    ident = ('hidden',)

    def __init__(self, page, count):
        self.count = count
        self.live = {'local_ts': 0.0}
        box = Gtk.Box(spacing=10)
        box.add_css_class('devl-row-box')
        img = Gtk.Image.new_from_icon_name('view-conceal-symbolic')
        img.add_css_class('devl-dir')
        box.append(img)
        box.append(Gtk.Label(label='Hidden devices', xalign=0, hexpand=True))
        n = Gtk.Label(label=str(count), valign=Gtk.Align.CENTER)
        n.add_css_class('devl-count')
        box.append(n)
        self._row(page, box)
        self.row.add_css_class('devl-hidden-row')

    def matches(self, direction, text):
        return not text


class DeviceList:
    """The pane: direction filter, search, Add, and the rows."""

    def __init__(self, page):
        self.page = page
        self.entries = []
        self.current = None
        self._updating = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class('devl-list')
        tools = Gtk.Box(spacing=6, margin_top=10, margin_bottom=6,
                        margin_start=10, margin_end=10)
        self.dirs = Adw.ToggleGroup(hexpand=True, can_shrink=True)
        self.dirs.add_css_class('devl-dirs')
        for name, label in DIRECTIONS:
            self.dirs.add(Adw.Toggle(name=name, label=label))
        self.dirs.set_active_name('all')
        self.dirs.connect('notify::active-name', lambda *_a: self.refilter())
        tools.append(self.dirs)
        self.search_button = Gtk.ToggleButton(
            icon_name='system-search-symbolic', valign=Gtk.Align.CENTER,
            tooltip_text='Search devices')
        self.search_button.add_css_class('flat')
        tools.append(self.search_button)
        add = Gtk.MenuButton(icon_name='list-add-symbolic',
                             valign=Gtk.Align.CENTER,
                             tooltip_text='Add a device')
        add.add_css_class('flat')
        add.set_menu_model(self._add_menu(box))
        tools.append(add)
        box.append(tools)

        self.search = Gtk.SearchEntry(
            placeholder_text='Name, port or PipeWire name', hexpand=True)
        self.search.connect('search-changed', lambda *_a: self.refilter())
        self.search_bar = Gtk.SearchBar(child=self.search)
        self.search_bar.connect_entry(self.search)
        self.search_bar.set_key_capture_widget(box)
        self.search_bar.bind_property(
            'search-mode-enabled', self.search_button, 'active',
            GObject.BindingFlags.SYNC_CREATE
            | GObject.BindingFlags.BIDIRECTIONAL)
        self.search_bar.connect('notify::search-mode-enabled',
                                self._on_search_mode)
        box.append(self.search_bar)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class('navigation-sidebar')
        self.listbox.add_css_class('devl-listbox')
        self.listbox.set_header_func(self._header)
        self.listbox.set_filter_func(self._visible)
        empty = Gtk.Label(label='No devices match', margin_top=24,
                          margin_bottom=24)
        empty.add_css_class('dim-label')
        self.listbox.set_placeholder(empty)
        self.listbox.connect('row-activated', self._on_activate)
        keys = Gtk.EventControllerKey(
            propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self._on_key)
        self.listbox.add_controller(keys)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vexpand=True, child=self.listbox)
        box.append(sw)
        self.widget = box

    def _add_menu(self, box):
        group = Gio.SimpleActionGroup()
        window = self.page.window

        def action(name, cb):
            act = Gio.SimpleAction.new(name, None)
            act.connect('activate', lambda *_a: cb())
            group.add_action(act)

        action('virtual', self.page.add_virtual)
        action('eq', lambda: window.goto('enhance'))
        action('chain', lambda: window.goto('chains'))
        action('paths', lambda: window.goto('paths'))
        menu = Gio.Menu()
        menu.append('New virtual device…', 'devlist.virtual')
        more = Gio.Menu()
        more.append('Equalizer', 'devlist.eq')
        more.append('Filter Chains', 'devlist.chain')
        more.append('Signal Paths', 'devlist.paths')
        menu.append_section('Or make one on', more)
        box.insert_action_group('devlist', group)
        return menu

    # -- building -------------------------------------------------------------
    def rebuild(self, groups, ctx, hidden_count):
        """Replace every row.  Returns the entries, in display order."""
        child = self.listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            if isinstance(child, Gtk.ListBoxRow):
                self.listbox.remove(child)
            child = nxt
        self.entries = []
        self.current = None
        for g in groups:
            in_card = g.card is not None
            for n in g.nodes:
                e = NodeEntry(self.page, n, g, in_card)
                e.paint(n, ctx)
                self.entries.append(e)
            for card, sink in g.off:
                self.entries.append(OffEntry(self.page, card, sink, g,
                                             in_card))
        if hidden_count:
            self.entries.append(HiddenEntry(self.page, hidden_count))
        for e in self.entries:
            self.listbox.append(e.row)
        return self.entries

    def _header(self, row, before):
        entry = row.entry
        prev = before.entry if before is not None else None
        if entry.kind == 'hidden':
            sep = Gtk.Separator(margin_top=8, margin_bottom=4)
            row.set_header(sep)
            return
        parts = []
        if prev is None or prev.kind != entry.kind:
            head = micro(KIND_TITLE.get(entry.kind, 'Other'))
            head.add_css_class('devl-kind')
            head.set_margin_top(4 if prev is None else 14)
            parts.append(head)
        card = entry.group.card
        if card is not None and (prev is None or prev.group is not
                                 entry.group):
            parts.append(self._card_head(entry.group))
        if not parts:
            row.set_header(None)
            return
        if len(parts) == 1:
            row.set_header(parts[0])
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for p in parts:
            box.append(p)
        row.set_header(box)

    def _card_head(self, group):
        card = group.card
        box = Gtk.Box(spacing=10)
        box.add_css_class('devl-card-head')
        icon = (device_icon(group.nodes[0]) if group.nodes
                else 'pwctl-devices-symbolic')
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(17)
        img.add_css_class('dash-avatar')
        img.add_css_class('devl-tile')
        img.add_css_class('card')
        img.set_valign(Gtk.Align.CENTER)
        box.append(img)
        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                       hexpand=True, valign=Gtk.Align.CENTER)
        meta.append(_label(card.description, css=('devl-row-title',)))
        profile = endpoints.profile_name(card, card.active_profile)
        meta.append(_label(profile or 'Sound card', css=('caption',
                                                         'dim-label')))
        box.append(meta)
        box.set_tooltip_text(f'{card.description}: '
                             f'{profile or "no profile"}')
        return box

    # -- filtering --------------------------------------------------------------
    @property
    def direction(self):
        return self.dirs.get_active_name() or 'all'

    def _visible(self, row):
        entry = getattr(row, 'entry', None)
        if entry is None:
            return True
        return entry.matches(self.direction,
                             self.search.get_text().strip().lower())

    def refilter(self):
        self.listbox.invalidate_filter()
        self.listbox.invalidate_headers()

    def _on_search_mode(self, bar, _pspec):
        if not bar.get_search_mode() and self.search.get_text():
            self.search.set_text('')

    def visible_entries(self):
        return [e for e in self.entries if self._visible(e.row)]

    # -- the current row --------------------------------------------------------
    def highlight(self, current):
        """Draw one row as the selected device, without selecting anything.

        `.navigation-sidebar` styles `row:selected` from the SELECTED state
        flag, so setting it keeps libadwaita's look; a screen reader learns
        it from the accessible state, set beside it (lists of ints).
        """
        for e in self.entries:
            on = e is current
            if on:
                e.row.set_state_flags(Gtk.StateFlags.SELECTED, False)
            else:
                e.row.unset_state_flags(Gtk.StateFlags.SELECTED)
            e.row.update_state([Gtk.AccessibleState.SELECTED], [int(on)])
        self.current = current

    def _on_activate(self, _lb, row):
        entry = getattr(row, 'entry', None)
        if entry is not None:
            self.page.select(entry)

    def _on_key(self, _ctl, keyval, _code, state):
        """Up, Down, Home, End, Page Up/Down: open the row focus lands on.

        Only beside the detail.  In the narrow layout the detail is a page
        of its own, so moving through the list must not open one each step.
        """
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if self.page.collapsed:
            return False
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_KP_Up,
                      Gdk.KEY_KP_Down, Gdk.KEY_Home, Gdk.KEY_End,
                      Gdk.KEY_Page_Up, Gdk.KEY_Page_Down):
            GLib.idle_add(self._follow_focus)
        return False

    def _follow_focus(self):
        row = self.listbox.get_focus_child()
        entry = getattr(row, 'entry', None)
        if entry is not None and entry is not self.current:
            self.page.select(entry, show=False)
        return False

    def drag_marks(self, accepts):
        """Light up every row a dragged app would land on; dim the rest."""
        for e in self.entries:
            if isinstance(e, NodeEntry):
                e.row.add_css_class('drop-ready' if accepts(e)
                                    else 'drop-no')

    def clear_drag_marks(self):
        for e in self.entries:
            for css in ('drop-ready', 'drop-no', 'drop-into'):
                e.row.remove_css_class(css)
