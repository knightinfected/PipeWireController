"""Signal Paths: build a chain from an app to an output, stage by stage.

Sources in the left column, mixes in the right, the sends drawn between them.
A source is where audio enters — an app, a microphone, or everything on the
default output — and carries its own chain; a mix carries a chain of its own
and feeds real devices.  One source and one mix is a straight line, which is
what most setups are; the second dimension only matters when a chain has to
split.

The page is deliberately *not* a list of settings rows.  What the user is
building is a signal flow, so the flow is the interface: each strip is a card
whose chain is always visible as a row of stage chips, and a send is a curve
between two cards rather than a sentence in a subtitle.  Rows were tried first
and read as a control panel for something else — the shape of the audio was
nowhere on screen, and the page sat in one narrow column with the rest of the
window empty.

Two audiences pull in opposite directions and both are served here.  Someone
who just wants an equalizer on everything should never meet the word "mix":
Quick setup builds the whole arrangement in one click, and is the entire page
until something exists.  Someone running twenty plugin sinks into three
outputs needs every picker to cope with a long list, so devices, plugins and
apps are all chosen through a searchable dialog rather than a dropdown that
becomes unusable past a dozen entries.

Because the flow is on screen, it is also *handled* directly: stages are
dragged along the rail to reorder them and between cards to move them, cards
are dragged to rearrange a column or onto the opposite column to wire a send,
and an app is dragged from one card to another to move what it is playing
through.  A stage chip answers a single click by going in or out of the
signal, and a double-click or right-click by opening its editor — the thing
people do twenty times an hour is the cheapest gesture, and the thing they do
once is the deliberate one.
"""

from __future__ import annotations

import json

import cairo
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

from ..backend import levels, path_templates, paths, plugins, prefs, pw, \
    virtual
from .volume import make_volume
from .widgets import async_call, confirm, esc, group, icon_button, \
    pick_file, pick_folder, pill, state_style

_PLUGIN_CACHE: dict = {'all': None}

KIND_ICON = {
    'app': 'application-x-executable-symbolic',
    'mic': 'audio-input-microphone-symbolic',
    'everything': 'audio-volume-high-symbolic',
}
KIND_BLURB = {
    'app': 'An application',
    'mic': 'A microphone',
    'everything': 'Everything on the default output',
}
STAGE_ICON = {
    'eq': 'audio-x-generic-symbolic',
    'effect': 'applications-multimedia-symbolic',
    'convolver': 'audio-headphones-symbolic',
    'xover': 'view-list-symbolic',
}
BAND_TYPES = [('PK', 'Peak'), ('LSC', 'Low shelf'), ('HSC', 'High shelf')]
XOVER_MODES = [('lowpass', 'Low band'), ('highpass', 'High band'),
               ('bandpass', 'Middle band')]
XOVER_SLOPES = [(12, '12 dB/oct (LR2)'), (24, '24 dB/oct (LR4)'),
                (48, '48 dB/oct (LR8)')]

# Rewriting the graph restarts the unit, so audio stops for an instant.  A
# short delay after the last edit turns "bypass three stages" into one
# interruption instead of three.
APPLY_DELAY_MS = 350

# The board draws the live graph, so it has to follow it: an app starting or
# stopping, a strip's node coming back after a restart, a unit failing.  Same
# cadence as the dashboard.  Cheap because a poll only rebuilds when what it
# draws has actually changed — see `_signature`.
POLL_SEC = 3

# How long after `paths.apply` returns to look again.  systemd reports the
# unit started before the graph has the node published, linked and playing.
SETTLE_MS = 1500

# The classes a drop indicator can be wearing; cleared together so a widget
# never keeps one after the pointer has moved on.
DROP_CLASSES = ('drop-before', 'drop-after', 'drop-into', 'link-into')

# Channel counts users recognise.  "7.1" says more than "8ch" and takes less
# room; anything unusual falls back to the count.
LAYOUT_NAMES = {1: 'Mono', 2: 'Stereo', 3: '2.1', 4: 'Quad',
                6: '5.1', 8: '7.1'}


def _layout_name(n: int) -> str:
    return LAYOUT_NAMES.get(n, f'{n} ch')


def _adj(lo, hi, val, step, page):
    return Gtk.Adjustment(lower=lo, upper=hi, value=val,
                          step_increment=step, page_increment=page)


def _all_plugins():
    if _PLUGIN_CACHE['all'] is None:
        _PLUGIN_CACHE['all'] = plugins.scan_ladspa() + plugins.scan_lv2()
    return _PLUGIN_CACHE['all']


# ------------------------------------------------------------- primitives --

def micro(text: str, kind: str = '') -> Gtk.Label:
    """A section label: small, heavy, quiet.  Uppercased here rather than in
    CSS so it does not depend on GTK's text-transform support.

    `kind` tints it with a column's hue, which is only wanted for the two
    column headings — the ones inside a card stay quiet."""
    lbl = Gtk.Label(label=text.upper(), xalign=0, valign=Gtk.Align.CENTER)
    lbl.add_css_class('path-label')
    if kind:
        lbl.add_css_class(f'k-{kind}')
    return lbl


def avatar(icon_name: str, kind: str) -> Gtk.Image:
    img = Gtk.Image.new_from_icon_name(icon_name)
    img.set_pixel_size(18)
    img.add_css_class('path-avatar')
    img.add_css_class(f'k-{kind}')
    img.set_valign(Gtk.Align.CENTER)
    return img


def chip(label: str, tooltip: str = '', on_click=None, icon: str = '',
         active: bool = False, toggle: bool = False) -> Gtk.Widget:
    btn = Gtk.ToggleButton() if toggle else Gtk.Button()
    btn.add_css_class('path-chip')
    btn.set_valign(Gtk.Align.CENTER)
    if tooltip:
        btn.set_tooltip_text(tooltip)
    box = Gtk.Box(spacing=5)
    # A dot in front of the label, shown only while the chip is live.  It
    # takes the chip's own colour, so "this send carries audio" is legible at
    # a glance instead of resting entirely on a tinted background.
    dot = Gtk.Box(css_classes=['path-chip-dot'], valign=Gtk.Align.CENTER,
                  visible=active)
    box.append(dot)
    lbl = Gtk.Label(label=esc(label), ellipsize=Pango.EllipsizeMode.END,
                    max_width_chars=18)
    box.append(lbl)
    if icon:
        box.append(Gtk.Image.new_from_icon_name(icon))
    btn.set_child(box)
    if toggle:
        btn.set_active(active)
        # The rebuild that follows a toggle waits on the graph, so the dot
        # follows the button directly — otherwise a chip sits checked and
        # dotless for as long as the apply takes.
        btn.connect('toggled', lambda b: dot.set_visible(b.get_active()))
    elif active:
        btn.add_css_class('on')
    if on_click:
        btn.connect('toggled' if toggle else 'clicked', on_click)
    return btn


def catalog_card(icon: str, title: str, blurb: str, on_click, chain=(),
                 badge: str = '', tone: str = 'recipe') -> Gtk.Button:
    """One offer in the catalog: a recipe or a template.

    Recipes and templates are different things — one builds a whole
    arrangement, the other a single strip — but they answer the same question
    ("what can I start from?"), so they are drawn as one kind of card and
    told apart by the badge and by the hue of the icon tile.

    The chain is drawn as the board draws it, in the same chips with the same
    arrows, so a card is a small picture of what it is about to build rather
    than a paragraph describing it.
    """
    btn = Gtk.Button(css_classes=['path-recipe', f'k-{tone}'])
    # Explicitly *not* expanding, and it has to be said out loud: GTK treats a
    # widget as expanding when any descendant does, and the badge below pushes
    # itself to the right edge with `hexpand`.  Left alone, that reaches the
    # card, and a last line holding one card stretches it across the whole
    # width — a banner where a card was meant to be.  Setting the flag here
    # explicitly overrides what the children ask for.
    btn.set_hexpand(False)
    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)

    top = Gtk.Box(spacing=10)
    img = Gtk.Image.new_from_icon_name(icon)
    img.set_pixel_size(18)
    img.add_css_class('path-recipe-icon')
    img.add_css_class(f'k-{tone}')
    img.set_valign(Gtk.Align.CENTER)
    top.append(img)
    if badge:
        lbl = Gtk.Label(label=badge, valign=Gtk.Align.CENTER, halign=Gtk.Align.END,
                        hexpand=True, css_classes=['path-tpl-badge', f'k-{tone}'])
        top.append(lbl)
    inner.append(top)

    # Every label is bounded, and they are all bounded to about the same
    # width.  A wrap box packs its lines by natural width, so one card whose
    # blurb happens to fit on a long line makes that whole row hold two cards
    # while the next holds three — the grid stops looking like a grid.
    inner.append(Gtk.Label(label=esc(title), xalign=0, wrap=True,
                           max_width_chars=22, css_classes=['heading']))
    inner.append(Gtk.Label(label=esc(blurb), xalign=0, wrap=True,
                           max_width_chars=28,
                           css_classes=['dim-label', 'caption']))
    if chain:
        # Left to itself a wrap box asks for every child on one line, which a
        # five-stage chain turns into a very wide card.
        rail = Adw.WrapBox(child_spacing=4, line_spacing=4, margin_top=2,
                           natural_line_length=210)
        for i, name in enumerate(chain):
            step = Gtk.Label(label=esc(name), css_classes=['path-tpl-step'],
                             ellipsize=Pango.EllipsizeMode.END,
                             max_width_chars=16, valign=Gtk.Align.CENTER)
            if not i:
                rail.append(step)
                continue
            # Same trick the board's rail uses: the arrow travels with the
            # chip it points at, so a wrapped line never ends in a stray "›".
            pair = Gtk.Box(spacing=4)
            pair.append(Gtk.Label(label='›', css_classes=['path-arrow'],
                                  valign=Gtk.Align.CENTER))
            pair.append(step)
            rail.append(pair)
        inner.append(rail)

    # Filled in later, if a scan finds this machine has none of the plugins a
    # template asks for.  Built here and left hidden so the card never changes
    # height when the answer arrives.
    note = Gtk.Label(xalign=0, wrap=True, max_width_chars=28, visible=False,
                     css_classes=['caption', 'path-tpl-warn'])
    inner.append(note)

    btn.set_child(inner)
    btn.connect('clicked', lambda *_: on_click())
    btn._note = note
    return btn


def add_button(tooltip: str, on_click) -> Gtk.Button:
    btn = Gtk.Button(icon_name='list-add-symbolic', tooltip_text=tooltip,
                     valign=Gtk.Align.CENTER, css_classes=['path-add'])
    btn.connect('clicked', on_click)
    return btn


def menu_popover(items) -> Gtk.Popover:
    """Popover of flat icon+label buttons.  `items` is (icon, label, fn) or
    None for a separator."""
    pop = Gtk.Popover()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                  margin_top=6, margin_bottom=6,
                  margin_start=6, margin_end=6)
    for item in items:
        if item is None:
            box.append(Gtk.Separator(margin_top=3, margin_bottom=3))
            continue
        icon, label, fn, *rest = item
        btn = Gtk.Button(css_classes=['flat'])
        if rest and rest[0]:
            btn.add_css_class(rest[0])
        inner = Gtk.Box(spacing=10)
        inner.append(Gtk.Image.new_from_icon_name(icon))
        inner.append(Gtk.Label(label=label, xalign=0, hexpand=True))
        btn.set_child(inner)
        btn.connect('clicked', lambda _b, f=fn: (pop.popdown(), f()))
        box.append(btn)
    pop.set_child(box)
    return pop


# ------------------------------------------------------- drag and drop --
# Everything rearrangeable on the board moves the same way: a small object
# says what is being dragged, the widget under the pointer decides whether it
# wants that kind, and the drop mutates the model and re-renders.  Each kind
# is its own GType, so a card never has to inspect a stage drag to know it is
# not for it — GTK filters by type before the controller is ever asked, and a
# text drag from another application matches nothing here at all.
#
# Payloads carry ids, not objects.  A refresh in the middle of a drag replaces
# every strip with a freshly loaded one, and a held reference would then
# mutate a copy nothing else can see.

class StageDrag(GObject.Object):
    __gtype_name__ = 'PwctlPathStageDrag'

    def __init__(self, strip_id: str = '', stage_id: str = ''):
        super().__init__()
        self.strip_id, self.stage_id = strip_id, stage_id


class StripDrag(GObject.Object):
    __gtype_name__ = 'PwctlPathStripDrag'

    def __init__(self, strip_id: str = '', role: str = ''):
        super().__init__()
        self.strip_id, self.role = strip_id, role


class StreamDrag(GObject.Object):
    __gtype_name__ = 'PwctlPathStreamDrag'

    def __init__(self, stream_id: int = 0, name: str = ''):
        super().__init__()
        self.stream_id, self.name = stream_id, name


def clear_drop(widget):
    for c in DROP_CLASSES:
        widget.remove_css_class(c)


def drag_source(widget, make_payload, icon=None, offset=(0, 0),
                on_begin=None, on_end=None):
    """Let `widget` start a drag carrying whatever `make_payload()` returns.

    The controller runs in the capture phase because a stage chip is a real
    button: its own click gesture sits in the bubble phase and was installed
    first, so a bubble-phase drag source would only ever see presses it had
    already lost.

    `on_begin`/`on_end` bracket the whole gesture, which is how the board
    lights up every place the thing being carried could land.  Without that,
    "can I drop here?" can only be answered by trying it.
    """
    src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
    src.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    hot = [0.0, 0.0]

    def prepare(_s, x, y):
        hot[0], hot[1] = x, y
        payload = make_payload()
        if payload is None:
            return None
        return Gdk.ContentProvider.new_for_value(
            GObject.Value(type(payload), payload))

    def begin(s, _drag):
        widget.add_css_class('path-dragging')
        s.set_icon(Gtk.WidgetPaintable.new(icon or widget),
                   int(hot[0]) + offset[0], int(hot[1]) + offset[1])
        if on_begin:
            on_begin()

    def end(*_a):
        widget.remove_css_class('path-dragging')
        if on_end:
            on_end()
        return False

    src.connect('prepare', prepare)
    src.connect('drag-begin', begin)
    # Both endings have to be caught: a drop fires drag-end, a drag abandoned
    # over nothing fires drag-cancel, and the board must come back either way.
    src.connect('drag-end', end)
    src.connect('drag-cancel', end)
    widget.add_controller(src)
    return src


def drop_target(widget, gtype, on_drop, on_motion=None, on_leave=None):
    """Accept drags of one payload type.

    `on_motion(payload, x, y)` returns whether the drop would do anything and
    is where the indicator is drawn; returning False greys the cursor, so the
    pointer says "not here" before the button is released rather than after.
    """
    tgt = Gtk.DropTarget.new(gtype, Gdk.DragAction.MOVE)
    tgt.set_preload(True)          # so motion can look at what is coming

    def motion(_t, x, y):
        value = tgt.get_value()
        if value is None:          # not loaded yet — assume it will do
            return Gdk.DragAction.MOVE
        ok = True if on_motion is None else on_motion(value, x, y)
        return Gdk.DragAction.MOVE if ok else 0

    def leave(_t):
        if on_leave:
            on_leave()

    def drop(_t, value, x, y):
        if on_leave:
            on_leave()
        return bool(on_drop(value, x, y))

    tgt.connect('enter', motion)
    tgt.connect('motion', motion)
    tgt.connect('leave', leave)
    tgt.connect('drop', drop)
    widget.add_controller(tgt)
    return tgt


