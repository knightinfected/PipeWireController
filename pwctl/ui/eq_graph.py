"""The equalizer's frequency response, drawn and handled (Cairo).

`EqGraph` draws a curve from `eqcurve` and turns pointer, touch and key
input into edits, which it hands to callbacks; it never owns the curve.
`CurveThumb` is the same response at thumbnail size, for list rows.

Colours come from the stylesheet through invisible pen labels
(`eq-pen-*`), the bridge Signal Paths' wires use: Cairo cannot read CSS, and
`@accent_color` is not allowed to carry a meaning here (UI.md §2).

Input, all optional for the caller:
  drag a point            frequency (x) and gain (y); Shift is fine
  scroll on a point       Q (width)
  double-click empty      add a band there
  right-click / hold      the band's menu
  keys (graph focused)    ←/→ frequency, ↑/↓ gain, Page Up/Down Q,
                          [ ] previous/next band, Space on/off, Delete
"""

from __future__ import annotations

import math

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Gdk, Gtk, Pango, PangoCairo  # noqa: E402

from ..backend import eqcurve as E

PENS = ('eq-pen-lo', 'eq-pen-hi', 'eq-pen-node', 'eq-pen-hot',
        'eq-pen-bypass', 'eq-pen-bg')
HIT = 16                         # pointer radius around a point, px
GRID_HZ = (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000)
LABEL_HZ = (100, 1000, 10000)
LABEL_HZ_WIDE = (50, 100, 200, 500, 1000, 2000, 5000, 10000)


def round_freq(f: float) -> float:
    """Three significant figures (63.2, 1250, 12300): what a person types."""
    if f <= 0:
        return f
    digits = 3 - int(math.floor(math.log10(f))) - 1
    return round(f, digits) if digits > 0 else float(round(f, digits))


def _rgba(c):
    return (c.red, c.green, c.blue, c.alpha)


class _Plot:
    """The mapping between frequency/dB and pixels for one allocation."""

    def __init__(self, width, height, db_range, labels=True):
        self.left = 32 if labels else 2
        self.right = 8 if labels else 2
        self.top = 8 if labels else 2
        self.bottom = 18 if labels else 2
        self.w = max(1.0, width - self.left - self.right)
        self.h = max(1.0, height - self.top - self.bottom)
        self.range = db_range
        self._span = math.log(E.FREQ_MAX / E.FREQ_MIN)

    def x(self, f):
        f = min(max(f, E.FREQ_MIN), E.FREQ_MAX)
        return self.left + self.w * math.log(f / E.FREQ_MIN) / self._span

    def f(self, x):
        t = min(max((x - self.left) / self.w, 0.0), 1.0)
        return E.FREQ_MIN * math.exp(self._span * t)

    def y(self, db):
        return self.top + self.h / 2 - db / self.range * (self.h / 2)

    def db(self, y):
        return -(y - self.top - self.h / 2) / (self.h / 2) * self.range


def _db_range(curve: E.Curve, peak: float, low: float) -> float:
    """±18 dB unless the curve needs more (then ±24): steady, not jumpy."""
    extent = max(abs(peak), abs(low),
                 *(abs(b.gain) for b in curve.bands if b.kind.has_gain),
                 0.0)
    return 24.0 if extent > 17.0 else 18.0


class _Responses:
    """Per-band responses, kept until the band or the grid changes.

    `total` is the bands' shape **without** the preamp, so every point sits
    on the curve it moves; the preamp is drawn as the line the curve must
    stay under (`-preamp`), which is the same fact seen from the other side.
    """

    def __init__(self):
        self._key = None
        self.freqs = []
        self.bands = []
        self.total = []

    def get(self, curve: E.Curve, n: int, rate: int):
        key = (curve, n, rate)
        if key == self._key:
            return self
        old = dict(zip(getattr(self, '_band_keys', ()), self.bands)) \
            if self._key and self._key[1:] == (n, rate) else {}
        self.freqs = E.log_freqs(n)
        self._band_keys = list(curve.bands)
        self.bands = [old.get(b) or b.response(self.freqs, rate)
                      for b in curve.bands]
        total = [0.0] * n
        for b, r in zip(curve.bands, self.bands):
            if b.on:
                total = [t + v for t, v in zip(total, r)]
        self.total = total
        self._key = key
        return self


