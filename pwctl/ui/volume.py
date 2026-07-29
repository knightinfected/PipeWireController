"""Selectable volume-control widgets.

Four interchangeable styles, all sharing one tiny API so rows don't care
which one is active:

    ctl = make_volume(style, on_change)   # on_change(value) on user input
    ctl.widget                            # Gtk widget to pack
    ctl.set_value(v) / ctl.get_value()    # programmatic, never fires on_change

Any of them can additionally show the live audio level:

    ctl.set_meter(node.serial)            # None to turn it off again

Metering is tied to the widget's own map/unmap, so a control only captures
while it is actually on screen — scrolling a row out of view, or leaving the
page, stops its capture process on its own.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import GLib, Gtk  # noqa: E402

from ..backend import levels  # noqa: E402

MAX_VOL = 1.5
STEP = 0.05

# Level colours.  levels.level() is already dB-mapped, so these thresholds are
# positions on that scale, not raw amplitudes: -12 dBFS and -3 dBFS.
LEVEL_GREEN = (0.18, 0.76, 0.49)
LEVEL_AMBER = (0.96, 0.76, 0.07)
LEVEL_RED = (0.88, 0.19, 0.20)
GREEN_MAX = (-12.0 - levels.FLOOR_DB) / -levels.FLOOR_DB
AMBER_MAX = (-3.0 - levels.FLOOR_DB) / -levels.FLOOR_DB

STRIP_HEIGHT = 5        # thickness of the level bar under a slider
REDRAW_EPSILON = 0.004  # don't repaint for changes nobody can see


def level_color(pos: float):
    """Colour for a given position on the level scale."""
    if pos <= GREEN_MAX:
        return LEVEL_GREEN
    return LEVEL_AMBER if pos <= AMBER_MAX else LEVEL_RED


def _rounded_rect(cr, x, y, w, h, r):
    import math
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()

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

        # live level state, all zero unless a meter is attached
        self._serial = None
        self._subscribed = False
        self._tick_id = None
        self._level = 0.0
        self._hold = 0.0
        self._live = False

    def get_value(self):
        return self._value

    def _emit(self, value):
        value = max(0.0, min(MAX_VOL, value))
        self._value = value
        if not self._updating:
            self.on_change(value)

    # -- live level --------------------------------------------------------
    # Subclasses provide _level_widget() (what to repaint) and may override
    # _meter_shown() to react when metering starts or stops.

    def _setup_meter(self):
        """Called once by make_volume, after self.widget exists."""
        self.widget.connect('map', lambda *_: self._attach())
        self.widget.connect('unmap', lambda *_: self._detach())
        self.widget.connect('destroy', lambda *_: self._detach())

    def set_meter(self, serial):
        """Show the live level of this node serial (None turns it off)."""
        serial = int(serial) if serial else None
        if serial == self._serial:
            return
        self._detach()
        self._serial = serial
        self._meter_shown(serial is not None)
        if serial is not None and self.widget.get_mapped():
            self._attach()

    def _attach(self):
        if self._subscribed or self._serial is None:
            return
        if not levels.subscribe(self._serial):
            return
        self._subscribed = True
        if self._tick_id is None:
            self._tick_id = self.widget.add_tick_callback(self._tick)

    def _detach(self):
        if self._tick_id is not None:
            self.widget.remove_tick_callback(self._tick_id)
            self._tick_id = None
        if self._subscribed:
            levels.unsubscribe(self._serial)
            self._subscribed = False
        if self._level or self._hold or self._live:
            self._level = self._hold = 0.0
            self._live = False
            self._repaint()

    def _tick(self, _widget, _clock):
        if not self._subscribed:
            return GLib.SOURCE_REMOVE
        level, hold = levels.level(self._serial)
        live = levels.live(self._serial)
        if (abs(level - self._level) > REDRAW_EPSILON
                or abs(hold - self._hold) > REDRAW_EPSILON
                or live != self._live):
            self._level, self._hold, self._live = level, hold, live
            self._repaint()
        return GLib.SOURCE_CONTINUE

    def _repaint(self):
        widget = self._level_widget()
        if widget is not None:
            widget.queue_draw()

    def _level_widget(self):
        return None

    def _meter_shown(self, shown: bool):
        pass


class _LevelStrip:
    """Thin live-level bar drawn under a slider.

    Fills left-to-right, coloured by position so it runs green → amber → red
    as it approaches 0 dBFS, with a peak-hold tick that lags behind.  Draws
    nothing at all when the node is silent, so an idle device reads as idle
    rather than as a meter that happens to be at zero.
    """

    def __init__(self, owner, compact=False):
        self.owner = owner
        self.area = Gtk.DrawingArea()
        self.area.set_content_height(STRIP_HEIGHT)
        if compact:
            self.area.set_size_request(170, STRIP_HEIGHT)
        else:
            self.area.set_hexpand(True)
        self.area.set_draw_func(self._draw)
        self.area.set_visible(False)     # only shown once a meter is attached

    def _draw(self, _area, cr, w, h, *_):
        if w <= 0 or h <= 0:
            return
        radius = h / 2
        _rounded_rect(cr, 0, 0, w, h, radius)
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.18)
        cr.fill()

        level = self.owner._level
        if level <= 0.0:
            return

        cr.save()
        _rounded_rect(cr, 0, 0, w, h, radius)
        cr.clip()
        # colour by position rather than one flat colour, so the bar shades
        # toward red as it fills instead of switching all at once
        steps = max(1, int(w * level))
        for x in range(steps):
            r, g, b = level_color(x / w)
            cr.set_source_rgba(r, g, b, 1.0)
            cr.rectangle(x, 0, 1.2, h)
            cr.fill()

        hold = self.owner._hold
        if hold > 0.0:
            r, g, b = level_color(hold)
            cr.set_source_rgba(r, g, b, 0.95)
            cr.rectangle(max(0.0, hold * w - 1.5), 0, 2.0, h)
            cr.fill()
        cr.restore()


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

        # The strip lives under the slider in a vertical box.  It stays hidden
        # until set_meter() is called, so a control with no meter occupies
        # exactly the space it did before.
        self.strip = _LevelStrip(self, compact)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_valign(Gtk.Align.CENTER)
        box.set_hexpand(not compact)
        box.append(self.scale)
        box.append(self.strip.area)
        self.widget = box

    def _level_widget(self):
        return self.strip.area

    def _meter_shown(self, shown):
        self.strip.area.set_visible(shown)

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

        row = Gtk.Box(spacing=4)
        row.append(minus)
        row.append(self.scale)
        row.append(plus)
        row.set_hexpand(not compact)

        self.strip = _LevelStrip(self, compact)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_valign(Gtk.Align.CENTER)
        box.set_hexpand(not compact)
        box.append(row)
        box.append(self.strip.area)
        self.widget = box

    def _level_widget(self):
        return self.strip.area

    def _meter_shown(self, shown):
        self.strip.area.set_visible(shown)

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
        radius = min(3.0, seg_w / 2)

        # Without a meter this is a volume readout, exactly as before.  With
        # one, the segments show the audio — which is what a segment bar
        # looks like it is showing — and the volume setting becomes a marker.
        metering = self._serial is not None
        vol_pos = self._value / MAX_VOL if MAX_VOL else 0.0

        for i in range(n):
            x = i * (seg_w + gap)
            if metering:
                pos = (i + 0.5) / n
                r, g, b = level_color(pos)
                lit = pos <= self._level
                near_hold = (self._hold > 0.0
                             and abs(pos - self._hold) <= 0.5 / n)
                alpha = 1.0 if (lit or near_hold) else 0.14
                # faint tint below the volume setting, so it stays readable
                # even while the audio is silent
                if not lit and not near_hold and pos <= vol_pos:
                    alpha = 0.26
            else:
                threshold = (i + 0.5) / n * MAX_VOL
                r, g, b = self._seg_color(threshold)
                alpha = 1.0 if threshold <= self._value else 0.16
            cr.set_source_rgba(r, g, b, alpha)
            _rounded_rect(cr, x, 2, seg_w, h - 4, radius)
            cr.fill()

        if metering:
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.85)
            cr.rectangle(max(0.0, min(w - 2.0, vol_pos * w - 1.0)), 0, 2.0, h)
            cr.fill()

    def _level_widget(self):
        return self.area

    def set_value(self, v):
        self._value = v
        self.area.queue_draw()


_IMPL = {'classic': _ClassicVol, 'stepped': _SteppedVol,
         'precision': _PrecisionVol, 'meter': _MeterVol}


def make_volume(style, on_change, compact=False):
    ctl = _IMPL.get(style, _ClassicVol)(on_change, compact)
    ctl._setup_meter()
    return ctl