def context_menu(anchor, items, x, y):
    """Pop `items` up at a point inside `anchor`, and clean up after itself."""
    pop = menu_popover(items)
    pop.set_parent(anchor)
    pop.set_has_arrow(False)
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    pop.set_pointing_to(rect)
    # Unparenting from inside "closed" tears the widget down while it is still
    # emitting; one turn of the loop later is safe.
    pop.connect('closed', lambda p: GLib.idle_add(p.unparent))
    pop.popup()
    return pop


def dbl_ms() -> int:
    """The desktop's double-click time — how long a single click waits."""
    settings = Gtk.Settings.get_default()
    if settings is None:
        return 400
    try:
        return max(200, int(settings.get_property('gtk-double-click-time')))
    except (TypeError, ValueError):
        return 400


def chip_row(label: str, *widgets) -> Adw.WrapBox:
    """A labelled row of chips that wraps instead of overflowing — a mix with
    six outputs has to stay inside its card."""
    # Chips keep their natural width and the box takes another line when it
    # runs out — squeezing them instead (WrapPolicy.MINIMUM) turns every
    # device name into an ellipsis long before the card is actually full.
    # `max_width_chars` on the chip labels is what keeps that natural width
    # bounded, so one very long name can't set the column width.
    box = Adw.WrapBox(child_spacing=6, line_spacing=6)
    box.append(micro(label))
    for w in widgets:
        if w is not None:
            box.append(w)
    return box


# --------------------------------------------------------------- pickers --

def search_picker(parent, title, subtitle, items, on_pick, empty=''):
    """A searchable list dialog.

    `items` is a list of (key, title, subtitle) tuples.  Every long list on
    this page goes through here — a machine with twenty sinks and three
    hundred LV2 plugins makes a ComboRow useless, and the search entry is the
    difference between the page scaling and not.
    """
    dlg = Adw.Dialog(title=title, content_width=520, content_height=560)
    search = Gtk.SearchEntry(placeholder_text='Search…', margin_bottom=6)
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                          css_classes=['boxed-list'])
    rows = []
    for key, t, sub in items:
        row = Adw.ActionRow(title=esc(t), subtitle=esc(sub),
                            activatable=True, title_lines=1, subtitle_lines=1)
        row.add_suffix(Gtk.Image.new_from_icon_name('go-next-symbolic'))
        row._key = key
        row._hay = f'{t} {sub}'.lower()
        row.connect('activated', lambda r: (dlg.close(), on_pick(r._key)))
        listbox.append(row)
        rows.append(row)

    placeholder = Adw.StatusPage(
        title=empty or 'Nothing to show',
        icon_name='edit-find-symbolic', vexpand=True)
    placeholder.set_visible(not rows)

    def filter_rows(*_a):
        needle = search.get_text().strip().lower()
        for r in rows:
            r.set_visible(needle in r._hay)
    search.connect('search-changed', filter_rows)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                  margin_top=12, margin_bottom=12,
                  margin_start=12, margin_end=12)
    if subtitle:
        lbl = Gtk.Label(label=esc(subtitle), xalign=0, wrap=True,
                        css_classes=['dim-label'], margin_bottom=6)
        box.append(lbl)
    box.append(search)
    if rows:
        box.append(listbox)
    else:
        box.append(placeholder)
    sw = Gtk.ScrolledWindow(vexpand=True,
                            hscrollbar_policy=Gtk.PolicyType.NEVER)
    sw.set_child(box)
    view = Adw.ToolbarView()
    view.add_top_bar(Adw.HeaderBar())
    view.set_content(sw)
    dlg.set_child(view)
    dlg.present(parent)
    search.grab_focus()
    return dlg


def prompt_text(parent, heading, body, initial, on_accept, action='Save'):
    dlg = Adw.AlertDialog(heading=heading, body=body)
    entry = Gtk.Entry(text=initial, activates_default=True)
    dlg.set_extra_child(entry)
    dlg.add_response('cancel', 'Cancel')
    dlg.add_response('ok', action)
    dlg.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response('ok')
    dlg.connect('response', lambda _d, r: r == 'ok'
                and on_accept(entry.get_text().strip()))
    dlg.present(parent)
    entry.grab_focus()


# ------------------------------------------------------------------ wires --

class Wires(Gtk.DrawingArea):
    """The gutter between the two columns, with a curve per send.

    Positions are read straight off the live widget tree (`compute_bounds`
    against this area), so the curves follow the cards wherever the layout
    puts them — no bookkeeping to keep in sync, and nothing to get wrong when
    a card grows a stage.  If a card is not allocated yet the draw is retried
    once on idle, which is the normal case for the first frame after a
    rebuild.
    """

    def __init__(self, page):
        super().__init__()
        self.page = page
        self._retries = 0
        self.set_content_width(64)
        self.set_vexpand(True)
        self.set_can_target(False)      # never eats clicks meant for a card
        self.set_draw_func(self._draw)
        # Colours come out of the stylesheet rather than from
        # AdwStyleManager: on a machine with a third-party GTK theme the two
        # disagree, and a wire painted the manager's blue next to grey
        # buttons looks like a bug.  `pens` are two invisible widgets that
        # carry nothing but a css class and are read for their resolved
        # colour, so the curve runs from the source avatar's hue to the mix
        # avatar's hue and follows the theme.
        self._pen_src = self._pen_dst = None

    def set_pens(self, src, dst):
        self._pen_src, self._pen_dst = src, dst

    def _draw(self, _area, cr, w, h):
        pairs = self.page.wire_pairs()
        if not pairs:
            return
        fg = self.get_color()
        c_src = self._pen_src.get_color() if self._pen_src else fg
        c_dst = self._pen_dst.get_color() if self._pen_dst else fg
        for src, dst, live in pairs:
            ok1, r1 = src.compute_bounds(self)
            ok2, r2 = dst.compute_bounds(self)
            if not (ok1 and ok2):
                if self._retries < 8:
                    self._retries += 1
                    GLib.idle_add(self.queue_draw)
                return
            self._retries = 0
            y1 = r1.origin.y + r1.size.height / 2
            y2 = r2.origin.y + r2.size.height / 2
            if live:
                grad = cairo.LinearGradient(0, y1, w, y2)
                grad.add_color_stop_rgba(0, c_src.red, c_src.green,
                                         c_src.blue, 0.9)
                grad.add_color_stop_rgba(1, c_dst.red, c_dst.green,
                                         c_dst.blue, 0.9)
                cr.set_source(grad)
                cr.set_line_width(2.5)
                cr.set_dash([])
            else:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.22)
                cr.set_line_width(1.5)
                cr.set_dash([3.0, 4.0])
            cr.move_to(0, y1)
            cr.curve_to(w * 0.55, y1, w * 0.45, y2, w, y2)
            cr.stroke()
            cr.set_dash([])
            ends = ((0.0, y1, c_src), (float(w), y2, c_dst))
            for x, y, col in ends:
                if live:
                    cr.set_source_rgba(col.red, col.green, col.blue, 0.9)
                cr.arc(x, y, 3.0, 0, 6.2832)
                cr.fill()


# ----------------------------------------------------------- stage editor --