class EqGraph(Gtk.Box):
    def __init__(self, height=210):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class('eq-graph')
        self.area = Gtk.DrawingArea(hexpand=True, focusable=True,
                                    content_height=height)
        self.area.set_draw_func(self._draw)
        self.append(self.area)
        self._pens = {}
        for cls in PENS:
            pen = Gtk.Label(css_classes=[cls], visible=False)
            self.append(pen)
            self._pens[cls] = pen

        self.curve = E.Curve()
        self.selected = None
        self.bypassed = False
        self.rate = E.RATE
        self.hover = None              # band index under the pointer
        self.pointer = None            # (x, y) while over the graph
        self.dragging = None           # band index while dragging
        self._drag_from = None
        self._resp = _Responses()

        # callbacks, set by the owner
        self.on_select = None          # (index or None)
        self.on_edit = None            # (index, dict of changes, group)
        self.on_edit_end = None        # ()
        self.on_add = None             # (freq, gain)
        self.on_menu = None            # (index, x, y)
        self.on_remove = None          # (index)
        self.on_toggle = None          # (index)

        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect('drag-begin', self._drag_begin)
        drag.connect('drag-update', self._drag_update)
        drag.connect('drag-end', self._drag_end)
        self.area.add_controller(drag)

        click = Gtk.GestureClick(button=0)
        click.connect('pressed', self._pressed)
        self.area.add_controller(click)

        hold = Gtk.GestureLongPress(touch_only=True)
        hold.connect('pressed', self._long_pressed)
        self.area.add_controller(hold)

        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL
            | Gtk.EventControllerScrollFlags.DISCRETE)
        scroll.connect('scroll', self._scrolled)
        self.area.add_controller(scroll)

        motion = Gtk.EventControllerMotion()
        motion.connect('motion', self._motion)
        motion.connect('leave', self._leave)
        self.area.add_controller(motion)

        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self._key)
        self.area.add_controller(keys)
        self.area.connect('notify::has-focus',
                          lambda *_: self.area.queue_draw())

    # ------------------------------------------------------------ public --
    def set_curve(self, curve: E.Curve, selected=None, bypassed=False):
        self.curve = curve
        self.selected = (selected if selected is not None
                         and 0 <= selected < len(curve.bands) else None)
        self.bypassed = bypassed
        self.area.queue_draw()

    # ---------------------------------------------------------- geometry --
    def _plot(self):
        w = self.area.get_width()
        h = self.area.get_height()
        r = self._resp.get(self.curve, self._points(w), self.rate)
        peak = max(r.total) if r.total else 0.0
        low = min(r.total) if r.total else 0.0
        return _Plot(w, h, _db_range(self.curve, peak, low))

    @staticmethod
    def _points(width):
        return int(min(max(width / 2, 120), 400))

    def _node_xy(self, plot, i):
        b = self.curve.bands[i]
        y = plot.y(b.gain if b.kind.has_gain else 0.0)
        return plot.x(b.freq), min(max(y, plot.top), plot.top + plot.h)

    def _hit(self, x, y):
        plot = self._plot()
        best, dist = None, HIT
        # the selected point wins a tie: it is the one being worked on
        order = list(range(len(self.curve.bands)))
        if self.selected is not None:
            order.remove(self.selected)
            order.append(self.selected)
        for i in order:
            nx, ny = self._node_xy(plot, i)
            d = math.hypot(nx - x, ny - y)
            if d <= dist:
                best, dist = i, d
        return best

    # ------------------------------------------------------------- input --
    def _select(self, i):
        if i != self.selected:
            self.selected = i
            if self.on_select:
                self.on_select(i)
        self.area.queue_draw()

    def _drag_begin(self, gesture, x, y):
        self.area.grab_focus()
        i = self._hit(x, y)
        if i is None:
            self.dragging = None
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._select(i)
        self.dragging = i
        b = self.curve.bands[i]
        self._drag_from = (x, y, b.freq, b.gain)

    def _drag_update(self, gesture, dx, dy):
        i = self.dragging
        if i is None or i >= len(self.curve.bands):
            return
        x0, y0, f0, g0 = self._drag_from
        plot = self._plot()
        fine = bool(gesture.get_current_event_state()
                    & Gdk.ModifierType.SHIFT_MASK)
        k = 0.2 if fine else 1.0
        freq = plot.f(plot.x(f0) + dx * k)
        changes = {'freq': round_freq(freq)}
        b = self.curve.bands[i]
        if b.kind.has_gain:
            g = plot.db(plot.y(g0) + dy * k)
            changes['gain'] = round(min(max(g, E.GAIN_MIN), E.GAIN_MAX), 1)
        self.pointer = (x0 + dx, y0 + dy)
        if self.on_edit:
            self.on_edit(i, changes, f'drag-{i}')

    def _drag_end(self, *_):
        if self.dragging is not None and self.on_edit_end:
            self.on_edit_end()
        self.dragging = None
        self.area.queue_draw()

    def _pressed(self, gesture, n_press, x, y):
        button = gesture.get_current_button()
        i = self._hit(x, y)
        if button == Gdk.BUTTON_SECONDARY:
            if i is not None:
                self._select(i)
                if self.on_menu:
                    self.on_menu(i, x, y)
            return
        if button != Gdk.BUTTON_PRIMARY:
            return
        if i is None and n_press == 1:
            self.area.grab_focus()
        if n_press == 2 and i is None and self.on_add:
            plot = self._plot()
            gain = round(min(max(plot.db(y), E.GAIN_MIN), E.GAIN_MAX), 1)
            self.on_add(round_freq(plot.f(x)), gain)

    def _long_pressed(self, _g, x, y):
        i = self._hit(x, y)
        if i is not None:
            self._select(i)
            if self.on_menu:
                self.on_menu(i, x, y)

    def _scrolled(self, _c, _dx, dy):
        i = self.hover if self.hover is not None else self.selected
        if i is None or i >= len(self.curve.bands) or dy == 0:
            return False
        b = self.curve.bands[i]
        q = b.q * (1.12 ** (-dy))
        self._select(i)
        if self.on_edit:
            self.on_edit(i, {'q': round(min(max(q, E.Q_MIN), E.Q_MAX), 2)},
                         f'q-{i}')
        return True

    def _motion(self, _c, x, y):
        self.pointer = (x, y)
        if self.dragging is None:
            self.hover = self._hit(x, y)
        self.area.set_cursor(Gdk.Cursor.new_from_name(
            'grab' if self.hover is not None else 'crosshair', None))
        self.area.queue_draw()

    def _leave(self, *_):
        self.pointer = None
        self.hover = None
        self.area.queue_draw()

    def _key(self, _c, keyval, _code, state):
        n = len(self.curve.bands)
        if not n:
            return False
        i = self.selected if self.selected is not None else 0
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        b = self.curve.bands[i]
        name = Gdk.keyval_name(keyval) or ''
        changes = None
        if name in ('Left', 'Right'):
            step = 2 ** ((1 / 48 if shift else 1 / 12)
                         * (1 if name == 'Right' else -1))
            changes = {'freq': round_freq(b.freq * step)}
        elif name in ('Up', 'Down') and b.kind.has_gain:
            step = (0.1 if shift else 0.5) * (1 if name == 'Up' else -1)
            changes = {'gain': round(b.gain + step, 1)}
        elif name in ('Page_Up', 'Page_Down'):
            f = 1.1 if name == 'Page_Up' else 1 / 1.1
            changes = {'q': round(b.q * f, 2)}
        elif name in ('bracketleft', 'bracketright'):
            self._select((i + (1 if name == 'bracketright' else -1)) % n)
            return True
        elif name == 'space' and self.on_toggle:
            self.on_toggle(i)
            return True
        elif name in ('Delete', 'BackSpace') and self.on_remove:
            self.on_remove(i)
            return True
        if changes is None:
            return False
        if self.selected is None:
            self._select(i)
        if self.on_edit:
            self.on_edit(i, changes, f'key-{i}')
        return True

    # -------------------------------------------------------------- draw --
    def _pen(self, cls, fallback):
        pen = self._pens.get(cls)
        return _rgba(pen.get_color()) if pen else fallback

    def _draw(self, area, cr, width, height):
        fg = _rgba(self.area.get_color())
        lo = self._pen('eq-pen-lo', fg)
        hi = self._pen('eq-pen-hi', fg)
        node = self._pen('eq-pen-node', fg)
        hot = self._pen('eq-pen-hot', fg)
        byp = self._pen('eq-pen-bypass', fg)
        resp = self._resp.get(self.curve, self._points(width), self.rate)
        peak = max(resp.total) if resp.total else 0.0
        plot = _Plot(width, height, _db_range(self.curve, peak,
                                              min(resp.total or [0.0])))
        r, g, b, _ = fg

        # plot ground
        cr.set_source_rgba(r, g, b, 0.035)
        _round_rect(cr, plot.left, plot.top, plot.w, plot.h, 8)
        cr.fill()

        # Where the output reaches full scale: the curve plus the preamp is
        # 0 dB at -preamp.  Above it, it clips; the zone is tinted only when
        # the curve really goes there.
        ceiling = -self.curve.preamp
        clips = peak > ceiling + 0.05 and not self.bypassed
        if clips:
            cr.set_source_rgba(hot[0], hot[1], hot[2], 0.08)
            cr.rectangle(plot.left, plot.top, plot.w,
                         max(0.0, _clampy(plot, plot.y(ceiling)) - plot.top))
            cr.fill()

        # grid
        cr.set_line_width(1)
        for f in GRID_HZ:
            x = round(plot.x(f)) + 0.5
            cr.set_source_rgba(r, g, b, 0.07)
            cr.move_to(x, plot.top)
            cr.line_to(x, plot.top + plot.h)
            cr.stroke()
        step = 6
        d = -plot.range
        while d <= plot.range + 1e-9:
            y = round(plot.y(d)) + 0.5
            cr.set_source_rgba(r, g, b, 0.18 if d == 0 else 0.07)
            cr.set_line_width(1.3 if d == 0 else 1)
            cr.move_to(plot.left, y)
            cr.line_to(plot.left + plot.w, y)
            cr.stroke()
            d += step
        self._labels(cr, plot, fg, width)
        if (clips or abs(self.curve.preamp) >= 0.05) and \
                not self.bypassed and -plot.range < ceiling < plot.range:
            y = round(plot.y(ceiling)) + 0.5
            col = hot if clips else (r, g, b, 0.45)
            cr.set_source_rgba(col[0], col[1], col[2], 0.85 if clips else 0.45)
            cr.set_line_width(1.2)
            cr.set_dash([6, 4])
            cr.move_to(plot.left, y)
            cr.line_to(plot.left + plot.w, y)
            cr.stroke()
            cr.set_dash([])
            wide = plot.w > 420
            label = (('clips above this line' if wide else 'clips') if clips
                     else (f'preamp {self.curve.preamp:+.1f} dB' if wide
                           else f'{self.curve.preamp:+.1f} dB'))
            self._tag(cr, plot, label, y, col)

        pts = [(plot.x(f), plot.y(v)) for f, v in zip(resp.freqs,
                                                        resp.total)]

        # the selected band's own shape, faint, so you see what it does
        sel = self.selected
        if sel is not None and sel < len(self.curve.bands) and \
                self.curve.bands[sel].on and not self.bypassed:
            band_pts = [(plot.x(f), plot.y(v))
                        for f, v in zip(resp.freqs, resp.bands[sel])]
            y0 = plot.y(0)
            cr.move_to(band_pts[0][0], y0)
            for x, y in band_pts:
                cr.line_to(x, _clampy(plot, y))
            cr.line_to(band_pts[-1][0], y0)
            cr.close_path()
            cr.set_source_rgba(node[0], node[1], node[2], 0.12)
            cr.fill()

        # fill under the curve, to the 0 dB line
        if pts:
            y0 = plot.y(0)
            cr.move_to(pts[0][0], y0)
            for x, y in pts:
                cr.line_to(x, _clampy(plot, y))
            cr.line_to(pts[-1][0], y0)
            cr.close_path()
            grad = _gradient(plot, lo, hi, 0.10 if not self.bypassed else 0.03)
            cr.set_source(grad)
            cr.fill()

        # the curve
        if pts:
            cr.set_line_width(2.4)
            cr.set_line_join(1)
            cr.set_line_cap(1)
            cr.move_to(*pts[0])
            for x, y in pts[1:]:
                cr.line_to(x, _clampy(plot, y))
            if self.bypassed:
                cr.set_source_rgba(r, g, b, 0.35)
                cr.set_dash([5, 4])
            else:
                cr.set_source(_gradient(plot, lo, hi, 1.0))
            cr.stroke()
            cr.set_dash([])
        if self.bypassed:
            y = plot.y(0)
            cr.set_source_rgba(*byp[:3], 0.9)
            cr.set_line_width(2.4)
            cr.move_to(plot.left, y)
            cr.line_to(plot.left + plot.w, y)
            cr.stroke()
            self._text(cr, 'Bypassed', plot.left + 8, plot.top + 6, byp,
                       bold=True)

        # points
        focused = self.area.has_focus()
        for i in range(len(self.curve.bands)):
            self._draw_node(cr, plot, i, node, fg, focused)

        self._readout(cr, plot, fg, resp)

    def _draw_node(self, cr, plot, i, node, fg, focused):
        cr.new_path()
        b = self.curve.bands[i]
        x, y = self._node_xy(plot, i)
        sel = i == self.selected
        rad = 8 if sel else 6
        if sel or i == self.hover:
            cr.set_source_rgba(node[0], node[1], node[2], 0.22)
            cr.arc(x, y, rad + 6, 0, 2 * math.pi)
            cr.fill()
        if b.kind.has_gain:
            cr.arc(x, y, rad, 0, 2 * math.pi)
        else:
            # a cut filter has no gain to drag: a diamond on the 0 dB line
            cr.move_to(x, y - rad - 1)
            cr.line_to(x + rad + 1, y)
            cr.line_to(x, y + rad + 1)
            cr.line_to(x - rad - 1, y)
            cr.close_path()
        if b.on:
            cr.set_source_rgba(*node)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.9)
        else:
            cr.set_source_rgba(fg[0], fg[1], fg[2], 0.10)
            cr.fill_preserve()
            cr.set_source_rgba(fg[0], fg[1], fg[2], 0.55)
        cr.set_line_width(2 if sel else 1.5)
        cr.stroke()
        if sel:
            self._text(cr, str(i + 1), x, y - rad - 18, node, bold=True,
                       center=True)
            if focused:
                cr.set_source_rgba(node[0], node[1], node[2], 0.9)
                cr.set_line_width(1.5)
                cr.set_dash([2, 2])
                cr.arc(x, y, rad + 9, 0, 2 * math.pi)
                cr.stroke()
                cr.set_dash([])

    def _labels(self, cr, plot, fg, width):
        dim = (fg[0], fg[1], fg[2], 0.55)
        hz = LABEL_HZ_WIDE if width >= 560 else LABEL_HZ
        for f in hz:
            self._text(cr, E.format_hz(f).replace(' ', ''), plot.x(f),
                       plot.top + plot.h + 3, dim, center=True, small=True)
        for d in (plot.range, plot.range / 2, 0, -plot.range / 2,
                  -plot.range):
            label = f'{d:+.0f}' if d else '0'
            self._text(cr, label, plot.left - 5, plot.y(d) - 7, dim,
                       small=True, right=True)

    def _readout(self, cr, plot, fg, resp):
        i = self.dragging if self.dragging is not None else self.hover
        if i is not None and i < len(self.curve.bands):
            b = self.curve.bands[i]
            x, y = self._node_xy(plot, i)
            text = f'{E.format_hz(b.freq)}'
            if b.kind.has_gain:
                text += f' · {b.gain:+.1f} dB'
            text += f' · Q {b.q:.2f}'
            self._bubble(cr, plot, text, x, y - 34, fg)
            return
        if self.pointer is None or not resp.total:
            return
        px, py = self.pointer
        if not (plot.left <= px <= plot.left + plot.w):
            return
        f = plot.f(px)
        db = self.curve.response([f], self.rate, with_preamp=False)[0]
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.25)
        cr.set_line_width(1)
        cr.move_to(round(px) + 0.5, plot.top)
        cr.line_to(round(px) + 0.5, plot.top + plot.h)
        cr.stroke()
        self._bubble(cr, plot, f'{E.format_hz(f)} · {db:+.1f} dB',
                     px, plot.top + 4, fg)

    def _tag(self, cr, plot, text, y, col):
        """A small label sitting on a line, right-aligned, on a backing
        so the curve passing under it does not make it unreadable."""
        layout = self._layout(text, small=True, bold=True)
        tw, th = layout.get_pixel_size()
        x = plot.left + plot.w - tw - 12
        ty = max(plot.top + 2, y - th - 6)
        cr.set_source_rgba(*self._pen_bg()[:3], 0.85)
        _round_rect(cr, x - 4, ty - 1, tw + 8, th + 2, 4)
        cr.fill()
        cr.set_source_rgba(col[0], col[1], col[2], 0.95)
        cr.move_to(x, ty)
        PangoCairo.show_layout(cr, layout)
        cr.new_path()

    def _bubble(self, cr, plot, text, x, y, fg):
        layout = self._layout(text, small=True, bold=True)
        tw, th = layout.get_pixel_size()
        bx = min(max(x - tw / 2 - 6, plot.left + 2),
                 plot.left + plot.w - tw - 14)
        by = min(max(y, plot.top + 2), plot.top + plot.h - th - 8)
        bg = self._pen_bg()
        cr.set_source_rgba(*bg)
        _round_rect(cr, bx, by, tw + 12, th + 6, 6)
        cr.fill_preserve()
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.18)
        cr.set_line_width(1)
        cr.stroke()
        cr.set_source_rgba(*fg)
        cr.move_to(bx + 6, by + 3)
        PangoCairo.show_layout(cr, layout)
        cr.new_path()

    def _pen_bg(self):
        # an opaque surface under the readout: the view background
        return self._pen('eq-pen-bg', (0.12, 0.12, 0.12, 0.92))

    def _layout(self, text, small=False, bold=False):
        layout = self.area.create_pango_layout(text)
        desc = self.area.get_pango_context().get_font_description().copy()
        size = desc.get_size() or 10 * Pango.SCALE
        if small:
            desc.set_size(int(size * 0.78))
        if bold:
            desc.set_weight(Pango.Weight.BOLD)
        layout.set_font_description(desc)
        return layout

    def _text(self, cr, text, x, y, color, bold=False, small=False,
              center=False, right=False):
        layout = self._layout(text, small=small, bold=bold)
        tw, _th = layout.get_pixel_size()
        if center:
            x -= tw / 2
        elif right:
            x -= tw
        cr.set_source_rgba(*color)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)
        cr.new_path()


