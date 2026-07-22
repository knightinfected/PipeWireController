"""Patchbay: live graph view with drag-to-connect patching.

Cairo-drawn node graph of every PipeWire node, port and link (audio + MIDI).
Drag from an output port to an input port to connect, select a link and hit
Delete (or the toolbar button) to disconnect, drag node bodies to arrange
them (positions persist).  Links between running nodes animate to show
signal flow.  Double-click a node for details, latency and metadata editing.
"""

from __future__ import annotations

import math
import time

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from ..backend import graph, prefs, routing, rules
from .widgets import async_call, icon_button, pick_file

NODE_W = 200.0
ROW_H = 18.0
HEADER_H = 26.0
PORT_R = 5.0
COL_X = [60.0, 460.0, 860.0]
KIND_TINT = {
    'source': (0.30, 0.65, 0.35), 'stream-out': (0.35, 0.55, 0.85),
    'sink': (0.85, 0.55, 0.25), 'stream-in': (0.60, 0.45, 0.85),
    'filter': (0.30, 0.70, 0.70), 'duplex': (0.30, 0.70, 0.70),
    'midi': (0.75, 0.35, 0.55), 'other': (0.55, 0.55, 0.55),
}


def _node_height(node):
    rows = max(len(node.inputs), len(node.outputs), 1)
    return HEADER_H + rows * ROW_H + 8


class _View:
    """Pan/zoom state shared by drawing and hit-testing."""
    def __init__(self):
        self.zoom = 1.0
        self.ox = 0.0
        self.oy = 0.0

    def to_world(self, x, y):
        return (x - self.ox) / self.zoom, (y - self.oy) / self.zoom

    def to_screen(self, x, y):
        return x * self.zoom + self.ox, y * self.zoom + self.oy