class StageDialog(Adw.Window):
    """Edit one stage.  The middle section changes with the stage kind; the
    placement controls (bypass, order, remove) are the same for all of them."""

    def __init__(self, window, page, strip, stage, on_done):
        super().__init__(title=f"{stage['name']}", transient_for=window,
                         modal=True, resizable=True,
                         default_width=560, default_height=680)
        self.window, self.page = window, page
        self.strip, self.stage, self.on_done = strip, stage, on_done
        self._band_rows: list = []
        # Closing the window and finishing through a button both end up here,
        # so the outcome is reported exactly once: without the flag, `close()`
        # inside _save would fire close-request and report a cancel, throwing
        # away the stage that had just been saved.
        self._handled = False
        self.connect('close-request', self._on_close)

        g = group('Stage')
        self.name_row = Adw.EntryRow(title='Name')
        self.name_row.set_text(stage.get('name', ''))
        g.add(self.name_row)

        self.bypass_row = Adw.SwitchRow(
            title='Bypass this stage',
            subtitle='Leaves it in the chain but takes it out of the signal. '
                     'The chain is rebuilt, so audio stops for a moment.')
        self.bypass_row.set_active(bool(stage.get('bypass')))
        g.add(self.bypass_row)

        idx = strip.stages.index(stage)
        move = Adw.ActionRow(title='Position in the chain',
                             subtitle=f'Stage {idx + 1} of {len(strip.stages)}')
        up = icon_button('go-up-symbolic', 'Move earlier',
                         lambda *_: self._move(-1))
        down = icon_button('go-down-symbolic', 'Move later',
                           lambda *_: self._move(1))
        up.set_sensitive(idx > 0)
        down.set_sensitive(idx < len(strip.stages) - 1)
        move.add_suffix(up)
        move.add_suffix(down)
        g.add(move)

        body = self._kind_group()

        save = Gtk.Button(label='Save', halign=Gtk.Align.END, margin_top=12,
                          css_classes=['suggested-action', 'pill'])
        save.connect('clicked', self._save)
        remove = Gtk.Button(label='Remove from chain', halign=Gtk.Align.START,
                            css_classes=['destructive-action', 'pill'])
        remove.connect('clicked', self._remove)
        actions = Gtk.Box(spacing=8, margin_top=12)
        actions.append(remove)
        actions.append(Gtk.Box(hexpand=True))
        actions.append(save)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24,
                      margin_start=18, margin_end=18)
        box.append(g)
        if body:
            box.append(body)
        box.append(actions)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=560)
        clamp.set_child(box)
        sw.set_child(clamp)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        self.set_content(view)

    # -- per-kind body ----------------------------------------------------
    def _kind_group(self):
        kind = self.stage.get('kind')
        p = self.stage.setdefault('params', {})
        if kind == 'eq':
            g = group('Bands', 'Each band is one filter. Frequency, gain and '
                               'Q can be changed while audio is playing.')
            add = icon_button('list-add-symbolic', 'Add band',
                              lambda *_: self._add_band())
            g.set_header_suffix(add)
            self.preamp = Adw.SpinRow(
                title='Preamp', subtitle='Overall trim in dB — go negative to '
                'leave headroom for boosted bands.',
                adjustment=_adj(-24, 24, float(p.get('preamp') or 0.0), 0.5, 3),
                digits=1)
            g.add(self.preamp)
            self.bands_group = g
            for b in (p.get('bands') or []):
                self._add_band(b)
            return g
        if kind == 'effect':
            g = group('Plugin')
            self.plug_row = Adw.ActionRow(title='Plugin')
            btn = Gtk.Button(label='Choose…', valign=Gtk.Align.CENTER)
            btn.connect('clicked', lambda *_: self._pick_plugin())
            self.plug_row.add_suffix(btn)
            self.plug_row.set_activatable_widget(btn)
            g.add(self.plug_row)
            self._refresh_plugin_row()
            return g
        if kind == 'xover':
            return self._xover_group(p)
        if kind == 'convolver':
            g = group('Impulse response',
                      'A WAV or SOFA file. Convolution is the most expensive '
                      'stage kind — expect it to add latency.')
            self.ir_row = Adw.ActionRow(title='File')
            btn = Gtk.Button(label='Browse…', valign=Gtk.Align.CENTER)
            btn.connect('clicked', lambda *_: self._pick_ir())
            self.ir_row.add_suffix(btn)
            self.ir_row.set_activatable_widget(btn)
            g.add(self.ir_row)
            self.gain_row = Adw.SpinRow(
                title='Gain', adjustment=_adj(0, 4, float(p.get('gain') or 1.0),
                                              0.05, 0.5), digits=2)
            g.add(self.gain_row)
            self._refresh_ir_row()
            return g
        return None

    # -- crossover --------------------------------------------------------
    # A band is two decisions, so it is two groups: what it keeps, and where
    # it goes.  The second one is what makes this more than an equalizer —
    # a band can leave on lanes the audio did not come in on, which is how
    # one strip feeds a subwoofer and a pair of satellites at once.

    def _xover_group(self, p):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        g = group('Band', 'Linkwitz-Riley filters: two bands crossing at the '
                          'same frequency add back up to a flat response.')
        self.mode_row = Adw.ComboRow(
            title='Keep', model=Gtk.StringList.new([m[1] for m in XOVER_MODES]))
        self.mode_row.set_selected(next(
            (i for i, m in enumerate(XOVER_MODES)
             if m[0] == (p.get('mode') or 'lowpass')), 0))
        g.add(self.mode_row)

        self.freq_row = Adw.SpinRow(
            title='Crossover (Hz)',
            adjustment=_adj(20, 20000, float(p.get('freq') or 80.0), 10, 100),
            digits=0)
        g.add(self.freq_row)
        self.freq_hi_row = Adw.SpinRow(
            title='Upper crossover (Hz)',
            subtitle='The top of the band.',
            adjustment=_adj(20, 20000, float(p.get('freq_hi') or 2000.0),
                            10, 100), digits=0)
        g.add(self.freq_hi_row)

        self.slope_row = Adw.ComboRow(
            title='Slope',
            subtitle='How sharply the band stops. Steeper keeps the bands out '
                     'of each other’s way; gentler sounds more natural through '
                     'the crossover.',
            model=Gtk.StringList.new([s[1] for s in XOVER_SLOPES]))
        self.slope_row.set_selected(next(
            (i for i, s in enumerate(XOVER_SLOPES)
             if s[0] == int(p.get('slope') or paths.DEFAULT_SLOPE)), 1))
        g.add(self.slope_row)
        box.append(g)

        a = group('Alignment', 'Drivers are rarely the same distance away or '
                               'wired the same way round — this is where that '
                               'is corrected.')
        self.xgain_row = Adw.SpinRow(
            title='Level (dB)',
            adjustment=_adj(-24, 12, float(p.get('gain') or 0.0), 0.5, 3),
            digits=1)
        a.add(self.xgain_row)
        self.xdelay_row = Adw.SpinRow(
            title='Delay (ms)',
            subtitle='1 ms ≈ 34 cm. Delay the closer driver, not the far one.',
            adjustment=_adj(0, paths.MAX_DELAY_S * 1000,
                            float(p.get('delay') or 0.0), 0.1, 1), digits=2)
        a.add(self.xdelay_row)
        self.xinvert_row = Adw.SwitchRow(
            title='Invert polarity',
            subtitle='Flips this band over. Try it if the crossover region '
                     'sounds hollow.')
        self.xinvert_row.set_active(bool(p.get('invert')))
        a.add(self.xinvert_row)
        box.append(a)

        layout = self.strip.out_layout
        c = group('Channels',
                  'Which lanes this band listens to, and which lanes it comes '
                  'out on. Sending it somewhere else leaves the lanes it came '
                  'from carrying the full range, so another band can take '
                  'them. To gain lanes the strip hasn’t got, widen its output '
                  'layout from the card menu.')
        self.read_chips = self._lane_chips(
            c, 'Listens to', layout, p.get('channels') or [])
        self.route_chips = self._lane_chips(
            c, 'Comes out on', layout, p.get('route') or [])
        box.append(c)

        def retitle(*_a):
            mode = XOVER_MODES[self.mode_row.get_selected()][0]
            self.freq_hi_row.set_visible(mode == 'bandpass')
            self.freq_row.set_title(
                'Lower crossover (Hz)' if mode == 'bandpass'
                else 'Crossover (Hz)')
            self.freq_row.set_subtitle(
                {'lowpass': 'Everything above this is cut.',
                 'highpass': 'Everything below this is cut.',
                 'bandpass': 'The bottom of the band.'}[mode])
        self.mode_row.connect('notify::selected', retitle)
        retitle()
        return box

    def _lane_chips(self, grp, title, layout, chosen):
        """A row of channel toggles; nothing ticked means "all of them"."""
        from ..backend.surround import SPEAKER_NAMES
        row = Adw.ActionRow(title=title, subtitle='')
        wrap = Adw.WrapBox(child_spacing=6, line_spacing=6,
                           valign=Gtk.Align.CENTER, margin_top=8,
                           margin_bottom=8)
        chips = {}
        for pos in layout:
            b = chip(pos, SPEAKER_NAMES.get(pos, pos), None,
                     active=pos in chosen, toggle=True)
            chips[pos] = b
            wrap.append(b)

        def note(*_a):
            on = [p for p, b in chips.items() if b.get_active()]
            row.set_subtitle('Every channel' if not on else ' · '.join(on))
        for b in chips.values():
            b.connect('toggled', note)
        note()
        row.add_suffix(wrap)
        grp.add(row)
        return chips

    def _collect_lanes(self, chips):
        return [p for p, b in chips.items() if b.get_active()]

    # -- eq bands ---------------------------------------------------------
    def _add_band(self, band=None):
        band = dict(band or {'on': True, 'type': 'PK', 'freq': 1000.0,
                             'gain': 0.0, 'q': 1.0})
        row = Adw.ExpanderRow(show_enable_switch=True)
        row.set_enable_expansion(bool(band.get('on', True)))
        type_row = Adw.ComboRow(
            title='Type',
            model=Gtk.StringList.new([t[1] for t in BAND_TYPES]))
        type_row.set_selected(next(
            (i for i, t in enumerate(BAND_TYPES)
             if t[0] == str(band.get('type', 'PK')).upper()), 0))
        freq = Adw.SpinRow(title='Frequency (Hz)',
                           adjustment=_adj(20, 20000, float(band['freq']),
                                           10, 100), digits=0)
        gain = Adw.SpinRow(title='Gain (dB)',
                           adjustment=_adj(-30, 30, float(band['gain']),
                                           0.5, 3), digits=1)
        q = Adw.SpinRow(title='Q', adjustment=_adj(0.1, 10, float(band['q']),
                                                   0.1, 1), digits=2)
        for r in (type_row, freq, gain, q):
            row.add_row(r)
        rm = icon_button('user-trash-symbolic', 'Remove band',
                         lambda *_: self._drop_band(row))
        row.add_suffix(rm)

        def retitle(*_a):
            t = BAND_TYPES[type_row.get_selected()][1]
            row.set_title(f'{t} · {int(freq.get_value())} Hz')
            row.set_subtitle(f'{gain.get_value():+.1f} dB · Q '
                             f'{q.get_value():.2f}')
        for r in (freq, gain, q):
            r.connect('notify::value', retitle)
        type_row.connect('notify::selected', retitle)
        retitle()
        self.bands_group.add(row)
        self._band_rows.append((row, type_row, freq, gain, q))

    def _drop_band(self, row):
        self._band_rows = [b for b in self._band_rows if b[0] is not row]
        self.bands_group.remove(row)

    def _collect_bands(self):
        out = []
        for row, tr, f, g, q in self._band_rows:
            out.append({'on': row.get_enable_expansion(),
                        'type': BAND_TYPES[tr.get_selected()][0],
                        'freq': round(f.get_value(), 2),
                        'gain': round(g.get_value(), 2),
                        'q': round(q.get_value(), 3)})
        return out

    # -- effect / convolver -----------------------------------------------
    def _refresh_plugin_row(self):
        p = self.stage['params']
        if p.get('plugin'):
            ports = f"{len(p.get('audio_in') or [])} in / " \
                    f"{len(p.get('audio_out') or [])} out"
            self.plug_row.set_subtitle(
                esc(f"{p.get('label') or p['plugin']} — {ports}"))
        else:
            self.plug_row.set_subtitle('No plugin chosen yet')

    def _pick_plugin(self):
        def loaded(all_p, error):
            if error or not all_p:
                self.window.toast('No LADSPA or LV2 plugins found')
                return
            items = []
            for pl in all_p:
                d = pl if isinstance(pl, dict) else vars(pl)
                ports = (f"{len(d.get('audio_in') or [])} in / "
                         f"{len(d.get('audio_out') or [])} out")
                items.append((d, d.get('name') or d.get('label') or '?',
                              f"{d.get('type', '')} · {ports}"))

            def picked(d):
                self.stage['params'].update({
                    'type': d.get('type'), 'plugin': d.get('plugin'),
                    'label': d.get('label'),
                    'audio_in': list(d.get('audio_in') or []),
                    'audio_out': list(d.get('audio_out') or [])})
                if not self.name_row.get_text().strip() or \
                        self.name_row.get_text().strip() == 'effect':
                    self.name_row.set_text(d.get('name') or d.get('label') or '')
                self._refresh_plugin_row()
            search_picker(self, 'Choose a plugin',
                          'Stereo plugins run once per channel pair, mono '
                          'plugins once per channel.', items, picked,
                          empty='No LADSPA or LV2 plugins found')
        async_call(_all_plugins, loaded)

    def _refresh_ir_row(self):
        f = self.stage['params'].get('filename')
        self.ir_row.set_subtitle(esc(f) if f else 'No file chosen yet')

    def _pick_ir(self):
        def got(path):
            self.stage['params']['filename'] = path
            self._refresh_ir_row()
        pick_file(self, 'Choose an impulse response', got)

    # -- actions ----------------------------------------------------------
    def _finish(self, saved):
        self._handled = True
        self.on_done(saved)
        self.close()

    def _on_close(self, *_a):
        if not self._handled:
            self._handled = True
            self.on_done(False)
        return False

    def _move(self, delta):
        i = self.strip.stages.index(self.stage)
        j = max(0, min(len(self.strip.stages) - 1, i + delta))
        if i != j:
            self.strip.stages.insert(j, self.strip.stages.pop(i))
            self._finish(True)

    def _remove(self, _b):
        self.strip.stages = [s for s in self.strip.stages
                             if s.get('id') != self.stage.get('id')]
        self._finish(True)

    def _save(self, _b):
        self.stage['name'] = self.name_row.get_text().strip() or \
            self.stage.get('kind', 'stage')
        self.stage['bypass'] = self.bypass_row.get_active()
        kind = self.stage.get('kind')
        p = self.stage.setdefault('params', {})
        if kind == 'eq':
            p['preamp'] = round(self.preamp.get_value(), 2)
            p['bands'] = self._collect_bands()
        elif kind == 'xover':
            mode = XOVER_MODES[self.mode_row.get_selected()][0]
            freq = round(self.freq_row.get_value(), 1)
            freq_hi = round(self.freq_hi_row.get_value(), 1)
            if mode == 'bandpass' and freq_hi <= freq:
                self.window.toast('The upper crossover has to sit above the '
                                  'lower one')
                return
            p.update({
                'mode': mode, 'freq': freq, 'freq_hi': freq_hi,
                'slope': XOVER_SLOPES[self.slope_row.get_selected()][0],
                'gain': round(self.xgain_row.get_value(), 2),
                'delay': round(self.xdelay_row.get_value(), 3),
                'invert': self.xinvert_row.get_active(),
                'channels': self._collect_lanes(self.read_chips),
                'route': self._collect_lanes(self.route_chips)})
        elif kind == 'convolver':
            p['gain'] = round(self.gain_row.get_value(), 3)
            if not p.get('filename'):
                self.window.toast('Choose an impulse response first')
                return
        elif kind == 'effect':
            if not p.get('plugin'):
                self.window.toast('Choose a plugin first')
                return
        self._finish(True)


# ------------------------------------------------------------------ page --