def _clampy(plot, y):
    return min(max(y, plot.top - 3), plot.top + plot.h + 3)


def _gradient(plot, lo, hi, alpha):
    import cairo
    g = cairo.LinearGradient(plot.left, 0, plot.left + plot.w, 0)
    g.add_color_stop_rgba(0, lo[0], lo[1], lo[2], alpha)
    g.add_color_stop_rgba(1, hi[0], hi[1], hi[2], alpha)
    return g


def _round_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class CurveThumb(Gtk.DrawingArea):
    """An equalizer's curve at list-row size; its colour is the CSS
    `color` of `.eq-thumb` (dimmed with the row when the device is off)."""

    def __init__(self, curve: E.Curve, width=56, height=30):
        super().__init__(content_width=width, content_height=height,
                         valign=Gtk.Align.CENTER)
        self.add_css_class('eq-thumb')
        self.curve = curve
        self.set_draw_func(self._draw)

    def _draw(self, _a, cr, width, height):
        c = _rgba(self.get_color())
        freqs = E.log_freqs(48)
        resp = self.curve.response(freqs, with_preamp=False)
        rng = 12.0 if max(map(abs, resp + [0.0])) <= 11 else 24.0
        mid = height / 2
        cr.set_source_rgba(c[0], c[1], c[2], 0.16)
        cr.set_line_width(1)
        cr.move_to(1, round(mid) + 0.5)
        cr.line_to(width - 1, round(mid) + 0.5)
        cr.stroke()
        cr.set_source_rgba(*c)
        cr.set_line_width(1.8)
        for k, v in enumerate(resp):
            x = 1 + (width - 2) * k / (len(resp) - 1)
            y = mid - max(min(v / rng, 1), -1) * (mid - 2)
            (cr.move_to if k == 0 else cr.line_to)(x, y)
        cr.stroke()