class GraphArea(Gtk.DrawingArea):
    def __init__(self, page):
        super().__init__(hexpand=True, vexpand=True, focusable=True)
        self.page = page
        self.g = graph.Graph()
        self.pos: dict[str, list] = dict(prefs.get('graph_positions') or {})
        self.view = _View()
        self.show_midi = True
        self.show_monitors = False
        self.selected_link = None       # GLink
        self.selected_node = None       # GNode id
        self.drag_node = None
        self.drag_port = None           # Port being dragged from
        self.drag_xy = (0, 0)
        self.hover_port = None
        self._anim_start = time.monotonic()
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag()
        drag.connect('drag-begin', self._drag_begin)
        drag.connect('drag-update', self._drag_update)
        drag.connect('drag-end', self._drag_end)
        self.add_controller(drag)

        click = Gtk.GestureClick()
        click.connect('pressed', self._pressed)
        self.add_controller(click)

        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect('scroll', self._scroll)
        self.add_controller(scroll)

        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self._key)
        self.add_controller(keys)

        motion = Gtk.EventControllerMotion()
        motion.connect('motion', self._motion)
        self.add_controller(motion)

        self.add_tick_callback(self._tick)

    # ------------------------------------------------------------- data ----
    def set_graph(self, g: graph.Graph):
        self.g = g
        if self.selected_link and not any(
                l.id == self.selected_link.id for l in g.links):
            self.selected_link = None
        if self.selected_node and self.selected_node not in g.nodes:
            self.selected_node = None
        self._ensure_positions()
        self.queue_draw()

    def visible_nodes(self):
        for n in self.g.nodes.values():
            if n.kind == 'midi' and not self.show_midi:
                continue
            yield n

    def _visible_ports(self, node, direction):
        plist = node.inputs if direction == 'in' else node.outputs
        out = []
        for p in plist:
            if p.is_midi and not self.show_midi:
                continue
            if p.is_monitor and not self.show_monitors:
                continue
            out.append(p)
        return out

    def _ensure_positions(self):
        col_y = {}
        order = sorted(self.visible_nodes(),
                       key=lambda n: (graph.KIND_COLUMN.get(n.kind, 1),
                                      n.label.lower()))
        for n in order:
            if n.name in self.pos:
                continue
            col = graph.KIND_COLUMN.get(n.kind, 1)
            y = col_y.get(col, 40.0)
            self.pos[n.name] = [COL_X[col], y]
            col_y[col] = y + _node_height(n) + 26

    def auto_layout(self):
        for n in self.visible_nodes():
            self.pos.pop(n.name, None)
        self._ensure_positions()
        self._save_positions()
        self.queue_draw()

    def _save_positions(self):
        # only keep positions for nodes that still exist to avoid growth
        live = {n.name for n in self.g.nodes.values()}
        prefs.save(graph_positions={k: v for k, v in self.pos.items()
                                    if k in live})

    def fit(self):
        nodes = list(self.visible_nodes())
        if not nodes:
            return
        xs = [self.pos[n.name][0] for n in nodes if n.name in self.pos]
        ys = [self.pos[n.name][1] for n in nodes if n.name in self.pos]
        if not xs:
            return
        x0, y0 = min(xs) - 40, min(ys) - 40
        x1 = max(self.pos[n.name][0] + NODE_W for n in nodes
                 if n.name in self.pos) + 40
        y1 = max(self.pos[n.name][1] + _node_height(n) for n in nodes
                 if n.name in self.pos) + 40
        w, h = self.get_width(), self.get_height()
        if w < 10 or h < 10 or x1 <= x0 or y1 <= y0:
            return
        self.view.zoom = max(0.25, min(1.5, min(w / (x1 - x0), h / (y1 - y0))))
        self.view.ox = -x0 * self.view.zoom + (w - (x1 - x0) * self.view.zoom) / 2
        self.view.oy = -y0 * self.view.zoom + (h - (y1 - y0) * self.view.zoom) / 2
        self.queue_draw()

    # -------------------------------------------------------- geometry -----
    def _port_pos(self, port):
        """World coordinates of a port's connection point."""
        node = self.g.nodes.get(port.node_id)
        if not node or node.name not in self.pos:
            return None
        x, y = self.pos[node.name]
        plist = self._visible_ports(node, port.direction)
        try:
            idx = [p.id for p in plist].index(port.id)
        except ValueError:
            return None
        py = y + HEADER_H + idx * ROW_H + ROW_H / 2
        px = x if port.direction == 'in' else x + NODE_W
        return px, py

    def _hit_port(self, wx, wy):
        for node in self.visible_nodes():
            for direction in ('in', 'out'):
                for p in self._visible_ports(node, direction):
                    pos = self._port_pos(p)
                    if pos and math.hypot(pos[0] - wx, pos[1] - wy) \
                            <= PORT_R * 2.2 / min(self.view.zoom, 1.0):
                        return p
        return None

    def _hit_node(self, wx, wy):
        for node in sorted(self.visible_nodes(),
                           key=lambda n: 0, reverse=True):
            if node.name not in self.pos:
                continue
            x, y = self.pos[node.name]
            if x <= wx <= x + NODE_W and y <= wy <= y + _node_height(node):
                return node
        return None

    def _hit_link(self, wx, wy):
        best, best_d = None, 8.0 / min(self.view.zoom, 1.0)
        for link in self.g.links:
            pts = self._link_points(link)
            if not pts:
                continue
            for t in range(1, 20):
                bx, by = self._bezier(pts, t / 20)
                d = math.hypot(bx - wx, by - wy)
                if d < best_d:
                    best, best_d = link, d
        return best

    def _link_points(self, link):
        op = next((p for n in self.g.nodes.values() for p in n.outputs
                   if p.id == link.out_port), None)
        ip = next((p for n in self.g.nodes.values() for p in n.inputs
                   if p.id == link.in_port), None)
        if not op or not ip:
            return None
        a, b = self._port_pos(op), self._port_pos(ip)
        if not a or not b:
            return None
        dx = max(50.0, abs(b[0] - a[0]) * 0.45)
        return (a, (a[0] + dx, a[1]), (b[0] - dx, b[1]), b)

    @staticmethod
    def _bezier(pts, t):
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
        mt = 1 - t
        x = mt**3 * x0 + 3 * mt**2 * t * x1 + 3 * mt * t**2 * x2 + t**3 * x3
        y = mt**3 * y0 + 3 * mt**2 * t * y1 + 3 * mt * t**2 * y2 + t**3 * y3
        return x, y

    # -------------------------------------------------------- interaction --
    def _pressed(self, gesture, n_press, x, y):
        self.grab_focus()
        wx, wy = self.view.to_world(x, y)
        node = self._hit_node(wx, wy)
        if n_press == 2 and node:
            self.page.show_node_details(node)
            return
        if not self._hit_port(wx, wy):
            link = None if node else self._hit_link(wx, wy)
            self.selected_link = link
            self.selected_node = node.id if node else None
            self.page.update_selection_label()
            self.queue_draw()

    def _drag_begin(self, gesture, x, y):
        wx, wy = self.view.to_world(x, y)
        port = self._hit_port(wx, wy)
        if port:
            self.drag_port = port
            self.drag_xy = (wx, wy)
            return
        node = self._hit_node(wx, wy)
        if node:
            px, py = self.pos[node.name]
            self.drag_node = (node, wx - px, wy - py)
            return
        self.drag_node = None
        self.drag_port = None
        self._pan_start = (self.view.ox, self.view.oy)

    def _drag_update(self, gesture, dx, dy):
        ok, sx, sy = gesture.get_start_point()
        if self.drag_port:
            self.drag_xy = self.view.to_world(sx + dx, sy + dy)
        elif self.drag_node:
            node, gx, gy = self.drag_node
            wx, wy = self.view.to_world(sx + dx, sy + dy)
            self.pos[node.name] = [wx - gx, wy - gy]
        else:
            self.view.ox = self._pan_start[0] + dx
            self.view.oy = self._pan_start[1] + dy
        self.queue_draw()

    def _drag_end(self, gesture, dx, dy):
        if self.drag_port:
            ok, sx, sy = gesture.get_start_point()
            wx, wy = self.view.to_world(sx + dx, sy + dy)
            target = self._hit_port(wx, wy)
            src = self.drag_port
            self.drag_port = None
            if target and target.id != src.id:
                if src.direction == 'out' and target.direction == 'in':
                    self.page.patch(src, target)
                elif src.direction == 'in' and target.direction == 'out':
                    self.page.patch(target, src)
        elif self.drag_node:
            self.drag_node = None
            self._save_positions()
        self.queue_draw()

    def _motion(self, _c, x, y):
        wx, wy = self.view.to_world(x, y)
        port = self._hit_port(wx, wy)
        if port is not self.hover_port:
            self.hover_port = port
            if port:
                node = self.g.nodes.get(port.node_id)
                self.set_tooltip_text(
                    f'{node.label if node else "?"} · {port.name}'
                    + (' (MIDI)' if port.is_midi else ''))
            else:
                self.set_tooltip_text(None)
            self.queue_draw()

    def _scroll(self, controller, dx, dy):
        state = controller.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            factor = 1.1 if dy < 0 else 1 / 1.1
            self.set_zoom(self.view.zoom * factor)
        else:
            self.view.ox -= dx * 40
            self.view.oy -= dy * 40
            self.queue_draw()
        return True

    def set_zoom(self, zoom):
        w, h = self.get_width() / 2, self.get_height() / 2
        wx, wy = self.view.to_world(w, h)
        self.view.zoom = max(0.25, min(2.5, zoom))
        self.view.ox = w - wx * self.view.zoom
        self.view.oy = h - wy * self.view.zoom
        self.queue_draw()

    def _key(self, _c, keyval, _code, _state):
        if keyval == Gdk.KEY_Delete and self.selected_link:
            self.page.disconnect_selected()
            return True
        return False

    def _tick(self, _widget, _clock):
        if self.get_mapped() and any(
                self.g.nodes.get(l.out_node) and self.g.nodes.get(l.in_node)
                and self.g.nodes[l.out_node].is_running
                and self.g.nodes[l.in_node].is_running
                for l in self.g.links):
            self.queue_draw()
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------ drawing --
    def _draw(self, _area, cr, w, h):
        style = self.get_style_context()
        fg = style.get_color()
        dark = fg.red + fg.green + fg.blue > 1.5   # light text = dark theme
        base = 0.10 if dark else 0.96
        cr.set_source_rgb(base, base, base + 0.01)
        cr.paint()

        cr.save()
        cr.translate(self.view.ox, self.view.oy)
        cr.scale(self.view.zoom, self.view.zoom)

        anim = (time.monotonic() - self._anim_start) * 30.0
        for link in self.g.links:
            self._draw_link(cr, link, fg, anim)

        if self.drag_port:
            a = self._port_pos(self.drag_port)
            if a:
                b = self.drag_xy
                if self.drag_port.direction == 'in':
                    a, b = b, a
                dx = max(50.0, abs(b[0] - a[0]) * 0.45)
                cr.move_to(*a)
                cr.curve_to(a[0] + dx, a[1], b[0] - dx, b[1], *b)
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
                cr.set_line_width(2.0)
                cr.set_dash([6, 4])
                cr.stroke()
                cr.set_dash([])

        for node in self.visible_nodes():
            self._draw_node(cr, node, fg, dark)
        cr.restore()

    def _draw_link(self, cr, link, fg, anim):
        pts = self._link_points(link)
        if not pts:
            return
        out_node = self.g.nodes.get(link.out_node)
        in_node = self.g.nodes.get(link.in_node)
        op = next((p for p in (out_node.outputs if out_node else [])
                   if p.id == link.out_port), None)
        is_midi = bool(op and op.is_midi)
        if op and op.is_monitor and not self.show_monitors:
            return
        running = bool(out_node and in_node and out_node.is_running
                       and in_node.is_running)
        selected = self.selected_link and link.id == self.selected_link.id

        cr.move_to(*pts[0])
        cr.curve_to(pts[1][0], pts[1][1], pts[2][0], pts[2][1],
                    pts[3][0], pts[3][1])
        if is_midi:
            r, g_, b = KIND_TINT['midi']
        elif running:
            r, g_, b = (0.35, 0.75, 0.45)
        else:
            r, g_, b = fg.red, fg.green, fg.blue
        alpha = 0.95 if selected else (0.85 if running else 0.35)
        cr.set_source_rgba(r, g_, b, alpha)
        cr.set_line_width(3.2 if selected else (2.2 if running else 1.4))
        if running and not selected:
            cr.set_dash([8, 6], -anim % 14)   # animated flow direction
        cr.stroke()
        cr.set_dash([])

    def _draw_node(self, cr, node, fg, dark):
        if node.name not in self.pos:
            return
        x, y = self.pos[node.name]
        hgt = _node_height(node)
        tint = KIND_TINT.get(node.kind, KIND_TINT['other'])
        selected = self.selected_node == node.id

        # body
        self._rounded(cr, x, y, NODE_W, hgt, 8)
        if dark:
            cr.set_source_rgba(0.16, 0.17, 0.19, 0.96)
        else:
            cr.set_source_rgba(1, 1, 1, 0.96)
        cr.fill()
        # header
        self._rounded(cr, x, y, NODE_W, HEADER_H, 8, top_only=True)
        cr.set_source_rgba(*tint, 0.30 if node.is_running else 0.16)
        cr.fill()
        # outline
        self._rounded(cr, x, y, NODE_W, hgt, 8)
        if selected:
            cr.set_source_rgba(*tint, 1.0)
            cr.set_line_width(2.0)
        else:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.25)
            cr.set_line_width(1.0)
        cr.stroke()

        # title
        layout = PangoCairo.create_layout(cr)
        layout.set_text(node.label, -1)
        layout.set_font_description(
            Pango.FontDescription('Sans Bold 8.5'))
        layout.set_width(int((NODE_W - 16) * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.95)
        cr.move_to(x + 8, y + 5)
        PangoCairo.show_layout(cr, layout)

        # ports
        font = Pango.FontDescription('Sans 7')
        for direction in ('in', 'out'):
            for i, p in enumerate(self._visible_ports(node, direction)):
                py = y + HEADER_H + i * ROW_H + ROW_H / 2
                px = x if direction == 'in' else x + NODE_W
                if p.is_midi:
                    cr.set_source_rgba(*KIND_TINT['midi'], 0.95)
                elif p.is_monitor:
                    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
                else:
                    cr.set_source_rgba(*tint, 0.95)
                r = PORT_R * (1.5 if p is self.hover_port else 1.0)
                cr.arc(px, py, r, 0, 2 * math.pi)
                cr.fill()

                layout = PangoCairo.create_layout(cr)
                layout.set_text(p.name, -1)
                layout.set_font_description(font)
                layout.set_width(int((NODE_W / 2 - 14) * Pango.SCALE))
                layout.set_ellipsize(Pango.EllipsizeMode.END)
                cr.set_source_rgba(fg.red, fg.green, fg.blue,
                                   0.4 if p.is_monitor else 0.7)
                if direction == 'in':
                    cr.move_to(x + 10, py - 7)
                else:
                    ext = layout.get_pixel_extents()[1]
                    cr.move_to(x + NODE_W - 10 - min(ext.width,
                                                     NODE_W / 2 - 14),
                               py - 7)
                PangoCairo.show_layout(cr, layout)

    @staticmethod
    def _rounded(cr, x, y, w, h, r, top_only=False):
        cr.new_path()
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
        if top_only:
            cr.line_to(x + w, y + h)
            cr.line_to(x, y + h)
        else:
            cr.arc(x + w - r, y + h - r, r, 0, 0.5 * math.pi)
            cr.arc(x + r, y + h - r, r, 0.5 * math.pi, math.pi)
        cr.close_path()


class GraphPage:
    def __init__(self, window):
        self.window = window
        self._timer = None
        self._busy = False

        self.area = GraphArea(self)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.area)

        bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8,
                      margin_start=12, margin_end=12)
        bar.append(icon_button('view-refresh-symbolic', 'Refresh now',
                               lambda *_: self.refresh()))
        bar.append(icon_button('view-grid-symbolic', 'Auto-arrange nodes',
                               lambda *_: self.area.auto_layout()))
        bar.append(icon_button('zoom-out-symbolic', 'Zoom out',
                               lambda *_: self.area.set_zoom(
                                   self.area.view.zoom / 1.2)))
        bar.append(icon_button('zoom-in-symbolic', 'Zoom in',
                               lambda *_: self.area.set_zoom(
                                   self.area.view.zoom * 1.2)))
        bar.append(icon_button('zoom-fit-best-symbolic', 'Fit graph',
                               lambda *_: self.area.fit()))
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL,
                                 margin_start=4, margin_end=4))

        self.midi_btn = Gtk.ToggleButton(label='MIDI', active=True,
                                         tooltip_text='Show MIDI ports and '
                                                      'nodes')
        self.midi_btn.add_css_class('flat')
        self.midi_btn.connect('toggled', self._toggle_midi)
        bar.append(self.midi_btn)
        self.mon_btn = Gtk.ToggleButton(label='Monitors', active=False,
                                        tooltip_text='Show monitor ports')
        self.mon_btn.add_css_class('flat')
        self.mon_btn.connect('toggled', self._toggle_monitors)
        bar.append(self.mon_btn)

        self.disc_btn = Gtk.Button(label='Disconnect', sensitive=False,
                                   tooltip_text='Remove the selected link '
                                                '(Delete)')
        self.disc_btn.add_css_class('destructive-action')
        self.disc_btn.connect('clicked', lambda *_: self.disconnect_selected())
        bar.append(self.disc_btn)

        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)

        self.hint = Gtk.Label(label='Drag port → port to connect · '
                                    'double-click a node for details')
        self.hint.add_css_class('dim-label')
        self.hint.add_css_class('caption')
        bar.append(self.hint)

        snap_btn = Gtk.MenuButton(icon_name='document-save-symbolic',
                                  tooltip_text='Routing snapshots')
        self._snap_popover = Gtk.Popover()
        self._snap_popover.connect('show', self._fill_snapshots)
        snap_btn.set_popover(self._snap_popover)
        bar.append(snap_btn)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(bar)
        root.append(Gtk.Separator())
        root.append(sw)
        self.widget = root
        self.widget.connect('map', self._on_map)
        self.widget.connect('unmap', self._on_unmap)
        self._did_fit = False

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
        if not (self.area.drag_node or self.area.drag_port):
            self.refresh()
        return True

    def refresh(self):
        if self._busy:
            return
        self._busy = True

        def done(g, error):
            self._busy = False
            if error or g is None:
                return
            self.area.set_graph(g)
            if not self._did_fit and g.nodes:
                if self.area.get_width() > 10:
                    self._did_fit = True
                    self.area.fit()
                else:                     # canvas not allocated yet
                    GLib.timeout_add(150, self._late_fit)
        async_call(graph.snapshot, done)

    def _late_fit(self):
        if not self._did_fit and self.area.get_width() > 10:
            self._did_fit = True
            self.area.fit()
        return False

    # ------------------------------------------------------------- actions --
    def patch(self, out_port, in_port):
        self.window.mark_user_patch()
        existing = self.area.g.find_link(out_port.id, in_port.id)
        if existing:
            def done(res, e):
                self.window.toast('Disconnected' if res and res[0]
                                  else 'Disconnect failed')
                self.refresh()
            async_call(lambda: graph.disconnect(existing.id), done)
            return

        def done(res, e):
            ok = res and res[0]
            self.window.toast('Connected' if ok else
                              f'Connect failed{": " + res[1] if res and res[1] else ""}')
            self.refresh()
        async_call(lambda: graph.connect(out_port.id, in_port.id), done)

    def disconnect_selected(self):
        link = self.area.selected_link
        if not link:
            return
        self.window.mark_user_patch()

        def done(res, e):
            self.window.toast('Link removed' if res and res[0]
                              else 'Disconnect failed')
            self.area.selected_link = None
            self.update_selection_label()
            self.refresh()
        async_call(lambda: graph.disconnect(link.id), done)

    def update_selection_label(self):
        link = self.area.selected_link
        self.disc_btn.set_sensitive(bool(link))
        if link:
            out_n = self.area.g.nodes.get(link.out_node)
            in_n = self.area.g.nodes.get(link.in_node)
            self.hint.set_label(
                f'{out_n.label if out_n else "?"} → '
                f'{in_n.label if in_n else "?"}')
        elif self.area.selected_node:
            n = self.area.g.nodes.get(self.area.selected_node)
            if n:
                lat = n.latency_ms()
                self.hint.set_label(
                    f'{n.label} · {n.media_class or n.kind} · {n.state}'
                    + (f' · latency {lat:.1f} ms' if lat else ''))
        else:
            self.hint.set_label('Drag port → port to connect · '
                                'double-click a node for details')

    def _toggle_midi(self, btn):
        self.area.show_midi = btn.get_active()
        self.area.queue_draw()

    def _toggle_monitors(self, btn):
        self.area.show_monitors = btn.get_active()
        self.area.queue_draw()

    # ------------------------------------------------------ node details ----
    def show_node_details(self, node):
        NodeDialog(self.window, self, node).present(self.window)

    # --------------------------------------------------------- snapshots ----
    def _fill_snapshots(self, _pop):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12, width_request=360)
        title = Gtk.Label(label='Routing snapshots', xalign=0)
        title.add_css_class('heading')
        box.append(title)
        hint = Gtk.Label(
            label='A snapshot stores all links, default devices and volumes '
                  'by name, so it can be re-applied or shared.',
            xalign=0, wrap=True, max_width_chars=44)
        hint.add_css_class('caption')
        hint.add_css_class('dim-label')
        box.append(hint)

        row = Gtk.Box(spacing=6)
        entry = Gtk.Entry(placeholder_text='Snapshot name', hexpand=True)
        save = Gtk.Button(label='Save')
        save.add_css_class('suggested-action')

        def do_save(*_a):
            name = entry.get_text().strip() or time.strftime('%Y-%m-%d %H:%M')
            self._snap_popover.popdown()
            async_call(lambda: routing.save(name),
                       lambda r, e: self.window.toast(
                           f'Snapshot “{name}” saved' if not e
                           else f'Save failed: {e}'))
        save.connect('clicked', do_save)
        entry.connect('activate', do_save)
        row.append(entry)
        row.append(save)
        box.append(row)

        imp = Gtk.Button()
        imp.set_child(Adw.ButtonContent(icon_name='document-open-symbolic',
                                        label='Import snapshot file…'))
        imp.connect('clicked', self._import_snapshot)
        box.append(imp)

        snaps = routing.list_snapshots()
        if snaps:
            box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        for snap in snaps:
            box.append(self._snapshot_row(snap))
        self._snap_popover.set_child(box)

    def _snapshot_row(self, snap):
        row = Gtk.Box(spacing=6)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        t = Gtk.Label(label=snap.get('name', '?'), xalign=0)
        labels.append(t)
        sub = Gtk.Label(label=f'{len(snap.get("links", []))} links · '
                              f'saved {snap.get("saved", "?")}', xalign=0)
        sub.add_css_class('caption')
        sub.add_css_class('dim-label')
        labels.append(sub)
        row.append(labels)

        def apply(strict):
            self._snap_popover.popdown()
            self.window.mark_user_patch()

            def done(rep, e):
                self.window.toast(f'Snapshot applied: {rep.summary()}'
                                  if rep and not e else 'Apply failed')
                self.refresh()
            async_call(lambda: routing.apply(snap, remove_extra=strict), done)

        row.append(icon_button('media-playback-start-symbolic',
                               'Apply (adds missing links)',
                               lambda *_: apply(False)))
        row.append(icon_button('view-refresh-symbolic',
                               'Apply strictly (also removes links not in '
                               'the snapshot — app streams are kept)',
                               lambda *_: apply(True)))

        def do_export(*_a):
            self._snap_popover.popdown()
            dlg = Gtk.FileDialog(title='Export snapshot',
                                 initial_name=f'{snap.get("name", "routing")}.json')

            def done(d, res):
                try:
                    f = d.save_finish(res)
                except GLib.Error:
                    return
                if f:
                    routing.export(snap, f.get_path())
                    self.window.toast('Snapshot exported')
            dlg.save(self.window, None, done)
        row.append(icon_button('document-send-symbolic', 'Export to file',
                               do_export))

        def do_delete(*_a):
            routing.delete(snap)
            self._fill_snapshots(None)
        row.append(icon_button('user-trash-symbolic', 'Delete snapshot',
                               do_delete))
        return row

    def _import_snapshot(self, *_a):
        self._snap_popover.popdown()

        def picked(path):
            def work():
                return routing.import_file(path)
            async_call(work, lambda snap, e: self.window.toast(
                f'Imported “{snap["name"]}”' if snap and not e
                else f'Import failed: {e}'))
        pick_file(self.window, 'Import routing snapshot', picked,
                  filters=[('Routing snapshots', ['*.json'])])