class PathsPage:
    def __init__(self, window):
        self.window = window
        self.volume_style = prefs.get('volume_style') or 'meter'
        self._strips: list = []
        self._states: dict = {}
        self._nodes: list = []
        self._streams: list = []
        self._vols: dict = {}
        self._cards: dict = {}          # strip.id -> card widget
        self._chips: dict = {}          # stage id -> chip widget
        self._rails: dict = {}          # strip.id -> chain rail widget
        self._timers: dict = {}         # strip.id -> pending apply
        self._editing: set = set()      # stage ids with an open dialog
        self._focus_stage = ''          # chip to re-focus after a rebuild
        self._focus_timer = 0
        self._poll = 0                  # periodic refresh while on screen
        self._busy = False              # a collect() is already in flight
        self._soon = 0                  # extra refresh after a unit restart
        self._dragging = False
        self._sig = None                # last rendered structural signature
        self._scanned = False           # catalog has asked what is installed

        self.widget = self._build()
        self.widget.connect('map', self._on_map)
        self.widget.connect('unmap', self._on_unmap)

    # ------------------------------------------------------------- shell --
    def _build(self):
        # toolbar: what exists, and how to add to it
        self.summary = Gtk.Label(xalign=0, hexpand=True,
                                 ellipsize=Pango.EllipsizeMode.END)
        self.summary.add_css_class('dim-label')
        # `can_shrink` lets a label ellipsize away to its icon instead of
        # setting a floor under the toolbar.  Every page shares one Gtk.Stack,
        # so a toolbar that cannot shrink is a minimum width charged to the
        # whole app — and three labelled buttons across is exactly the shape
        # that does it.
        add_src = Gtk.Button(css_classes=['suggested-action'],
                             tooltip_text='A source is where audio enters')
        add_src.set_child(Adw.ButtonContent(icon_name='list-add-symbolic',
                                            label='Source', can_shrink=True))
        add_src.connect('clicked', lambda *_: self._new_strip('source'))
        add_mix = Gtk.Button(tooltip_text='A mix feeds your output devices')
        add_mix.set_child(Adw.ButtonContent(icon_name='list-add-symbolic',
                                            label='Mix', can_shrink=True))
        add_mix.connect('clicked', lambda *_: self._new_strip('mix'))
        add_xov = Gtk.Button(
            tooltip_text='A crossover splits the audio by frequency and '
                         'sends each band to its own destination')
        add_xov.set_child(Adw.ButtonContent(icon_name='view-list-symbolic',
                                            label='Crossover',
                                            can_shrink=True))
        add_xov.connect('clicked', lambda *_: self._new_xover())
        # Its own colour, because it does something of a different order from
        # the two beside it: those add an empty strip, this one arrives with a
        # chain already in it.
        tpl = Gtk.Button(css_classes=['path-tpl-btn'],
                         tooltip_text='Start from a ready-made chain')
        tpl.set_child(Adw.ButtonContent(icon_name='view-grid-symbolic',
                                        label='Templates', can_shrink=True))
        tpl.connect('clicked', lambda *_: self._open_templates())
        more = Gtk.MenuButton(icon_name='view-more-symbolic',
                              css_classes=['flat'], tooltip_text='More')
        more.set_popover(menu_popover([
            ('document-open-symbolic', 'Import a path…', self._import),
            ('view-refresh-symbolic', 'Refresh', self.refresh),
            None,
            ('user-trash-symbolic', 'Delete strips…', self._manage_strips,
             'destructive-flat'),
        ]))
        toolbar = Gtk.Box(spacing=8, css_classes=['path-toolbar'])
        toolbar.append(self.summary)
        toolbar.append(tpl)
        toolbar.append(add_xov)
        toolbar.append(add_mix)
        toolbar.append(add_src)
        toolbar.append(more)
        self.toolbar = toolbar

        # quick setup — the whole page until something exists
        self.quick = self._quick_setup()

        # the board: sources | wires | mixes
        self.col_src, self.src_body = self._column(
            'Sources', 'Add a source', lambda: self._new_strip('source'),
            'source')
        self.col_mix, self.mix_body = self._column(
            'Mixes', 'Add a mix', lambda: self._new_strip('mix'), 'mix')
        self.wires = Wires(self)
        self.board = Gtk.Box(spacing=0)
        self.board.append(self.col_src)
        self.board.append(self.wires)
        self.board.append(self.col_mix)
        pens = [Gtk.Label(css_classes=[c], visible=False)
                for c in ('path-wire-src', 'path-wire-dst')]
        for pen in pens:
            self.board.append(pen)
        self.wires.set_pens(*pens)
        sg = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        sg.add_widget(self.col_src)
        sg.add_widget(self.col_mix)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=18, margin_bottom=36,
                      margin_start=16, margin_end=16)
        # Crossovers sit above the two columns, full width, because that is
        # what they are: not a source and not a mix, but the thing in between
        # that takes what plays and hands each band of it to a destination.
        # They connect to devices rather than to strips, so drawing them as a
        # third column with no wires reaching it would say the wrong thing.
        self.xover_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                  spacing=12)
        box.append(toolbar)
        box.append(self.quick)
        box.append(self.xover_body)
        box.append(self.board)
        # 1600 let each column grow past 700px on a wide screen, which a card
        # has nothing to do with: the name sits in the first third and the
        # rest is the rail stretching out empty.  Cards look built rather than
        # stretched at around 600, and long device names still wrap instead of
        # being cut.
        clamp = Adw.Clamp(maximum_size=1320, tightening_threshold=1100)
        clamp.set_child(box)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vexpand=True)
        sw.set_child(clamp)

        # Below ~720px of board the two columns stop being readable, so they
        # stack and the gutter goes away — same information, one column.
        #
        # The bin wraps the *scroller*, and that placement is the whole point.
        # AdwBreakpointBin reports a minimum size of zero by design, so that it
        # can be squeezed far enough for a breakpoint to apply; with the bin
        # inside the scrolled window the scroller therefore believed the entire
        # board fitted in the bin's 200px height request, never showed a
        # scrollbar, and simply clipped everything past the bottom of the
        # window once there were more than a few strips.  Outside it, the
        # scroller measures the real content again and the bin only ever sees
        # the space the page actually has.
        #
        # The condition moved 720 -> 752 with it: it used to be tested against
        # the board's own width, which is the clamped content minus this box's
        # 16px margins, and is now tested against the width of the whole page.
        # The requests are the bin's whole minimum size, since its own measure
        # reports zero.  352 is the page minimum this page has always had and
        # is checked by the harness; the height is only there because the bin
        # needs one, and is kept at roughly the toolbar so the page does not
        # start dictating how short the window may be.
        bin_ = Adw.BreakpointBin(width_request=352, height_request=64)
        bin_.set_child(sw)
        bp = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse('max-width: 752px'))
        bp.add_setter(self.board, 'orientation', Gtk.Orientation.VERTICAL)
        bp.add_setter(self.board, 'spacing', 24)
        bp.add_setter(self.wires, 'visible', False)
        bin_.add_breakpoint(bp)
        self.scroller = sw
        return bin_

    def _column(self, title, tooltip, on_add, role):
        head = Gtk.Box(spacing=8)
        head.append(micro(title, role))
        count = Gtk.Label(xalign=0, css_classes=['path-count'], hexpand=True,
                          valign=Gtk.Align.CENTER)
        head.append(count)
        head.append(add_button(tooltip, lambda *_: on_add()))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        # The gaps between cards, and the space around the "add" placeholder,
        # send a card to the end of the column.
        drop_target(body, StripDrag,
                    lambda p, _x, _y: self._drop_strip_end(p, role),
                    on_motion=lambda p, _x, _y: p.role == role,
                    on_leave=None)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      hexpand=True, valign=Gtk.Align.START)
        col.append(head)
        col.append(body)
        col._count = count
        return col, body

    # ------------------------------------------------------- empty board --
    # Nothing is on screen and nothing has been decided yet, so this is the
    # page's one chance to answer "what is this for?".  It answers with the
    # things themselves rather than with an explanation: three complete
    # arrangements, then the plain single strips, then the chains people
    # actually run.  Everything here builds ordinary strips, so none of it is
    # a door the user cannot walk back out of.

    RECIPES = (
        ('format-justify-fill-symbolic', 'Equalize everything',
         'One equalizer between every app and your current output.',
         '_quick_eq_all'),
        ('applications-multimedia-symbolic', 'Put effects on one app',
         'Send a single app through a plugin chain, leaving the rest of your '
         'audio alone.', '_quick_app_fx'),
        ('camera-video-symbolic', 'Speakers and a stream mix',
         'One chain into your speakers, a second into a virtual output that '
         'OBS or Discord can capture.', '_quick_stream'),
        ('view-list-symbolic', 'Subwoofer and satellites',
         'A crossover: the low band goes to one output, everything above it '
         'to another. Your output stays what it is — nothing new to select.',
         '_quick_crossover'),
        ('audio-headphones-symbolic', 'Headphones and speakers',
         'A speaker mix carrying the audio and a headphone mix with its own '
         'tilt waiting beside it. Switch with one click.',
         '_quick_two_outputs'),
    )

    def _quick_setup(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        # Its own registry, not shared with the dialog's: a dialog is thrown
        # away when it closes, and a scan landing afterwards must not reach
        # into cards that no longer exist.
        cards: dict = {}
        box.append(Gtk.Label(label='Start with a ready-made path', xalign=0,
                             css_classes=['title-4']))
        box.append(Gtk.Label(
            label='Each of these builds a complete path in one step. You can '
                  'take it apart afterwards — they are ordinary sources and '
                  'mixes.',
            xalign=0, wrap=True, css_classes=['dim-label']))
        box.append(self._card_grid([
            catalog_card(icon, title, sub, getattr(self, fn),
                         badge='Complete path', tone='recipe')
            for icon, title, sub, fn in self.RECIPES]))

        box.append(self._section('One strip at a time',
                                 'A single source or mix with its chain '
                                 'already filled in. Add as many as you like '
                                 'and wire them together.'))
        box.append(self._card_grid(
            [self._template_card(t, cards)
             for t in path_templates.starters()]))

        box.append(self._section(
            'Advanced',
            'Chains built the way they are built for broadcast, streaming and '
            'monitoring. Anything needing a plugin this machine does not have '
            'is left out and named when it happens.'))
        box.append(self._card_grid(
            [self._template_card(t, cards)
             for t in path_templates.advanced()]))
        # Not scanned here: this runs while the window is being built, and
        # every page is built whether or not it is ever opened.  Plugin
        # discovery dlopens every LADSPA library on the system, which is not
        # something to do on the way to the Dashboard.  `_render` asks for it
        # the first time these cards are actually on screen.
        self._tpl_registry = cards
        return box

    def _section(self, title, subtitle):
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       margin_top=16)
        head.append(micro(title))
        head.append(Gtk.Label(label=subtitle, xalign=0, wrap=True,
                              css_classes=['dim-label', 'caption']))
        return head

    def _card_grid(self, cards):
        """A grid that reflows instead of scrolling sideways.

        `natural_line_length` is what decides how many cards a line holds, so
        the same catalog is three across in a window and two in a dialog
        without either being told how wide it is."""
        wrap = Adw.WrapBox(child_spacing=12, line_spacing=12,
                           natural_line_length=1200,
                           justify=Adw.JustifyMode.FILL)
        for c in cards:
            wrap.append(c)
        return wrap

    def _template_card(self, tpl, registry, on_pick=None):
        card = catalog_card(
            tpl.icon, tpl.title, tpl.blurb,
            (lambda t=tpl: on_pick(t)) if on_pick
            else (lambda t=tpl: self._load_template(t)),
            chain=tpl.chain,
            badge='Source' if tpl.role == 'source' else 'Mix',
            tone=tpl.role)
        card.set_tooltip_text(tpl.detail or tpl.blurb)
        card._tpl = tpl
        registry[tpl.id] = card
        return card

    def _scan_plugins(self, registry):
        """Find out what this machine can actually build, in the background.

        Discovery dlopens every LADSPA library and walks every LV2 bundle, so
        it is far too slow to hold a card up: the catalog draws immediately
        and the cards that turn out to be short of a plugin say so when the
        answer arrives.
        """
        def done(installed, error):
            if error or not installed:
                return
            for tid, card in registry.items():
                tpl = path_templates.by_id(tid)
                if tpl is None:
                    continue
                gone = path_templates.missing_steps(tpl, installed)
                if not gone:
                    continue
                card._note.set_label(esc(
                    'Not installed here: ' + ', '.join(gone) +
                    ' — built without ' + ('them' if len(gone) > 1 else 'it')))
                card._note.set_visible(True)
        async_call(_all_plugins, done)

    # ------------------------------------------------------------ refresh --
    def _on_map(self, *_a):
        self.refresh()
        if not self._poll:
            self._poll = GLib.timeout_add_seconds(POLL_SEC, self._tick)

    def _on_unmap(self, *_a):
        if self._poll:
            GLib.source_remove(self._poll)
            self._poll = 0

    def _tick(self):
        self.refresh()
        return True

    def refresh_soon(self):
        """One extra refresh a moment after a strip's unit was restarted.

        `paths.apply` returns once systemd reports the unit started, which is
        earlier than the graph has it: the node is not published, relinked and
        playing again for a fraction of a second more.  Refreshing on that
        boundary is what left a card with no volume row and "Nothing playing
        here yet" — a true reading of a graph that was still coming back.  The
        poll would heal it either way; this just makes it quick.
        """
        if self._soon:
            return

        def fire():
            self._soon = 0
            self.refresh()
            return False
        self._soon = GLib.timeout_add(SETTLE_MS, fire)

    def refresh(self):
        if self._busy:
            return
        self._busy = True

        def collect():
            dump = pw.pw_dump()
            # Inserts included: this page draws the strips themselves, and an
            # inserted strip still has a volume and a level meter to show.
            nodes = pw.list_audio_nodes(dump, inserts=True)
            streams = [s for s in pw.list_streams(dump) if s.is_playback
                       and not s.props.get('node.name', '').startswith('pwctl.')]
            strips = paths.list_strips()
            states = {s.id: paths.status(s) for s in strips}
            return strips, states, nodes, streams
        async_call(collect, self._apply)

    def _signature(self):
        """Everything on the board that a rebuild would change.

        Deliberately *not* volume or mute: those arrive on every poll, the
        controls already show them, and treating them as structure would tear
        the card down under a slider the user is dragging.  They are the one
        thing the poll leaves alone.
        """
        return (
            tuple((s.id, s.role, s.order, s.name, s.kind, s.enabled,
                   s.channels, s.out_channels, tuple(s.sends),
                   tuple(s.outputs), s.mode, s.insert_into,
                   tuple((b.get('id'), b.get('name'), b.get('lo'),
                          b.get('hi'), b.get('mute'), tuple(b.get('outputs')
                                                            or ()))
                         for b in s.bands),
                   tuple((st.get('id'), st.get('name'), st.get('kind'),
                          bool(st.get('bypass'))) for st in s.stages))
                  for s in self._strips),
            tuple(sorted(self._states.items())),
            tuple(sorted((n.name, n.id, n.serial) for n in self._nodes)),
            tuple(sorted((s.id, s.target_id, s.name) for s in self._streams)),
        )

    def _apply(self, payload, error):
        self._busy = False
        if error or payload is None:
            return
        self._strips, self._states, self._nodes, self._streams = payload
        # A rebuild replaces every card, which cancels a drag in flight, closes
        # the dialog's parent chip and restarts the level meters.  The poll
        # exists to catch the graph changing underneath, so it only rebuilds
        # when something it draws actually differs.
        if self._signature() == self._sig or self._dragging or self._editing:
            return
        self._render()

    def _render(self):
        """Rebuild the board from what is already in memory.

        Split out of the refresh so a rearrangement shows up the instant it
        happens: dragging a stage changes a list and redraws, and only the
        rebuild of the actual audio graph waits behind `pw-dump`.

        The signature is taken here rather than in `_apply` so it always
        describes what is actually on screen, including the local edits that
        render straight from memory without going near `pw-dump`.
        """
        self._sig = self._signature()
        self._vols = {}
        self._cards = {}
        self._chips = {}
        self._rails = {}
        for body in (self.src_body, self.mix_body, self.xover_body):
            child = body.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                body.remove(child)
                child = nxt

        srcs = paths.sources(self._strips)
        mixes = paths.mixes(self._strips)
        xovers = paths.crossovers(self._strips)
        for x in xovers:
            card = self._xover_card(x)
            self._cards[x.id] = card
            self.xover_body.append(card)
        empty = not self._strips
        self.quick.set_visible(empty)
        self.board.set_visible(not empty)
        if empty and not self._scanned:
            self._scanned = True
            self._scan_plugins(self._tpl_registry)
        self.col_src._count.set_label(str(len(srcs)) if srcs else '')
        self.col_mix._count.set_label(str(len(mixes)) if mixes else '')
        self._set_summary(srcs, mixes)
        if empty:
            return

        for s in srcs:
            card = self._strip_card(s, mixes)
            self._cards[s.id] = card
            self.src_body.append(card)
        self.src_body.append(self._ghost(
            'Add a source', 'An app, a microphone, or everything',
            lambda: self._new_strip('source')))
        for m in mixes:
            card = self._strip_card(m, mixes)
            self._cards[m.id] = card
            self.mix_body.append(card)
        self.mix_body.append(self._ghost(
            'Add a mix', 'A chain of its own, feeding real devices',
            lambda: self._new_strip('mix')))
        GLib.idle_add(self.wires.queue_draw)
        chip = self._chips.get(self._focus_stage)
        if chip is not None:
            # Keyboard reordering only works if the stage keeps the focus it
            # was moved with; the chip it had is gone by now.  `grab_focus`
            # returns True, which as an idle callback means "call me again" —
            # hence the wrapper rather than passing the method itself.
            GLib.idle_add(lambda c=chip: (c.grab_focus(), False)[1])

    def _set_summary(self, srcs, mixes):
        if not self._strips:
            self.summary.set_label(
                'Nothing routed yet — audio takes its usual path.')
            return
        outs = len({o for m in mixes for o in m.outputs})
        live = sum(1 for s in self._strips
                   if s.enabled and self._states.get(s.id) == 'active')
        bits = [f'{len(srcs)} source' + ('s' if len(srcs) != 1 else ''),
                f'{len(mixes)} mix' + ('es' if len(mixes) != 1 else '')]
        if outs:
            bits.append(f'{outs} output' + ('s' if outs != 1 else ''))
        bits.append(f'{live} running')
        self.summary.set_label(' · '.join(bits))

    def wire_pairs(self):
        """(source card, mix card, live) for every send — read by Wires."""
        out = []
        for s in paths.sources(self._strips):
            src_card = self._cards.get(s.id)
            if src_card is None:
                continue
            src_live = s.enabled and self._states.get(s.id) == 'active'
            by_id = {m.id: m for m in paths.mixes(self._strips)}
            for mid in s.sends:
                dst = self._cards.get(mid)
                if dst is None:
                    continue
                mix = by_id.get(mid)
                live = bool(src_live and mix and mix.enabled
                            and self._states.get(mid) == 'active')
                out.append((src_card, dst, live))
        return out

    def _ghost(self, title, subtitle, on_click):
        btn = Gtk.Button(css_classes=['path-ghost'])
        inner = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        inner.append(Gtk.Image.new_from_icon_name('list-add-symbolic'))
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        labels.append(Gtk.Label(label=title, xalign=0,
                                css_classes=['heading']))
        labels.append(Gtk.Label(label=subtitle, xalign=0,
                                css_classes=['dim-label', 'caption']))
        inner.append(labels)
        btn.set_child(inner)
        btn.connect('clicked', lambda *_: on_click())
        return btn

    # --------------------------------------------------------- strip card --
    def _node_for(self, strip):
        return next((n for n in self._nodes if n.name == strip.node_name), None)

    def _device_label(self, node_name):
        n = next((x for x in self._nodes if x.name == node_name), None)
        return n.description if n else node_name

    def _strip_card(self, strip, mixes):
        state = self._states.get(strip.id, 'inactive')
        running = strip.enabled and state == 'active'
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       css_classes=['path-card'])
        # Two independent facts, two classes: which column it belongs to (its
        # hue, which never changes) and whether it is running (how strongly
        # that hue is carried).
        card.add_css_class(f'role-{strip.role}')
        card.add_css_class('live' if running else 'idle')
        card.append(self._card_head(strip, state, running, card))
        if running:
            vol = self._volume_box(strip)
            if vol is not None:
                card.append(vol)
        card.append(self._rail(strip))
        if strip.role == 'source':
            card.append(self._sends_box(strip, mixes))
            card.append(self._apps_box(strip))
        else:
            card.append(self._outputs_box(strip))

        # A card takes three kinds of drop.  Another card of the same role
        # means "sit here"; one from the opposite column means "wire us
        # together", which is the same fact the curve in the gutter draws.  A
        # stage lands at the end of this chain, and an app is relinked to play
        # through this strip.
        drop_target(card, StripDrag,
                    lambda p, _x, y: self._drop_strip(p, strip, y),
                    on_motion=lambda p, _x, y: self._card_hover(card, p,
                                                                strip, y),
                    on_leave=lambda: clear_drop(card))
        drop_target(card, StageDrag,
                    lambda p, _x, _y: self._drop_stage(p, strip,
                                                       len(strip.stages)),
                    on_motion=lambda p, _x, _y: self._card_into(card, True),
                    on_leave=lambda: clear_drop(card))
        drop_target(card, StreamDrag,
                    lambda p, _x, _y: self._drop_stream(p, strip),
                    on_motion=lambda p, _x, _y: self._card_into(
                        card, self._node_for(strip) is not None),
                    on_leave=lambda: clear_drop(card))
        return card

    # -- drop feedback -----------------------------------------------------
    def _begin_drag(self, kind, strip):
        """Mark everything that would accept what has just been picked up.

        The indicator under the pointer says "it lands here"; this one says
        "here is possible at all", which is the question a drag actually opens
        with.  Two different classes because both can be on screen at once.
        """
        self._end_drag()
        # A rebuild mid-drag destroys the widget being carried, so the poll
        # holds off until the gesture is over.
        self._dragging = True
        if kind == 'stage':
            for rail in self._rails.values():
                rail.add_css_class('drop-ready')
            return
        if kind == 'stream':
            for s in self._strips:
                card = self._cards.get(s.id)
                if card is not None and self._node_for(s) is not None:
                    card.add_css_class('drop-ready')
            return
        for s in self._strips:                      # a card being rearranged
            card = self._cards.get(s.id)
            if card is None or s.id == strip.id:
                continue
            card.add_css_class('drop-ready' if s.role == strip.role
                               else 'link-ready')

    def _end_drag(self):
        # Safe to clear unconditionally: `_begin_drag` calls this to sweep up
        # leftovers *before* it raises the flag.
        self._dragging = False
        for w in (*self._rails.values(), *self._cards.values()):
            w.remove_css_class('drop-ready')
            w.remove_css_class('link-ready')
            clear_drop(w)

    def _card_hover(self, card, payload, strip, y):
        clear_drop(card)
        if payload.strip_id == strip.id:
            return False
        if payload.role != strip.role:
            card.add_css_class('link-into')     # a send, not a rearrangement
            return True
        card.add_css_class('drop-before' if y < card.get_height() / 2
                           else 'drop-after')
        return True

    def _card_into(self, card, ok):
        clear_drop(card)
        if ok:
            card.add_css_class('drop-into')
        return ok

    def _card_head(self, strip, state, running, card):
        head = Gtk.Box(spacing=10, css_classes=['path-head'])
        kind = strip.kind if strip.role == 'source' else 'mix'
        icon = KIND_ICON.get(strip.kind, 'audio-card-symbolic') \
            if strip.role == 'source' else 'audio-speakers-symbolic'
        av = avatar(icon, kind)
        head.append(av)

        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                        hexpand=True, valign=Gtk.Align.CENTER)
        line = Gtk.Box(spacing=6)
        title = Gtk.Label(label=esc(strip.name), xalign=0,
                          css_classes=['heading'],
                          ellipsize=Pango.EllipsizeMode.END)
        line.append(title)
        # A split strip carries two layouts, and which one it comes out as is
        # the more surprising of the two, so both are on the tag.
        split = strip.out_channels != strip.channels
        tag = Gtk.Label(
            label=(f'{_layout_name(strip.channels)} → '
                   f'{_layout_name(strip.out_channels)}') if split
            else _layout_name(strip.channels),
            css_classes=['path-tag'], valign=Gtk.Align.CENTER)
        tag.set_tooltip_text(
            f'In: {" ".join(strip.positions)}\n'
            f'Out: {" ".join(strip.out_layout)}' if split
            else f'{strip.channels} channels: ' + ' '.join(strip.positions))
        line.append(tag)
        if strip.enabled and state == 'failed':
            line.append(pill('failed', state_style(state)))
        names.append(line)
        sub = Gtk.Label(label=esc(self._card_subtitle(strip)), xalign=0,
                        css_classes=['dim-label', 'caption'],
                        ellipsize=Pango.EllipsizeMode.END)
        names.append(sub)
        head.append(names)

        dot = Gtk.Box(css_classes=['path-dot'], valign=Gtk.Align.CENTER)
        if running:
            dot.add_css_class('on')
        elif strip.enabled and state == 'failed':
            dot.add_css_class('err')
        elif strip.enabled:
            dot.add_css_class('busy')
        dot.set_tooltip_text(state if strip.enabled else 'switched off')
        head.append(dot)

        sw = Gtk.Switch(active=strip.enabled, valign=Gtk.Align.CENTER,
                        tooltip_text='Turn this strip on or off')
        sw.connect('state-set', self._toggle, strip)
        head.append(sw)

        more = Gtk.MenuButton(icon_name='view-more-symbolic',
                              css_classes=['flat'], valign=Gtk.Align.CENTER,
                              tooltip_text='More')
        more.set_popover(menu_popover(self._strip_menu(strip)))
        head.append(more)

        # The head is the card's handle: it is the one band with nothing in it
        # that takes a press of its own, so the switch and the menu keep
        # working while the card as a whole can be picked up and moved.
        head.set_tooltip_text('Drag to rearrange, or onto the other column '
                              'to connect')
        for w in (av, names):
            w.set_cursor_from_name('grab')
        drag_source(head, lambda s=strip: StripDrag(s.id, s.role),
                    icon=card, offset=(12, 12),
                    on_begin=lambda: self._begin_drag('strip', strip),
                    on_end=self._end_drag)

        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect('pressed', lambda _g, _n, x, y: context_menu(
            head, self._strip_menu(strip), x, y))
        head.add_controller(click)
        return head

    def _strip_menu(self, strip):
        return [
            ('document-edit-symbolic', 'Rename…',
             lambda s=strip: self._rename(s)),
            ('media-playlist-repeat-symbolic', 'Channel layout…',
             lambda s=strip: self._pick_layout(s)),
            ('object-flip-horizontal-symbolic', 'Output layout…',
             lambda s=strip: self._pick_out_layout(s)),
            ('insert-link-symbolic', 'How it connects…',
             lambda s=strip: self._pick_mode(s)),
            None,
            ('edit-copy-symbolic', 'Duplicate',
             lambda s=strip: self._duplicate(s)),
            ('document-save-symbolic', 'Export…',
             lambda s=strip: self._export(s)),
            None,
            ('user-trash-symbolic', 'Delete',
             lambda s=strip: self._delete(s), 'destructive-flat'),
        ]

    def _card_subtitle(self, strip):
        if strip.role == 'source':
            by_id = {m.id: m for m in paths.mixes(self._strips)}
            dest = ', '.join(by_id[m].name for m in strip.sends if m in by_id)
            base = KIND_BLURB.get(strip.kind, 'A source')
            return f'{base} → {dest}' if dest \
                else f'{base} → the default output'
        # An inserted mix has no feeder and never will: it sits inside the
        # device's own path.  Saying "nothing feeds this" would read as a
        # fault, so each mode states what it is instead.
        if strip.mode == 'insert':
            dev = strip.insert_device()
            return ('Inside ' + self._device_label(dev)) if dev \
                else 'Inside the default output'
        if strip.mode == 'tap':
            src = next((s.name for s in self._strips
                        if s.node_name == strip.tap_source), '')
            dev = strip.insert_device()
            out = self._device_label(dev) if dev else 'the default output'
            return f'Copy of {src} → {out}' if src else f'A copy → {out}'
        feeders = [s.name for s in paths.sources(self._strips)
                   if strip.id in s.sends]
        return ('Fed by ' + ', '.join(feeders)) if feeders \
            else 'Nothing feeds this yet'

    # ------------------------------------------------------ crossover card --
    # One card, one table: a row per band, each row saying what it keeps and
    # where that goes.  The whole point of the object is that those two facts
    # sit side by side and can be changed independently, so the card refuses
    # to hide either of them behind a dialog.

    def _xover_card(self, strip):
        state = self._states.get(strip.id, 'inactive')
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       css_classes=['path-card', 'k-xover'])

        head = Gtk.Box(spacing=10)
        head.append(avatar('view-list-symbolic', 'xover'))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                        hexpand=True, valign=Gtk.Align.CENTER)
        line = Gtk.Box(spacing=6)
        line.append(Gtk.Label(label=esc(strip.name), xalign=0,
                              css_classes=['heading']))
        line.append(Gtk.Label(label=f'{len(strip.bands)} bands',
                              css_classes=['path-tag'],
                              valign=Gtk.Align.CENTER))
        if strip.enabled and state == 'failed':
            line.append(pill('failed', state_style(state)))
        names.append(line)
        dev = strip.insert_into
        names.append(Gtk.Label(
            label=esc('In front of ' + (self._device_label(dev) if dev
                                        else 'the default output')),
            xalign=0, css_classes=['dim-label', 'caption'],
            ellipsize=Pango.EllipsizeMode.END))
        head.append(names)

        dot = Gtk.Box(css_classes=['path-dot'], valign=Gtk.Align.CENTER)
        if strip.enabled and state == 'active':
            dot.add_css_class('on')
        elif strip.enabled and state == 'failed':
            dot.add_css_class('err')
        elif strip.enabled:
            dot.add_css_class('busy')
        head.append(dot)
        sw = Gtk.Switch(active=strip.enabled, valign=Gtk.Align.CENTER,
                        tooltip_text='Turn this crossover on or off')
        sw.connect('state-set', self._toggle, strip)
        head.append(sw)
        more = Gtk.MenuButton(icon_name='view-more-symbolic',
                              css_classes=['flat'], valign=Gtk.Align.CENTER)
        more.set_popover(menu_popover([
            ('document-edit-symbolic', 'Rename…',
             lambda s=strip: self._rename(s)),
            ('audio-speakers-symbolic', 'Sits in front of…',
             lambda s=strip: self._pick_insert_into(s)),
            None,
            ('user-trash-symbolic', 'Delete',
             lambda s=strip: self._delete(s), 'destructive-flat'),
        ]))
        head.append(more)
        card.append(head)

        for band in strip.bands:
            card.append(self._band_row(strip, band))

        add = Gtk.Button(css_classes=['flat', 'path-ghost'])
        inner = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        inner.append(Gtk.Image.new_from_icon_name('list-add-symbolic'))
        inner.append(Gtk.Label(label='Add a band'))
        add.set_child(inner)
        add.connect('clicked', lambda *_: self._add_band_row(strip))
        card.append(add)
        return card

    def _band_row(self, strip, band):
        row = Gtk.Box(spacing=8, css_classes=['path-band'])

        edit = Gtk.Button(css_classes=['path-stage', 'k-xover'],
                          valign=Gtk.Align.CENTER,
                          tooltip_text='Change what this band keeps')
        inner = Gtk.Box(spacing=6)
        inner.append(Gtk.Image.new_from_icon_name('view-list-symbolic'))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.append(Gtk.Label(label=esc(band.get('name') or 'Band'), xalign=0))
        text.append(Gtk.Label(label=paths.band_label(band), xalign=0,
                              css_classes=['dim-label', 'caption']))
        inner.append(text)
        edit.set_child(inner)
        if band.get('mute'):
            edit.add_css_class('path-stage-off')
        edit.connect('clicked', lambda *_: self._edit_band(strip, band))
        row.append(edit)

        row.append(Gtk.Image.new_from_icon_name('go-next-symbolic'))

        chips = []
        for dev in band.get('outputs') or []:
            chips.append(chip(self._device_label(dev),
                              'Stop sending this band here',
                              lambda _b, s=strip, bd=band, d=dev:
                              self._drop_band_dest(s, bd, d),
                              icon='window-close-symbolic', active=True))
        chips.append(chip('Add a destination', 'Where this band is played',
                          lambda _b, s=strip, bd=band:
                          self._add_band_dest(s, bd),
                          icon='list-add-symbolic'))
        wrap = Adw.WrapBox(child_spacing=6, line_spacing=6, hexpand=True,
                           valign=Gtk.Align.CENTER)
        for c in chips:
            wrap.append(c)
        row.append(wrap)

        rm = icon_button('user-trash-symbolic', 'Remove this band',
                         lambda *_: self._drop_band(strip, band))
        rm.set_valign(Gtk.Align.CENTER)
        row.append(rm)
        return row

    # -- crossover editing -------------------------------------------------
    def _new_xover(self):
        def make(name):
            if not name:
                return
            dev = self._default_sink_name()
            x = paths.new_strip(name, 'xover', insert_into=dev)
            x.enabled = False
            paths.save_meta(x)
            self.refresh()
            self.window.toast(f'“{name}” added — give each band a destination, '
                              'then turn it on')
        prompt_text(self.window, 'New crossover',
                    'It sits in front of an output and hands each band of the '
                    'audio to a destination of your choosing.',
                    'Crossover', make, action='Add')

    def _add_band_row(self, strip):
        # A new band starts where the last one ended, so adding rows walks up
        # the spectrum instead of stacking bands on top of each other.
        top = max([float(b.get('hi') or 0) for b in strip.bands] or [0.0])
        edge = max([float(b.get('lo') or 0) for b in strip.bands] + [top])
        strip.bands = [*strip.bands,
                       paths.new_band(f'Band {len(strip.bands) + 1}',
                                      lo=edge or 80.0)]
        self._save_and_apply(strip)

    def _drop_band(self, strip, band):
        strip.bands = [b for b in strip.bands if b['id'] != band['id']]
        self._save_and_apply(strip)

    def _drop_band_dest(self, strip, band, dev):
        band['outputs'] = [d for d in band.get('outputs') or [] if d != dev]
        self._save_and_apply(strip)

    def _add_band_dest(self, strip, band):
        taken = set(band.get('outputs') or [])
        items = [(n.name, n.description, n.name) for n in self._nodes
                 if n.is_sink and n.name not in taken
                 and not n.name.startswith('pwctl.')]

        def picked(dev):
            band['outputs'] = [*(band.get('outputs') or []), dev]
            self._save_and_apply(strip)
        search_picker(self.window, f"Where does “{band.get('name')}” play?",
                      'The band is sent here. Give a band several '
                      'destinations and it plays on all of them; give two '
                      'bands the same one and they are summed.',
                      items, picked, empty='No output devices found')

    def _pick_insert_into(self, strip):
        """The output the crossover stands in front of.

        Everything heading for that device goes through the crossover first,
        which is what makes it intermediate: the device stays the output apps
        play to, and the bands decide where the audio actually ends up.
        """
        items = [('', 'The default output',
                  'Follows whatever output is current')]
        items += [(n.name, n.description, n.name) for n in self._nodes
                  if n.is_sink and not n.name.startswith('pwctl.')]

        def picked(dev):
            strip.insert_into = dev
            self._save_and_apply(strip)
        search_picker(self.window, 'Sits in front of',
                      'Audio on its way to this output is split by the '
                      'crossover before it gets there.', items, picked)

    def _edit_band(self, strip, band):
        dlg = Adw.Window(title=band.get('name') or 'Band',
                         transient_for=self.window, modal=True,
                         default_width=520, default_height=620)
        g = group('Band', 'Leave an edge at 0 for "no limit on that side": '
                          '0 to 80 is a subwoofer band, 80 to 0 is everything '
                          'above it.')
        name = Adw.EntryRow(title='Name')
        name.set_text(band.get('name') or '')
        g.add(name)
        lo = Adw.SpinRow(title='From (Hz)', subtitle='0 = no lower limit',
                         adjustment=_adj(0, 20000, float(band.get('lo') or 0),
                                         10, 100), digits=0)
        hi = Adw.SpinRow(title='To (Hz)', subtitle='0 = no upper limit',
                         adjustment=_adj(0, 20000, float(band.get('hi') or 0),
                                         10, 100), digits=0)
        g.add(lo)
        g.add(hi)
        slope = Adw.ComboRow(
            title='Slope', subtitle='How sharply the band stops.',
            model=Gtk.StringList.new([s[1] for s in XOVER_SLOPES]))
        slope.set_selected(next((i for i, s in enumerate(XOVER_SLOPES)
                                 if s[0] == int(band.get('slope') or 24)), 1))
        g.add(slope)

        a = group('Alignment', 'Drivers are rarely the same distance away or '
                               'wired the same way round.')
        gain = Adw.SpinRow(title='Level (dB)',
                           adjustment=_adj(-24, 12, float(band.get('gain') or 0),
                                           0.5, 3), digits=1)
        delay = Adw.SpinRow(
            title='Delay (ms)', subtitle='1 ms ≈ 34 cm.',
            adjustment=_adj(0, paths.MAX_DELAY_S * 1000,
                            float(band.get('delay') or 0), 0.1, 1), digits=2)
        invert = Adw.SwitchRow(title='Invert polarity')
        invert.set_active(bool(band.get('invert')))
        mute = Adw.SwitchRow(title='Mute this band',
                             subtitle='Keeps the row, takes it out of the '
                                      'signal.')
        mute.set_active(bool(band.get('mute')))
        for r in (gain, delay, invert, mute):
            a.add(r)

        def save(_b):
            lov, hiv = round(lo.get_value()), round(hi.get_value())
            if lov and hiv and hiv <= lov:
                self.window.toast('“To” has to sit above “From”')
                return
            band.update({'name': name.get_text().strip() or 'Band',
                         'lo': float(lov), 'hi': float(hiv),
                         'slope': XOVER_SLOPES[slope.get_selected()][0],
                         'gain': round(gain.get_value(), 2),
                         'delay': round(delay.get_value(), 3),
                         'invert': invert.get_active(),
                         'mute': mute.get_active()})
            dlg.close()
            self._save_and_apply(strip)

        ok = Gtk.Button(label='Save', halign=Gtk.Align.END, margin_top=12,
                        css_classes=['suggested-action', 'pill'])
        ok.connect('clicked', save)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24,
                      margin_start=18, margin_end=18)
        box.append(g)
        box.append(a)
        box.append(ok)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(Adw.Clamp(maximum_size=520, child=box))
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        dlg.set_content(view)
        dlg.present()

    # -- chain rail --------------------------------------------------------
    def _rail(self, strip):
        rail = Adw.WrapBox(child_spacing=5, line_spacing=5,
                           css_classes=['path-rail'])
        for i, st in enumerate(strip.stages):
            chip_w = self._stage_chip(strip, st, i)
            if not i:
                rail.append(chip_w)
                continue
            # The arrow travels with the chip it points at.  As separate
            # children of the wrap box they wrap separately, which leaves a
            # line ending in a dangling "›" as soon as a chain is long enough
            # to need two rows.
            pair = Gtk.Box(spacing=5)
            pair.append(Gtk.Label(label='›', css_classes=['path-arrow'],
                                  valign=Gtk.Align.CENTER))
            pair.append(chip_w)
            rail.append(pair)
        # Anywhere on the rail that is not a chip means "put it at the end",
        # which is where a stage dragged in from another card usually goes.
        def over_rail(*_a):
            rail.add_css_class('drop-into')
            return True
        drop_target(rail, StageDrag,
                    lambda p, _x, _y: self._drop_stage(p, strip,
                                                       len(strip.stages)),
                    on_motion=over_rail,
                    on_leave=lambda: rail.remove_css_class('drop-into'))
        self._rails[strip.id] = rail
        if not strip.stages:
            rail.add_css_class('path-rail-empty')
            rail.append(Gtk.Label(
                label='No processing — audio passes straight through',
                css_classes=['dim-label', 'caption'], xalign=0,
                valign=Gtk.Align.CENTER, max_width_chars=42,
                ellipsize=Pango.EllipsizeMode.END))
        add = Gtk.MenuButton(icon_name='list-add-symbolic',
                             valign=Gtk.Align.CENTER,
                             tooltip_text='Add a stage',
                             css_classes=['path-add'])
        add.set_popover(self._stage_popover(strip))
        rail.append(add)
        return rail

    def _stage_chip(self, strip, st, index):
        kind = st.get('kind', '')
        off = bool(st.get('bypass'))
        btn = Gtk.Button(valign=Gtk.Align.CENTER,
                         css_classes=['path-stage', f'k-{kind}'])
        btn.set_tooltip_text(
            ('Bypassed — click to put it back in the signal' if off
             else 'Click to bypass this stage')
            + '\nDouble-click or right-click to edit it, drag to reorder')
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name(
            STAGE_ICON.get(kind, 'preferences-other-symbolic')))
        content.append(Gtk.Label(label=esc(st.get('name', '?')),
                                 ellipsize=Pango.EllipsizeMode.END,
                                 max_width_chars=18))
        btn.set_child(content)
        if off:
            btn.add_css_class('path-stage-off')

        # A single click switches the stage in or out; the work waits out the
        # double-click window so that opening the editor never cuts the audio
        # on the way in.  The chip itself flips immediately, so the delay is
        # only ever felt by the graph, not by the user.
        pending = {'id': 0}

        def paint(bypassed):
            if bypassed:
                btn.add_css_class('path-stage-off')
            else:
                btn.remove_css_class('path-stage-off')

        def fire():
            pending['id'] = 0
            self._toggle_bypass(strip, st)
            return False

        def clicked(_b):
            if st.get('id') in self._editing or pending['id']:
                return
            paint(not st.get('bypass'))
            pending['id'] = GLib.timeout_add(dbl_ms(), fire)
        btn.connect('clicked', clicked)

        def second(gesture, n_press, _x, _y):
            if n_press < 2:
                return
            # Claiming stops the button's own gesture, so the second release
            # never turns into another click to undo.
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            if pending['id']:
                GLib.source_remove(pending['id'])
                pending['id'] = 0
                paint(bool(st.get('bypass')))
            self._edit_stage(strip, st)
        dbl = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        dbl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        dbl.connect('pressed', second)
        btn.add_controller(dbl)

        menu = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        menu.connect('pressed', lambda _g, _n, x, y: context_menu(
            btn, self._stage_menu(strip, st), x, y))
        btn.add_controller(menu)

        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', lambda _c, kv, _kc, mods:
                     self._stage_key(btn, strip, st, kv, mods))
        btn.add_controller(keys)

        drag_source(btn, lambda: StageDrag(strip.id, st.get('id', '')),
                    on_begin=lambda: self._begin_drag('stage', strip),
                    on_end=self._end_drag)
        drop_target(btn, StageDrag,
                    lambda p, x, _y: self._drop_stage(
                        p, strip,
                        index + (0 if x < btn.get_width() / 2 else 1)),
                    on_motion=lambda p, x, _y: self._stage_hover(btn, p, st, x),
                    on_leave=lambda: clear_drop(btn))
        self._chips[st.get('id', '')] = btn
        return btn

    def _stage_hover(self, btn, payload, st, x):
        clear_drop(btn)
        if payload.stage_id == st.get('id'):
            return False
        btn.add_css_class('drop-before' if x < btn.get_width() / 2
                          else 'drop-after')
        return True

    def _stage_menu(self, strip, st):
        bypassed = bool(st.get('bypass'))
        return [
            ('document-edit-symbolic', 'Edit…',
             lambda: self._edit_stage(strip, st)),
            ('media-playback-start-symbolic' if bypassed
             else 'media-playback-pause-symbolic',
             'Put back in the signal' if bypassed else 'Bypass',
             lambda: self._toggle_bypass(strip, st)),
            None,
            ('go-previous-symbolic', 'Move earlier',
             lambda: self._move_stage(strip, st, -1)),
            ('go-next-symbolic', 'Move later',
             lambda: self._move_stage(strip, st, 1)),
            ('edit-copy-symbolic', 'Duplicate',
             lambda: self._duplicate_stage(strip, st)),
            None,
            ('user-trash-symbolic', 'Remove from the chain',
             lambda: self._remove_stage(strip, st), 'destructive-flat'),
        ]

    def _stage_key(self, btn, strip, st, keyval, mods):
        ctrl = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            self._remove_stage(strip, st)
            return True
        if ctrl and keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            self._move_stage(strip, st, -1)
            return True
        if ctrl and keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            self._move_stage(strip, st, 1)
            return True
        if keyval == Gdk.KEY_Menu:
            context_menu(btn, self._stage_menu(strip, st),
                         btn.get_width() / 2, btn.get_height())
            return True
        return False

    def _stage_popover(self, strip):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        for kind, label, sub in (
            ('eq', 'Equalizer', 'Bands you can move while it plays'),
            ('xover', 'Crossover', 'Split the audio by frequency and send '
                                   'each band its own way'),
            ('effect', 'Plugin', 'Any LADSPA or LV2 on this system'),
            ('convolver', 'Convolver', 'An impulse response file'),
        ):
            b = Gtk.Button(css_classes=['flat'])
            inner = Gtk.Box(spacing=10)
            inner.append(Gtk.Image.new_from_icon_name(STAGE_ICON[kind]))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            text.append(Gtk.Label(label=label, xalign=0))
            text.append(Gtk.Label(label=sub, xalign=0,
                                  css_classes=['dim-label', 'caption']))
            inner.append(text)
            b.set_child(inner)
            b.connect('clicked', lambda _b, k=kind: (pop.popdown(),
                                                     self._add_stage(strip, k)))
            box.append(b)
        pop.set_child(box)
        return pop

    # -- destinations ------------------------------------------------------
    def _sends_box(self, strip, mixes):
        chips = []
        for m in mixes:
            chips.append(chip(m.name, f'Send this source into “{m.name}”',
                              lambda b, s=strip, mid=m.id:
                              self._toggle_send(b, s, mid),
                              active=m.id in strip.sends, toggle=True))
        if not mixes:
            chips.append(chip('Add a mix', 'A source needs somewhere to go',
                              lambda *_: self._new_strip('mix'),
                              icon='list-add-symbolic'))
        return chip_row('Sends to', *chips)

    def _outputs_box(self, strip):
        chips = [chip(self._device_label(o), 'Remove this output',
                      lambda _b, s=strip, o=o: self._drop_output(s, o),
                      icon='window-close-symbolic', active=True)
                 for o in strip.outputs]
        if not strip.outputs:
            chips.append(Gtk.Label(label='Follows the default output',
                                   css_classes=['dim-label', 'caption'],
                                   valign=Gtk.Align.CENTER))
        chips.append(add_button('Add an output',
                                lambda *_: self._add_output(strip)))
        return chip_row('Out to', *chips)

    def _apps_box(self, strip):
        node = self._node_for(strip)
        here = [s for s in self._streams
                if node is not None and s.target_id == node.id]
        chips = []
        for s in here:
            c = chip(s.name,
                     f'{s.media or "playing"}\nClick to send it back to the '
                     'default output, or drag it onto another strip',
                     lambda _b, st=s: self._release_app(st),
                     icon='window-close-symbolic', active=True)
            drag_source(c, lambda st=s: StreamDrag(st.id, st.name),
                        on_begin=lambda: self._begin_drag('stream', strip),
                        on_end=self._end_drag)
            chips.append(c)
        if not here:
            chips.append(Gtk.Label(label='Nothing playing here yet',
                                   css_classes=['dim-label', 'caption'],
                                   valign=Gtk.Align.CENTER))
        chips.append(add_button('Send an app here',
                                lambda *_: self._send_app(strip)))
        return chip_row('Playing', *chips)

    def _volume_box(self, strip):
        node = self._node_for(strip)
        if node is None:
            return None
        box = Gtk.Box(spacing=8)
        mute = Gtk.ToggleButton(icon_name='audio-volume-muted-symbolic',
                                active=node.muted, valign=Gtk.Align.CENTER,
                                css_classes=['flat', 'circular'],
                                tooltip_text='Mute')
        mute.connect('toggled', lambda b, n=node: pw.set_mute(n.id,
                                                              b.get_active()))
        box.append(mute)
        ctl = make_volume(self.volume_style,
                          lambda v, n=node: pw.set_volume(n.id, v))
        ctl.set_value(node.volume if node.volume is not None else 1.0)
        if node.serial and node.serial > 0 and not levels.at_capacity():
            ctl.set_meter(node.serial)
        self._vols[strip.id] = ctl
        ctl.widget.set_hexpand(True)
        box.append(ctl.widget)
        return box

    # ------------------------------------------------------------ actions --
    def _save_and_apply(self, strip, message=None):
        self._cancel_apply(strip.id)

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            if message and ok:
                self.window.toast(message)
            elif not ok:
                self.window.toast(f'Failed: {err or "unknown error"}')
            self.refresh()
            self.refresh_soon()     # the graph is still coming back
        paths.save_meta(strip)
        async_call(lambda: paths.apply(strip, self._strips), done)

    def _cancel_apply(self, strip_id):
        tid = self._timers.pop(strip_id, None)
        if tid:
            GLib.source_remove(tid)

    def _apply_soon(self, strip, message=None, render=True):
        """Write the change now, rebuild the graph in a moment.

        Rearranging a chain rewrites the filter graph, which restarts the unit
        and stops audio for an instant.  Waiting out a fraction of a second
        after the last edit is what makes bypassing three stages in a row one
        interruption rather than three, and the page has already shown the
        result by then either way.
        """
        paths.save_meta(strip)
        if render:
            self._render()
        self._cancel_apply(strip.id)

        def fire():
            self._timers.pop(strip.id, None)
            self._save_and_apply(strip, message)
            return False
        self._timers[strip.id] = GLib.timeout_add(APPLY_DELAY_MS, fire)

    def _remember_focus(self, st):
        """Keep the focus on a stage across the rebuilds a move causes."""
        self._focus_stage = st.get('id', '')
        if self._focus_timer:
            GLib.source_remove(self._focus_timer)

        def forget():
            self._focus_timer = 0
            self._focus_stage = ''
            return False
        # Long enough to cover the render and the refresh that follows the
        # apply, short enough that an unrelated refresh later never steals the
        # focus back.
        self._focus_timer = GLib.timeout_add(3000, forget)

    # -- stages ------------------------------------------------------------
    def _toggle_bypass(self, strip, st):
        st['bypass'] = not st.get('bypass')
        name = st.get('name') or st.get('kind') or 'stage'
        self._apply_soon(
            strip, f'“{name}” bypassed' if st['bypass']
            else f'“{name}” is back in the signal', render=False)

    def _move_stage(self, strip, st, delta):
        stages = list(strip.stages)
        i = next((k for k, s in enumerate(stages)
                  if s.get('id') == st.get('id')), None)
        if i is None:
            return
        j = max(0, min(len(stages) - 1, i + delta))
        if i == j:
            return
        stages.insert(j, stages.pop(i))
        strip.stages = stages
        self._remember_focus(st)
        self._apply_soon(strip)

    def _remove_stage(self, strip, st):
        strip.stages = [s for s in strip.stages
                        if s.get('id') != st.get('id')]
        self._apply_soon(strip,
                         f'“{st.get("name") or "Stage"}” removed')

    def _duplicate_stage(self, strip, st):
        clone = paths.clone_stage(st)
        stages = list(strip.stages)
        i = next((k for k, s in enumerate(stages)
                  if s.get('id') == st.get('id')), len(stages) - 1)
        stages.insert(i + 1, clone)
        strip.stages = stages
        self._apply_soon(strip, f'“{clone["name"]}” added')

    def _drop_stage(self, payload, dst, index):
        src = next((s for s in self._strips if s.id == payload.strip_id), None)
        if src is None:
            return False
        stage = next((s for s in src.stages
                      if s.get('id') == payload.stage_id), None)
        if stage is None:
            return False
        if src.id == dst.id:
            stages = list(dst.stages)
            old = stages.index(stage)
            if index in (old, old + 1):
                return False            # dropped back where it came from
            stages.pop(old)
            if index > old:
                index -= 1
            stages.insert(index, stage)
            dst.stages = stages
            self._remember_focus(stage)
            self._apply_soon(dst)
            return True
        # Across cards the stage really moves: it leaves one chain and joins
        # another, so both are rewritten.
        src.stages = [s for s in src.stages if s is not stage]
        stages = list(dst.stages)
        stages.insert(max(0, min(len(stages), index)), stage)
        dst.stages = stages
        self._remember_focus(stage)
        self._apply_soon(src, render=False)
        self._apply_soon(dst, f'“{stage.get("name") or "Stage"}” moved to '
                              f'“{dst.name}”')
        return True

    # -- strips ------------------------------------------------------------
    def _renumber(self, role):
        """Write the current on-screen order back onto the strips."""
        for i, s in enumerate(x for x in self._strips if x.role == role):
            if s.order != i:
                s.order = i
                paths.save_meta(s)

    def _place_strip(self, moving, seq, index):
        seq = [s for s in seq if s.id != moving.id]
        seq.insert(max(0, min(len(seq), index)), moving)
        rest = [s for s in self._strips if s.role != moving.role]
        self._strips = seq + rest
        self._renumber(moving.role)
        # Stable, like the store's own sort: the column just renumbered comes
        # out in the new order and the other one is left exactly as it was.
        self._strips = sorted(self._strips, key=lambda s: s.order)
        self._render()
        return True

    def _drop_strip(self, payload, target, y):
        moving = next((s for s in self._strips
                       if s.id == payload.strip_id), None)
        if moving is None or moving.id == target.id:
            return False
        if moving.role != target.role:
            self._link_strips(moving, target)
            return True
        seq = [s for s in self._strips if s.role == target.role]
        card = self._cards.get(target.id)
        before = y < (card.get_height() / 2 if card is not None else 0)
        rest = [s for s in seq if s.id != moving.id]
        at = next((i for i, s in enumerate(rest) if s.id == target.id),
                  len(rest))
        return self._place_strip(moving, seq, at if before else at + 1)

    def _drop_strip_end(self, payload, role):
        moving = next((s for s in self._strips
                       if s.id == payload.strip_id), None)
        if moving is None or moving.role != role:
            return False
        seq = [s for s in self._strips if s.role == role]
        return self._place_strip(moving, seq, len(seq))

    def _link_strips(self, a, b):
        """A card dropped on the opposite column becomes a send."""
        src, mix = (a, b) if a.role == 'source' else (b, a)
        if mix.id in src.sends:
            self.window.toast(f'“{src.name}” already sends to “{mix.name}”')
            return
        src.sends = [*src.sends, mix.id]
        self._apply_soon(src, f'“{src.name}” now sends to “{mix.name}”')

    def _drop_stream(self, payload, strip):
        node = self._node_for(strip)
        if node is None:
            self.window.toast(f'Turn “{strip.name}” on first')
            return False
        stream = next((s for s in self._streams
                       if s.id == payload.stream_id), None)
        if stream is None or stream.target_id == node.id:
            return False

        def done(ok, e):
            self.window.toast(f'“{stream.name}” → “{strip.name}”'
                              if ok and not e else 'Could not move it')
            self.refresh()
        async_call(lambda: pw.move_stream(stream.id, node.serial), done)
        return True

    def _duplicate(self, strip):
        from dataclasses import asdict
        d = asdict(strip)
        for key in ('id', 'name', 'enabled', 'order'):
            d.pop(key, None)
        role = d.pop('role')
        # Fresh stage ids, or the copy and the original would be the same
        # chain as far as every click on the board is concerned.
        d['stages'] = [paths.clone_stage(s, rename=False)
                       for s in d.get('stages') or []]
        clone = paths.new_strip(f'{strip.name} copy', role, **d)
        clone.enabled = False
        paths.save_meta(clone)
        self.window.toast(f'“{clone.name}” added — switched off')
        self.refresh()

    def _toggle(self, sw, state, strip):
        if state == strip.enabled:
            return False
        strip.enabled = state
        self._save_and_apply(strip)
        return False

    def _toggle_send(self, btn, strip, mix_id):
        want = btn.get_active()
        if want == (mix_id in strip.sends):
            return
        strip.sends = ([*strip.sends, mix_id] if want
                       else [m for m in strip.sends if m != mix_id])
        self._save_and_apply(strip)

    def _drop_output(self, strip, node_name):
        strip.outputs = [o for o in strip.outputs if o != node_name]
        self._save_and_apply(strip)

    def _add_output(self, strip):
        edges = paths.target_edges(self._strips)
        allowed = paths.output_targets(
            strip, [n for n in self._nodes if n.is_sink], edges)
        items = [(n.name, n.description, n.name) for n in allowed
                 if n.name not in strip.outputs]
        items.append(('__virtual__', 'Create a virtual output…',
                      'A capture sink other apps (OBS, Discord) can record'))

        def picked(key):
            if key == '__virtual__':
                self._new_capture_sink(strip)
                return
            strip.outputs = [*strip.outputs, key]
            self._save_and_apply(strip)
        search_picker(self.window, 'Add an output',
                      'Where this mix sends its audio. Pick several and they '
                      'are combined into one output automatically.',
                      items, picked, empty='No output devices found')

    def _new_capture_sink(self, strip):
        def make(name):
            if not name:
                return
            dev = virtual.new_device(name, 'null-sink',
                                     positions=list(strip.positions))
            dev.enabled = True

            def done(result, e):
                ok, err = result if result else (False, str(e or ''))
                if not ok:
                    self.window.toast(f'Failed: {err}')
                    return
                strip.outputs = [*strip.outputs, dev.node_name]
                self._save_and_apply(
                    strip, f'“{name}” created — pick it as the source in '
                    'your recording app')
            async_call(lambda: virtual.apply(dev), done)
        prompt_text(self.window, 'Create a virtual output',
                    'It appears as a recording source in other apps, so a '
                    'stream mix can be captured without going through your '
                    'speakers.', 'Stream Mix', make, action='Create')

    def _send_app(self, strip):
        node = self._node_for(strip)
        if node is None:
            self.window.toast('Turn this source on first')
            return
        items = [(s.id, s.name, s.media or 'playing')
                 for s in self._streams if s.target_id != node.id]
        if not items:
            self.window.toast('Nothing else is playing right now')
            return

        def picked(stream_id):
            def done(ok, e):
                self.window.toast('Moved' if ok and not e
                                  else 'Could not move it')
                self.refresh()
            async_call(lambda: pw.move_stream(stream_id, node.serial), done)
        search_picker(self.window, 'Send an app here',
                      'The app keeps playing; it is just relinked.',
                      items, picked)

    def _release_app(self, stream):
        """Put an app back on the default output."""
        dest = next((n for n in self._nodes if n.is_sink and n.is_default),
                    None)
        if dest is None:
            self.window.toast('No default output to send it back to')
            return

        def done(ok, e):
            self.window.toast('Back on the default output' if ok and not e
                              else 'Could not move it')
            self.refresh()
        async_call(lambda: pw.move_stream(stream.id, dest.serial), done)

    def _add_stage(self, strip, kind):
        stage = paths.new_stage(kind, {'eq': 'Equalizer', 'effect': 'Plugin',
                                       'convolver': 'Convolver',
                                       'xover': 'Crossover'}[kind])
        if kind == 'eq':
            stage['params'] = {'preamp': 0.0, 'bands': [
                {'on': True, 'type': 'PK', 'freq': f, 'gain': 0.0, 'q': 1.0}
                for f in (60, 250, 1000, 4000, 12000)]}
        strip.stages = [*strip.stages, stage]
        self._edit_stage(strip, stage, is_new=True)

    def _edit_stage(self, strip, stage, is_new=False):
        sid = stage.get('id')
        if sid in self._editing:
            return
        self._editing.add(sid)

        def done(saved):
            self._editing.discard(sid)
            if not saved and is_new:
                # Cancelling out of a stage that was created for this dialog
                # leaves nothing behind, so a half-configured plugin never
                # ends up in the chain.
                strip.stages = [s for s in strip.stages
                                if s.get('id') != stage.get('id')]
                if not strip.enabled:
                    paths.save_meta(strip)
                    self.refresh()
                    return
            self._save_and_apply(strip)
        StageDialog(self.window, self, strip, stage, done).present()

    def _new_strip(self, role):
        def make(name):
            if not name:
                return
            kw = {}
            if role == 'source':
                kw['kind'] = 'app'
            strip = paths.new_strip(name, role, **kw)
            strip.enabled = False
            paths.save_meta(strip)
            self.refresh()
            self.window.toast(f'“{name}” added — give it stages, then turn '
                              'it on')
        prompt_text(self.window,
                    'New source' if role == 'source' else 'New mix',
                    'Sources are where audio comes in; mixes feed your '
                    'devices.',
                    'Music' if role == 'source' else 'Speakers',
                    make, action='Add')

    def _rename(self, strip):
        def go(name):
            if name:
                strip.name = name
                self._save_and_apply(strip)
        prompt_text(self.window, 'Rename', 'What this strip is called.',
                    strip.name, go)

    def _pick_layout(self, strip):
        from ..backend import surround
        # Same list Virtual Devices offers, plus mono, so a strip in front of
        # a microphone doesn't have to pretend to be stereo.
        layouts = [('mono', 'Mono 1.0', ['FL'])] + list(surround.LAYOUTS)
        items = [(pos, label, ' '.join(pos)) for _k, label, pos in layouts]

        def picked(positions):
            positions = list(positions)
            if positions == list(strip.positions):
                return
            strip.positions = positions
            self._save_and_apply(
                strip, f'“{strip.name}” is now {_layout_name(len(positions))}')
        search_picker(self.window, 'Channel layout',
                      'How many channels this strip carries. Every stage is '
                      'built across all of them.', items, picked)

    def _pick_out_layout(self, strip):
        """What comes out, when that is not what went in.

        Only a crossover has a reason to change this: routing the low band to
        lanes of its own needs lanes of its own to exist.  Everything else is
        happier with the two layouts kept the same, which is why "same as the
        input" sits at the top and is what every strip starts as.
        """
        from ..backend import surround
        layouts = [('mono', 'Mono 1.0', ['FL'])] + list(surround.LAYOUTS)
        items = [([], 'Same as the input', ' '.join(strip.positions))]
        items += [(pos, label, ' '.join(pos)) for _k, label, pos in layouts]

        def picked(positions):
            positions = list(positions)
            if positions == list(strip.positions):
                positions = []          # not a split, just the same thing
            if positions == list(strip.out_positions):
                return
            strip.out_positions = positions
            self._save_and_apply(
                strip, f'“{strip.name}” now comes out as '
                f'{_layout_name(strip.out_channels)}')
        search_picker(self.window, 'Output layout',
                      'The channels leaving this strip. Widen it to give a '
                      'crossover band somewhere of its own to go — a stereo '
                      'strip coming out as 2.1 can put its low band on the '
                      'subwoofer lane.', items, picked)

    def _pick_mode(self, strip):
        """Insert into a device, or publish an output of its own.

        Inserting is the quiet option and the default: the device stays the
        thing people select, and nothing new turns up in anyone's list.  A
        strip only needs its own output when something has to *choose* it —
        the capture sink OBS records from, or a mix fed by a source strip.
        """
        items = []
        if paths.insertable(strip, self._strips) or strip.mode == 'insert':
            dev = strip.insert_device()
            items.append(('insert', 'Inside the output',
                          'Nothing new appears — it corrects '
                          + (self._device_label(dev) if dev
                             else 'whatever the default output is')))
        items.append(('sink', 'Its own output',
                      'Publishes a device others can select and play into'))
        if strip.mode == 'tap':
            items.append(('tap', 'A copy of another strip',
                          'Reads what another strip is fed, plays it '
                          'elsewhere. Set up by the crossover recipe.'))

        def picked(mode):
            if mode == strip.mode:
                return
            strip.mode = mode
            self._save_and_apply(strip)
        search_picker(self.window, 'How it connects', 'Whether this strip '
                      'becomes part of a device’s path or an output of its '
                      'own.', items, picked)

    def _delete(self, strip):
        def go():
            def done(_r, _e):
                self.window.toast(f'“{strip.name}” deleted')
                self.refresh()
            async_call(lambda: paths.delete(strip), done)
        confirm(self.window, f'Delete “{strip.name}”?',
                'The chain and anything it created are removed. Apps playing '
                'through it go back to the default output.',
                'Delete', go)

    def _manage_strips(self):
        """Tick several strips off and delete them, or clear the board.

        Deleting from the card menu is one confirmation each, which is fine
        for one and tiresome for the eight a scrapped experiment leaves
        behind.  Clearing everything lives in the same dialog because it is
        the same intention taken to its end, and putting it anywhere else
        would make it a surprise.
        """
        if not self._strips:
            self.window.toast('There is nothing to delete')
            return
        dlg = Adw.Dialog(title='Delete strips', content_width=520,
                         content_height=620)
        picks: dict = {}

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=18,
                      margin_start=16, margin_end=16)
        box.append(Gtk.Label(
            label='Whatever you remove takes its chain and any device it '
                  'built with it. Apps playing through a deleted strip go '
                  'back to the default output.',
            xalign=0, wrap=True, css_classes=['dim-label']))

        for role, title in (('source', 'Sources'), ('mix', 'Mixes')):
            rows = [s for s in self._strips if s.role == role]
            if not rows:
                continue
            g = group(title)
            for s in rows:
                state = self._states.get(s.id, 'inactive')
                running = s.enabled and state == 'active'
                bits = [f'{len(s.stages)} stage'
                        + ('s' if len(s.stages) != 1 else '')]
                if s.role == 'source' and s.sends:
                    bits.append(f'{len(s.sends)} send'
                                + ('s' if len(s.sends) != 1 else ''))
                if s.role == 'mix' and s.outputs:
                    bits.append(f'{len(s.outputs)} output'
                                + ('s' if len(s.outputs) != 1 else ''))
                bits.append('running' if running
                            else ('off' if not s.enabled else state))
                row = Adw.ActionRow(title=esc(s.name),
                                    subtitle=esc(' · '.join(bits)),
                                    activatable=True, title_lines=1,
                                    subtitle_lines=1)
                check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
                row.add_prefix(check)
                row.set_activatable_widget(check)
                picks[s.id] = (check, s)
                g.add(row)
            box.append(g)

        count = Gtk.Label(xalign=0, hexpand=True, css_classes=['dim-label'])
        delete = Gtk.Button(label='Delete selected', sensitive=False,
                            css_classes=['destructive-action', 'pill'])
        actions = Gtk.Box(spacing=10, margin_top=4)
        actions.append(count)
        actions.append(delete)

        def selected():
            return [s for check, s in picks.values() if check.get_active()]

        def retally(*_a):
            n = len(selected())
            delete.set_sensitive(bool(n))
            count.set_label(f'{n} selected' if n else 'Nothing selected')
        for check, _s in picks.values():
            check.connect('toggled', retally)
        retally()

        delete.connect('clicked', lambda *_: self._confirm_delete(
            dlg, selected(), 'Delete the selected strips?'))
        box.append(actions)

        # The last thing on the page, spelt out and boxed off, because it is
        # the one action here that does not need a single tick to be armed.
        reset = group('Start over',
                      'Removes every source and mix on the board, along with '
                      'the units and combine devices they created. Your '
                      'devices, equalizers and filter chains are untouched.')
        row = Adw.ActionRow(title='Delete everything',
                            subtitle=f'{len(self._strips)} strips')
        wipe = Gtk.Button(label='Reset the board', valign=Gtk.Align.CENTER,
                          css_classes=['destructive-action'])
        wipe.connect('clicked', lambda *_: self._confirm_delete(
            dlg, list(self._strips), 'Delete every strip on the board?'))
        row.add_suffix(wipe)
        reset.add(row)
        box.append(reset)

        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(box)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        dlg.set_child(view)
        dlg.present(self.window)

    def _confirm_delete(self, dlg, strips, heading):
        if not strips:
            return
        names = ', '.join(s.name for s in strips[:4])
        if len(strips) > 4:
            names += f' and {len(strips) - 4} more'

        def go():
            dlg.close()
            self._delete_many(strips)
        confirm(self.window, heading, f'{names}. This cannot be undone.',
                f'Delete {len(strips)}', go)

    def _delete_many(self, strips):
        # Sources first: deleting a mix walks the sources that fed it and
        # regenerates each one to drop the dangling send, and there is no
        # point rebuilding a strip that is about to be deleted anyway.
        order = sorted(strips, key=lambda s: s.role != 'source')

        def run():
            for s in order:
                paths.delete(s)
            return True

        def done(_r, e):
            self.window.toast(f'Could not finish: {e}' if e
                              else f'Deleted {len(order)} strip'
                                   + ('s' if len(order) != 1 else ''))
            self.refresh()
        async_call(run, done)

    # ------------------------------------------------------------ sharing --
    def _export(self, strip):
        data = {'format': 'pwctl-signal-path-1',
                'strips': [self._strip_dict(strip)]}
        if strip.role == 'source':
            by_id = {m.id: m for m in paths.mixes(self._strips)}
            data['strips'] += [self._strip_dict(by_id[m])
                               for m in strip.sends if m in by_id]

        def got(folder):
            if not folder:
                return
            p = GLib.build_filenamev([folder, f'{strip.id}.json'])
            try:
                with open(p, 'w') as fh:
                    json.dump(data, fh, indent=2)
                self.window.toast(f'Exported to {p}')
            except OSError as e:
                self.window.toast(f'Could not write it: {e}')
        pick_folder(self.window, 'Choose where to save it', got)

    def _strip_dict(self, strip):
        from dataclasses import asdict
        d = asdict(strip)
        d.pop('enabled', None)
        return d

    def _import(self):
        def got(path):
            if not path:
                return
            try:
                data = json.loads(open(path).read())
            except (OSError, ValueError) as e:
                self.window.toast(f'Could not read it: {e}')
                return
            if data.get('format') != 'pwctl-signal-path-1':
                self.window.toast('That is not an exported signal path')
                return
            remap: dict = {}
            made = []
            for raw in data.get('strips', []):
                known = set(paths.Strip.__dataclass_fields__)
                fields = {k: v for k, v in raw.items() if k in known}
                old = fields.pop('id', '')
                name = fields.pop('name', 'Imported')
                role = fields.pop('role', 'source')
                fields.pop('enabled', None)
                strip = paths.new_strip(name, role, **fields)
                strip.enabled = False
                remap[old] = strip.id
                made.append(strip)
            for strip in made:              # sends refer to the new ids
                strip.sends = [remap.get(s, s) for s in strip.sends]
                paths.save_meta(strip)
            self.window.toast(f'Imported {len(made)} — switched off, so look '
                              'before turning them on')
            self.refresh()
        pick_file(self.window, 'Choose an exported path', got)

    # -------------------------------------------------------- quick setup --
    def _default_sink_name(self):
        n = next((x for x in self._nodes if x.is_sink and x.is_default), None)
        return n.name if n else ''

    def _quick_pair(self, src_name, mix_name, stages, kind='everything'):
        dev = self._default_sink_name()
        mix = paths.new_strip(mix_name, 'mix',
                              outputs=[dev] if dev else [])
        mix.enabled = True
        src = paths.new_strip(src_name, 'source', kind=kind, sends=[mix.id],
                              stages=stages)
        src.enabled = True

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(f'“{src_name}” ready' if ok
                              else f'Failed: {err or "unknown error"}')
            self.refresh()

        def build():
            ok, err = paths.apply(mix, [mix, src])
            if not ok:
                return ok, err
            return paths.apply(src, [mix, src])
        async_call(build, done)

    def _quick_eq_all(self):
        """One equalizer, inside the output that is already selected.

        This used to be a source and a mix, which meant two new sinks and a
        trip to the sound settings to pick one of them.  Inserted, it is the
        same equalizer with nothing to choose: the device stays the output
        and every app keeps playing to it.
        """
        stage = paths.new_stage('eq', 'Equalizer')
        stage['params'] = {'preamp': 0.0, 'bands': [
            {'on': True, 'type': 'PK', 'freq': f, 'gain': 0.0, 'q': 1.0}
            for f in (60, 250, 1000, 4000, 12000)]}
        dev = self._default_sink_name()
        mix = paths.new_strip('Equalizer', 'mix', mode='insert',
                              outputs=[dev] if dev else [], stages=[stage])
        mix.enabled = True

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast('Equalizer added to your output — nothing to '
                              'select' if ok
                              else f'Failed: {err or "unknown error"}')
            self.refresh()
        async_call(lambda: paths.apply(mix, [mix]), done)

    def _quick_app_fx(self):
        self._quick_pair('App', 'Speakers', [], kind='app')

    def _quick_crossover(self):
        """Two mixes crossing at 80 Hz, one per output device.

        The two bands are built as a matched pair — same frequency, same
        slope, complementary filters — because that is the part that is easy
        to get wrong by hand and the part that has to stay true afterwards:
        move one and the crossover region either sags or doubles up.  Which
        device carries the low band is the only thing that cannot be guessed,
        so it is the only thing asked.
        """
        dev = self._default_sink_name()
        items = [(n.name, n.description, n.name) for n in self._nodes
                 if n.is_sink and not n.name.startswith('pwctl.')]

        def picked(sub_dev):
            # One crossover, two bands, one destination each.  It stands in
            # front of the output that is already selected, so no app moves
            # and nothing new turns up to be chosen.
            x = paths.new_strip('Crossover', 'xover', insert_into=dev,
                                bands=[paths.new_band('Low', hi=80.0,
                                                      outputs=[sub_dev]),
                                       paths.new_band('High', lo=80.0,
                                                      outputs=[dev] if dev
                                                      else [])])
            x.enabled = True
            made = [x]

            def build():
                for s in made:
                    ok, err = paths.apply(s, made)
                    if not ok:
                        return ok, err
                return True, ''

            def done(result, e):
                ok, err = result if result else (False, str(e or ''))
                self.window.toast(
                    'Crossing at 80 Hz — your output is unchanged, open '
                    'either band to move it'
                    if ok else f'Failed: {err or "unknown error"}')
                self.refresh()
            async_call(build, done)

        search_picker(self.window, 'Which output carries the low band?',
                      'Everything below 80 Hz goes there; everything above it '
                      'goes to your current output. Both bands are ordinary '
                      'stages afterwards — change the frequency, the slope or '
                      'the alignment on either one.',
                      items, picked, empty='No output devices found')

    def _quick_two_outputs(self):
        """Speakers now, headphones ready beside them.

        Both mixes are built and both are running; only the speaker one is
        fed.  Sending to both at once would mean the same audio arriving at
        the same device twice, so the second mix is left waiting for its send
        — which is one click on the source's chip, and is the gesture worth
        learning from this arrangement.
        """
        dev = self._default_sink_name()
        spk = paths.new_strip('Speakers', 'mix', outputs=[dev] if dev else [])
        spk.enabled = True
        tilt = paths.new_stage('eq', 'Headphone tilt')
        tilt['params'] = {'preamp': -1.0, 'bands': [
            {'on': True, 'type': 'LSC', 'freq': 105.0, 'gain': 1.5, 'q': 0.7},
            {'on': True, 'type': 'PK', 'freq': 3200.0, 'gain': -1.5, 'q': 1.1},
            {'on': True, 'type': 'HSC', 'freq': 10000.0, 'gain': 1.0,
             'q': 0.7}]}
        head = paths.new_strip('Headphones', 'mix', stages=[tilt])
        head.enabled = True
        src = paths.new_strip('Everything', 'source', kind='everything',
                              sends=[spk.id])
        src.enabled = True
        made = [spk, head, src]

        def build():
            for s in made:
                ok, err = paths.apply(s, made)
                if not ok:
                    return ok, err
            return True, ''

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(
                'Ready — click the “Headphones” chip on the source to send '
                'there instead' if ok else f'Failed: {err or "unknown error"}')
            self.refresh()
        async_call(build, done)

    # ---------------------------------------------------------- templates --
    def _open_templates(self):
        """The catalog as a dialog, for a board that already has things on it.

        Same cards as the empty board shows, in one uninterrupted order:
        simple at the top, a full broadcast chain at the bottom, and no
        headings in between.  Where something belongs on that scale is the
        only classification the list makes, and a search entry is what makes
        the length of it harmless.
        """
        dlg = Adw.Dialog(title='Templates', content_width=980,
                         content_height=720)
        registry: dict = {}
        search = Gtk.SearchEntry(placeholder_text='Search templates…')
        cards = [self._template_card(t, registry,
                                     on_pick=lambda t2: (dlg.close(),
                                                         self._load_template(t2)))
                 for t in path_templates.CATALOG]
        grid = self._card_grid(cards)
        nothing = Adw.StatusPage(title='Nothing matches',
                                 icon_name='edit-find-symbolic', vexpand=True,
                                 visible=False)

        def filter_cards(*_a):
            needle = search.get_text().strip().lower()
            hits = 0
            for card in cards:
                t = card._tpl
                hay = f'{t.title} {t.blurb} {t.detail} {" ".join(t.chain)}'
                ok = needle in hay.lower()
                card.set_visible(ok)
                hits += ok
            grid.set_visible(bool(hits))
            nothing.set_visible(not hits)
        search.connect('search-changed', filter_cards)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=18,
                      margin_start=16, margin_end=16)
        box.append(Gtk.Label(
            label='Each one builds an ordinary strip with its chain already '
                  'filled in — rename it, reorder it or pull it apart '
                  'afterwards. They run from the simplest at the top to the '
                  'ones people run for broadcast at the bottom.',
            xalign=0, wrap=True, css_classes=['dim-label']))
        box.append(search)
        box.append(grid)
        box.append(nothing)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(box)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        dlg.set_child(view)
        dlg.present(self.window)
        self._scan_plugins(registry)

    def _load_template(self, tpl):
        """Build the template's strip, and whatever it needs to be audible.

        A source stands on its own — with no send it simply plays into the
        default output — but a mix with nothing feeding it is a sink no audio
        ever reaches, so an empty board gets a plain source to go with it.
        Everything is created switched on, like the quick-setup recipes: a
        template that leaves you one more click away from hearing it is not
        much of a head start.
        """
        def ready(installed, error):
            installed = installed or []
            stages, missing = path_templates.build_stages(tpl, installed)
            strip = paths.new_strip(tpl.title, tpl.role, kind=tpl.kind,
                                    positions=list(tpl.positions),
                                    out_positions=list(tpl.out_positions),
                                    stages=stages)
            strip.enabled = True
            made = [strip]
            if tpl.role == 'source':
                mixes = paths.mixes(self._strips)
                # One mix is unambiguous, so wire it up; with several, guessing
                # would be worse than the chip the user can click.
                if len(mixes) == 1:
                    strip.sends = [mixes[0].id]
            elif not paths.sources(self._strips):
                paths.save_meta(strip)     # so the source gets its own order
                src = paths.new_strip('Everything', 'source',
                                      kind='everything', sends=[strip.id],
                                      positions=list(tpl.positions))
                src.enabled = True
                made = [strip, src]        # the mix has to exist first

            def build():
                for s in made:
                    ok, err = paths.apply(s, self._strips + made)
                    if not ok:
                        return ok, err
                return True, ''

            def done(result, e):
                ok, err = result if result else (False, str(e or ''))
                if not ok:
                    self.window.toast(f'Failed: {err or "unknown error"}')
                elif missing:
                    self.window.toast(
                        f'“{tpl.title}” ready, without ' + ', '.join(missing)
                        + ' — no plugin for that on this machine')
                else:
                    self.window.toast(f'“{tpl.title}” ready')
                self.refresh()
            async_call(build, done)
        async_call(_all_plugins, ready)

    def _quick_stream(self):
        dev = self._default_sink_name()
        speakers = paths.new_strip('Speakers', 'mix',
                                   outputs=[dev] if dev else [])
        speakers.enabled = True
        cap = virtual.new_device('Stream Mix', 'null-sink')
        cap.enabled = True
        stream = paths.new_strip('Stream', 'mix', outputs=[cap.node_name])
        stream.enabled = True
        src = paths.new_strip('Everything', 'source', kind='everything',
                              sends=[speakers.id, stream.id])
        src.enabled = True
        allst = [speakers, stream, src]

        def build():
            ok, err = virtual.apply(cap)
            if not ok:
                return ok, err
            for st in allst:
                ok, err = paths.apply(st, allst)
                if not ok:
                    return ok, err
            return True, ''

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(
                'Ready — pick “Stream Mix” as the source in OBS or Discord'
                if ok else f'Failed: {err or "unknown error"}')
            self.refresh()
        async_call(build, done)
