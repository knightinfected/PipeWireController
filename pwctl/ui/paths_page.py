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

from ..backend import levels, paths, plugins, prefs, pw, virtual
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
}
BAND_TYPES = [('PK', 'Peak'), ('LSC', 'Low shelf'), ('HSC', 'High shelf')]

# Rewriting the graph restarts the unit, so audio stops for an instant.  A
# short delay after the last edit turns "bypass three stages" into one
# interruption instead of three.
APPLY_DELAY_MS = 350

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

def micro(text: str) -> Gtk.Label:
    """A section label: small, heavy, quiet.  Uppercased here rather than in
    CSS so it does not depend on GTK's text-transform support."""
    lbl = Gtk.Label(label=text.upper(), xalign=0, valign=Gtk.Align.CENTER)
    lbl.add_css_class('path-label')
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
    lbl = Gtk.Label(label=esc(label), ellipsize=Pango.EllipsizeMode.END,
                    max_width_chars=18)
    box.append(lbl)
    if icon:
        box.append(Gtk.Image.new_from_icon_name(icon))
    btn.set_child(box)
    if toggle:
        btn.set_active(active)
    elif active:
        btn.add_css_class('on')
    if on_click:
        btn.connect('toggled' if toggle else 'clicked', on_click)
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
                cr.set_line_width(2.0)
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

        self.widget = self._build()
        self.widget.connect('map', lambda *_: self.refresh())

    # ------------------------------------------------------------- shell --
    def _build(self):
        # toolbar: what exists, and how to add to it
        self.summary = Gtk.Label(xalign=0, hexpand=True,
                                 ellipsize=Pango.EllipsizeMode.END)
        self.summary.add_css_class('dim-label')
        add_src = Gtk.Button(css_classes=['suggested-action'],
                             tooltip_text='A source is where audio enters')
        add_src.set_child(Adw.ButtonContent(icon_name='list-add-symbolic',
                                            label='Source'))
        add_src.connect('clicked', lambda *_: self._new_strip('source'))
        add_mix = Gtk.Button(tooltip_text='A mix feeds your output devices')
        add_mix.set_child(Adw.ButtonContent(icon_name='list-add-symbolic',
                                            label='Mix'))
        add_mix.connect('clicked', lambda *_: self._new_strip('mix'))
        more = Gtk.MenuButton(icon_name='view-more-symbolic',
                              css_classes=['flat'], tooltip_text='More')
        more.set_popover(menu_popover([
            ('document-open-symbolic', 'Import a path…', self._import),
            ('view-refresh-symbolic', 'Refresh', self.refresh),
        ]))
        toolbar = Gtk.Box(spacing=8, css_classes=['path-toolbar'])
        toolbar.append(self.summary)
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

        # Below ~720px of content the two columns stop being readable, so
        # they stack and the gutter goes away — same information, one column.
        bin_ = Adw.BreakpointBin(width_request=320, height_request=200)
        bin_.set_child(self.board)
        bp = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse('max-width: 720px'))
        bp.add_setter(self.board, 'orientation', Gtk.Orientation.VERTICAL)
        bp.add_setter(self.board, 'spacing', 24)
        bp.add_setter(self.wires, 'visible', False)
        bin_.add_breakpoint(bp)
        self.board_bin = bin_

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=18, margin_bottom=36,
                      margin_start=16, margin_end=16)
        box.append(toolbar)
        box.append(self.quick)
        box.append(bin_)
        clamp = Adw.Clamp(maximum_size=1600, tightening_threshold=1100)
        clamp.set_child(box)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vexpand=True)
        sw.set_child(clamp)
        return sw

    def _column(self, title, tooltip, on_add, role):
        head = Gtk.Box(spacing=8)
        head.append(micro(title))
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

    def _quick_setup(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        title = Gtk.Label(label='Start with a ready-made path', xalign=0,
                          css_classes=['title-4'])
        sub = Gtk.Label(
            label='Each of these builds a complete path in one step. You can '
                  'take it apart afterwards — they are ordinary sources and '
                  'mixes.',
            xalign=0, wrap=True, css_classes=['dim-label'])
        box.append(title)
        box.append(sub)
        wrap = Adw.WrapBox(child_spacing=12, line_spacing=12,
                           natural_line_length=1200,
                           justify=Adw.JustifyMode.FILL)
        for icon, title_t, sub_t, fn in (
            ('audio-x-generic-symbolic', 'Equalize everything',
             'One equalizer between every app and your current output.',
             self._quick_eq_all),
            ('applications-multimedia-symbolic', 'Put effects on one app',
             'Send a single app through a plugin chain, leaving the rest of '
             'your audio alone.', self._quick_app_fx),
            ('camera-video-symbolic', 'Speakers and a stream mix',
             'One chain into your speakers, a second into a virtual output '
             'that OBS or Discord can capture.', self._quick_stream),
        ):
            btn = Gtk.Button(css_classes=['path-recipe'], hexpand=True)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            img = Gtk.Image.new_from_icon_name(icon)
            img.set_pixel_size(18)
            img.add_css_class('path-recipe-icon')
            img.set_halign(Gtk.Align.START)
            inner.append(img)
            t = Gtk.Label(label=title_t, xalign=0, css_classes=['heading'],
                          wrap=True)
            inner.append(t)
            s = Gtk.Label(label=sub_t, xalign=0, wrap=True,
                          max_width_chars=30,
                          css_classes=['dim-label', 'caption'])
            inner.append(s)
            btn.set_child(inner)
            btn.connect('clicked', lambda _b, f=fn: f())
            wrap.append(btn)
        box.append(wrap)
        return box

    # ------------------------------------------------------------ refresh --
    def refresh(self):
        def collect():
            dump = pw.pw_dump()
            nodes = pw.list_audio_nodes(dump)
            streams = [s for s in pw.list_streams(dump) if s.is_playback
                       and not s.props.get('node.name', '').startswith('pwctl.')]
            strips = paths.list_strips()
            states = {s.id: paths.status(s) for s in strips}
            return strips, states, nodes, streams
        async_call(collect, self._apply)

    def _apply(self, payload, error):
        if error or payload is None:
            return
        self._strips, self._states, self._nodes, self._streams = payload
        self._render()

    def _render(self):
        """Rebuild the board from what is already in memory.

        Split out of the refresh so a rearrangement shows up the instant it
        happens: dragging a stage changes a list and redraws, and only the
        rebuild of the actual audio graph waits behind `pw-dump`.
        """
        self._vols = {}
        self._cards = {}
        self._chips = {}
        self._rails = {}
        for body in (self.src_body, self.mix_body):
            child = body.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                body.remove(child)
                child = nxt

        srcs = paths.sources(self._strips)
        mixes = paths.mixes(self._strips)
        empty = not self._strips
        self.quick.set_visible(empty)
        self.board_bin.set_visible(not empty)
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
        head = Gtk.Box(spacing=10)
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
        tag = Gtk.Label(label=_layout_name(strip.channels),
                        css_classes=['path-tag'], valign=Gtk.Align.CENTER)
        tag.set_tooltip_text(f'{strip.channels} channels: '
                             + ' '.join(strip.positions))
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
        feeders = [s.name for s in paths.sources(self._strips)
                   if strip.id in s.sends]
        return ('Fed by ' + ', '.join(feeders)) if feeders \
            else 'Nothing feeds this yet'

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
                                       'convolver': 'Convolver'}[kind])
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
        stage = paths.new_stage('eq', 'Equalizer')
        stage['params'] = {'preamp': 0.0, 'bands': [
            {'on': True, 'type': 'PK', 'freq': f, 'gain': 0.0, 'q': 1.0}
            for f in (60, 250, 1000, 4000, 12000)]}
        self._quick_pair('Everything', 'Speakers', [stage],
                         kind='everything')

    def _quick_app_fx(self):
        self._quick_pair('App', 'Speakers', [], kind='app')

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