class NodeDialog(Adw.Dialog):
    """Node details: properties, latency, metadata editor, rename/hide."""

    def __init__(self, window, page, node):
        super().__init__(title=node.label, content_width=560,
                         content_height=640)
        self.window = window
        self.page = page
        self.node = node

        info = Adw.PreferencesGroup(title='Node')
        for title, value in (
                ('Name', node.name),
                ('Class', node.media_class or node.kind),
                ('State', node.state),
                ('Object ID', str(node.id)),
                ('Declared latency', node.latency or '—'),
                ('API', str(node.props.get('device.api', 'virtual')))):
            row = Adw.ActionRow(title=title, subtitle=value,
                                subtitle_selectable=True)
            info.add(row)
        lat = node.latency_ms()
        if lat:
            info.add(Adw.ActionRow(title='Declared latency (ms)',
                                   subtitle=f'{lat:.2f} ms'))

        is_hw = node.name.startswith(('alsa_', 'bluez_'))
        manage = Adw.PreferencesGroup(
            title='Persistent rules',
            description='Stored as WirePlumber rules; applied after a '
                        'WirePlumber restart.' if is_hw else
                        'Only available for hardware (ALSA/Bluetooth) nodes.')
        rule = rules.node_rule(node.name) if is_hw else {}
        self.rename_row = Adw.EntryRow(title='Rename (description)')
        self.rename_row.set_text(rule.get('rename', ''))
        self.rename_row.set_sensitive(is_hw)
        manage.add(self.rename_row)
        self.hide_row = Adw.SwitchRow(
            title='Hide this node',
            subtitle='Disables the node entirely — it disappears from every '
                     'app until re-enabled here.')
        self.hide_row.set_active(bool(rule.get('hide')))
        self.hide_row.set_sensitive(is_hw)
        manage.add(self.hide_row)
        apply_btn = Gtk.Button(label='Save rules', halign=Gtk.Align.END,
                               margin_top=8, sensitive=is_hw)
        apply_btn.add_css_class('suggested-action')
        apply_btn.connect('clicked', self._save_rules)
        manage.add(apply_btn)

        meta = Adw.PreferencesGroup(
            title='Metadata',
            description='Live metadata on this object (e.g. target.object '
                        'to pin a stream to a device). Changes apply '
                        'instantly and last until restart.')
        self.meta_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 spacing=4)
        meta_row = Adw.ActionRow(title='Current values')
        meta.add(meta_row)
        meta.add(self.meta_list)
        edit = Gtk.Box(spacing=6, margin_top=6)
        self.meta_key = Gtk.Entry(placeholder_text='key (e.g. target.object)',
                                  hexpand=True)
        self.meta_val = Gtk.Entry(placeholder_text='value (empty = delete)',
                                  hexpand=True)
        set_btn = Gtk.Button(label='Set')
        set_btn.connect('clicked', self._set_meta)
        edit.append(self.meta_key)
        edit.append(self.meta_val)
        edit.append(set_btn)
        meta.add(edit)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24,
                      margin_start=18, margin_end=18)
        box.append(info)
        box.append(manage)
        box.append(meta)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=560)
        clamp.set_child(box)
        sw.set_child(clamp)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        self.set_child(view)
        self._load_meta()

    def _load_meta(self):
        node_id = self.node.id

        def done(vals, e):
            child = self.meta_list.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self.meta_list.remove(child)
                child = nxt
            if e or not vals:
                lbl = Gtk.Label(label='(no metadata set)', xalign=0,
                                margin_start=12)
                lbl.add_css_class('dim-label')
                lbl.add_css_class('caption')
                self.meta_list.append(lbl)
                return
            for k, v in sorted(vals.items()):
                lbl = Gtk.Label(label=f'{k} = {v}', xalign=0,
                                margin_start=12, selectable=True)
                lbl.add_css_class('caption')
                self.meta_list.append(lbl)
        async_call(lambda: graph.object_metadata(node_id), done)

    def _set_meta(self, _b):
        key = self.meta_key.get_text().strip()
        val = self.meta_val.get_text().strip()
        if not key:
            return
        node_id = self.node.id

        def done(res, e):
            ok = res and res[0]
            self.window.toast('Metadata updated' if ok else 'Failed')
            self._load_meta()
        async_call(lambda: graph.set_metadata(node_id, key, val or None), done)

    def _save_rules(self, _b):
        rename = self.rename_row.get_text().strip()
        hide = self.hide_row.get_active()
        name = self.node.name

        def work():
            rules.set_node_rule(name, rename=rename, hide=hide)
            return True
        async_call(work, lambda r, e: (
            self.window.toast('Rules saved' if not e else f'Failed: {e}'),
            self.window.flag_restart('wireplumber')))
