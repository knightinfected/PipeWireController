"""Selectable volume-control widgets.

Four interchangeable styles, all sharing one tiny API so rows don't care
which one is active:

    ctl = make_volume(style, on_change)   # on_change(value) on user input
    ctl.widget                            # Gtk widget to pack
    ctl.set_value(v) / ctl.get_value()    # programmatic, never fires on_change
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk  # noqa: E402

MAX_VOL = 1.5
STEP = 0.05

VOLUME_STYLES = [
    ('classic', 'Classic', 'Smooth continuous slider',
     'media-seek-forward-symbolic'),
    ('stepped', 'Stepped', 'Snaps to 5% notches — easy to hit exact values',
     'view-continuous-symbolic'),
    ('precision', 'Precision', 'Slider with −/+ nudge buttons — great on trackpads',
     'zoom-in-symbolic'),
    ('meter', 'LED meter', 'Studio-style segment bar — click or drag to set',
     'power-profile-performance-symbolic'),
]


class _VolBase:
    def __init__(self, on_change):
        self.on_change = on_change
        self._updating = False
        self._value = 0.0

    def get_value(self):
        return self._value

    def _emit(self, value):
        value = max(0.0, min(MAX_VOL, value))
        self._value = value
        if not self._updating:
            self.on_change(value)


class _ClassicVol(_VolBase):
    snap = None

    def __init__(self, on_change, compact=False):
        super().__init__(on_change)
        self.scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, MAX_VOL, 0.01)
        if compact:
            self.scale.set_size_request(170, -1)
        else:
            self.scale.set_hexpand(True)
        self.scale.set_valign(Gtk.Align.CENTER)
        self.scale.add_mark(1.0, Gtk.PositionType.BOTTOM, None)
        self.scale.connect('value-changed', self._changed)
        self.widget = self.scale

    def _changed(self, s):
        if self._updating:
            return
        v = s.get_value()
        if self.snap:
            snapped = round(v / self.snap) * self.snap
            if abs(snapped - v) > 1e-9:
                self._updating = True
                s.set_value(snapped)
                self._updating = False
            v = snapped
        self._emit(v)

    def set_value(self, v):
        self._value = v
        self._updating = True
        self.scale.set_value(v)
        self._updating = False


class _SteppedVol(_ClassicVol):
    snap = STEP

    def __init__(self, on_change, compact=False):
        super().__init__(on_change, compact)
        self.scale.set_increments(STEP, STEP * 4)
        for m in (0.25, 0.5, 0.75, 1.25):
            self.scale.add_mark(m, Gtk.PositionType.BOTTOM, None)
        self.scale.add_css_class('vol-stepped')


class _PrecisionVol(_VolBase):
    def __init__(self, on_change, compact=False):
        super().__init__(on_change)
        self.scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, MAX_VOL, 0.01)
        self.scale.set_hexpand(not compact)
        if compact:
            self.scale.set_size_request(120, -1)
        self.scale.set_valign(Gtk.Align.CENTER)
        self.scale.add_mark(1.0, Gtk.PositionType.BOTTOM, None)
        self.scale.connect('value-changed', self._changed)

        minus = Gtk.Button(label='−', tooltip_text='−5%')
        plus = Gtk.Button(label='+', tooltip_text='+5%')
        for b in (minus, plus):
            b.add_css_class('flat')
            b.add_css_class('circular')
            b.add_css_class('heading')
            b.set_valign(Gtk.Align.CENTER)
        minus.connect('clicked', self._nudge, -STEP)
        plus.connect('clicked', self._nudge, +STEP)

        box = Gtk.Box(spacing=4)
        box.append(minus)
        box.append(self.scale)
        box.append(plus)
        box.set_hexpand(not compact)
        self.widget = box

    def _nudge(self, _b, delta):
        target = round((self._value + delta) / STEP) * STEP
        self.scale.set_value(max(0.0, min(MAX_VOL, target)))

    def _changed(self, s):
        if not self._updating:
            self._emit(s.get_value())

    def set_value(self, v):
        self._value = v
        self._updating = True
        self.scale.set_value(v)
        self._updating = False


class _MeterVol(_VolBase):
    """LED segment bar: green up to 85%, amber to 100%, red above."""

    SEGMENTS = 26
    COLORS = ((0.85, (0.18, 0.76, 0.49)),
              (1.00, (0.96, 0.76, 0.07)),
              (9.99, (0.88, 0.19, 0.20)))

    def __init__(self, on_change, compact=False):
        super().__init__(on_change)
        self.area = Gtk.DrawingArea()
        self.area.set_content_height(24)
        if compact:
            self.area.set_size_request(170, 24)
        else:
            self.area.set_hexpand(True)
        self.area.set_valign(Gtk.Align.CENTER)
        self.area.set_draw_func(self._draw)
        self.area.set_cursor_from_name('pointer')
        self.area.set_tooltip_text('Click or drag to set volume')

        click = Gtk.GestureClick()
        click.connect('pressed', lambda _g, _n, x, _y: self._set_from_x(x))
        self.area.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect('drag-update',
                     lambda g, dx, _dy: self._set_from_x(
                         (g.get_start_point()[1] or 0) + dx))
        self.area.add_controller(drag)
        self.widget = self.area

    def _set_from_x(self, x):
        w = self.area.get_width() or 1
        v = round((x / w) * MAX_VOL / STEP) * STEP
        self._value = max(0.0, min(MAX_VOL, v))
        self.area.queue_draw()
        self._emit(self._value)

    def _seg_color(self, threshold):
        for limit, rgb in self.COLORS:
            if threshold <= limit:
                return rgb
        return self.COLORS[-1][1]

    def _draw(self, area, cr, w, h, *_):
        n = self.SEGMENTS
        gap = 3.0
        seg_w = (w - (n - 1) * gap) / n
        if seg_w <= 0:
            return
        for i in range(n):
            x = i * (seg_w + gap)
            threshold = (i + 0.5) / n * MAX_VOL
            r, g, b = self._seg_color(threshold)
            lit = threshold <= self._value
            cr.set_source_rgba(r, g, b, 1.0 if lit else 0.16)
            radius = min(3.0, seg_w / 2)
            self._rounded_rect(cr, x, 2, seg_w, h - 4, radius)
            cr.fill()

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):
        import math
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def set_value(self, v):
        self._value = v
        self.area.queue_draw()


_IMPL = {'classic': _ClassicVol, 'stepped': _SteppedVol,
         'precision': _PrecisionVol, 'meter': _MeterVol}


def make_volume(style, on_change, compact=False):
    return _IMPL.get(style, _ClassicVol)(on_change, compact)
