"""Patchbay: live graph view with drag-to-connect patching.

Cairo-drawn node graph of every PipeWire node, port and link (audio, MIDI
and, when asked for, video).  Drag from a port to a port to connect, or onto
a node's title to connect every channel at once; select a cable and press
Delete (or the toolbar button) to remove it; drag node bodies to arrange
them (positions persist).  Right-click (or long-press) a node or a cable for
its menu, double-click a node for details, latency and metadata editing.
Links between running nodes animate to show signal flow.
"""

from __future__ import annotations

import math
import time

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import (Adw, Gdk, Gio, GLib, GObject, Gtk,  # noqa: E402
                           Pango, PangoCairo)

from ..backend import graph, prefs, pw, routing, rules
from . import graph_geom as geom
from .widgets import async_call, confirm, esc, icon_button, pick_file

NODE_W = 200.0
ROW_H = 18.0
HEADER_H = 26.0
PORT_R = 5.0
COL_X = [60.0, 460.0, 860.0]
COL_STEP = 400.0         # Arrange: one column per step of the signal
KIND_TINT = {
    'source': (0.30, 0.65, 0.35), 'stream-out': (0.35, 0.55, 0.85),
    'sink': (0.85, 0.55, 0.25), 'stream-in': (0.60, 0.45, 0.85),
    'filter': (0.30, 0.70, 0.70), 'duplex': (0.30, 0.70, 0.70),
    'midi': (0.75, 0.35, 0.55), 'other': (0.55, 0.55, 0.55),
}
VIDEO_TINT = (0.50, 0.55, 0.62)
RUNNING = (0.35, 0.75, 0.45)
REMOVE = (0.86, 0.33, 0.30)          # a cable dropped here would be removed
MEDIA_NAME = {'audio': 'Audio', 'midi': 'MIDI', 'video': 'Video'}
HINT = 'Drag port → port to connect · right-click for more'


def _node_height(node):
    rows = max(len(node.inputs), len(node.outputs), 1)
    return HEADER_H + rows * ROW_H + 8


def _menu_label(text: str) -> str:
    """A menu label shows `_` as a mnemonic; node names are full of them."""
    return text.replace('_', '__')


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


class _Geo:
    """Everything drawing and hit-testing need, worked out once.

    Rebuilt only when something it depends on changes (a new snapshot, a
    node moved, a view toggle), not on every frame: the flow animation
    redraws ~60 times a second and the old per-frame port lookups were
    O(links x ports), which a 128-channel device made noticeable."""
    def __init__(self, port_xy, boxes, sides, paths):
        self.port_xy = port_xy          # port id -> (x, y)
        self.boxes = boxes              # node id -> (x0, y0, x1, y1)
        self.sides = sides              # (node id, 'in'|'out') -> [Port]
        self.paths = paths              # link id -> [cubic segment]


class GraphArea(Gtk.DrawingArea):
    def __init__(self, page):
        super().__init__(hexpand=True, vexpand=True, focusable=True)
        self.page = page
        self.g = graph.Graph()
        self.pos: dict[str, list] = dict(prefs.get('graph_positions') or {})
        self._keys: dict[int, str] = {}      # node id -> position key
        self._linked_ports: set[int] = set()  # port ids carrying a link
        self._ports: dict[int, graph.Port] = {}
        self._z: list[int] = []              # node ids; drawn in this order
        self._geo: _Geo | None = None
        self.view = _View()
        shown = prefs.get('graph_view') or {}
        self.show_midi = bool(shown.get('midi', True))
        self.show_monitors = bool(shown.get('monitors', False))
        # Cameras and screen shares are PipeWire nodes too, but this is an
        # audio app: off until asked for, in the Show menu.
        self.show_video = bool(shown.get('video', False))
        self.selected_link = None       # GLink
        self.selected_node = None       # GNode id
        self.drag_node = None
        self.drag_port = None           # Port being dragged from
        self.drag_xy = (0, 0)
        self.drop = None                # ('port', Port) | ('node', GNode)
        self.refusal = ''               # why the port under a cable can't take it
        self.hover_port = None
        self.hover_node = None          # node id
        self.hover_link = None          # link id
        self._hover_header = False
        self.matches: set[int] | None = None     # search; None = no search
        self._pointer = None            # last pointer position, screen
        self._pan_start = (0.0, 0.0)
        self._pinch_zoom = 1.0
        self._anim_start = time.monotonic()
        self.set_draw_func(self._draw)

        self.menu = Gtk.PopoverMenu(has_arrow=False)
        self.menu.set_parent(self)

        drag = Gtk.GestureDrag()
        drag.connect('drag-begin', self._drag_begin)
        drag.connect('drag-update', self._drag_update)
        drag.connect('drag-end', self._drag_end)
        self.add_controller(drag)

        # The middle button pans from anywhere, even over a node.
        pan = Gtk.GestureDrag(button=Gdk.BUTTON_MIDDLE)
        pan.connect('drag-begin', self._pan_begin)
        pan.connect('drag-update', self._pan_update)
        self.add_controller(pan)

        click = Gtk.GestureClick()
        click.connect('pressed', self._pressed)
        self.add_controller(click)

        menu_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        menu_click.connect('pressed', self._secondary)
        self.add_controller(menu_click)

        hold = Gtk.GestureLongPress(touch_only=True)
        hold.connect('pressed', lambda _g, x, y: self._secondary(None, 1, x, y))
        self.add_controller(hold)

        pinch = Gtk.GestureZoom()
        pinch.connect('begin', self._pinch_begin)
        pinch.connect('scale-changed', self._pinch)
        self.add_controller(pinch)

        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect('scroll', self._scroll)
        self.add_controller(scroll)

        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self._key)
        self.add_controller(keys)

        motion = Gtk.EventControllerMotion()
        motion.connect('motion', self._motion)
        motion.connect('leave', self._leave)
        self.add_controller(motion)

        self.add_tick_callback(self._tick)

    def do_dispose(self):
        # A widget must unparent what it parented, or GTK warns at finalize.
        if self.menu is not None:
            self.menu.unparent()
            self.menu = None
        Gtk.DrawingArea.do_dispose(self)

    # ------------------------------------------------------------- data ----
    def set_graph(self, g: graph.Graph):
        self.g = g
        self._build_keys()
        self._ports = {p.id: p for n in g.nodes.values()
                       for p in n.inputs + n.outputs}
        # cached once per snapshot: _visible_ports() runs per port per frame
        self._linked_ports = {l.out_port for l in g.links}
        self._linked_ports |= {l.in_port for l in g.links}
        known = set(self._z)
        self._z = [n for n in self._z if n in g.nodes] + \
            [n for n in sorted(g.nodes) if n not in known]
        if self.selected_link and not any(
                l.id == self.selected_link.id for l in g.links):
            self.selected_link = None
        if self.selected_node and self.selected_node not in g.nodes:
            self.selected_node = None
        if self.drag_port and self.drag_port.id not in self._ports:
            self.drag_port = None
            self.drop = None
        if self.matches is not None:
            self.matches &= set(g.nodes)
        self._ensure_positions()
        self._invalidate()

    def _invalidate(self):
        self._geo = None
        self.queue_draw()

    def _build_keys(self):
        """Assign each node the key its saved position is stored under.

        node.name is the only identity that survives a restart (ids are
        assigned per session and recycled), but it is *not* unique: two
        windows of the same app are two nodes both called e.g. "Firefox", and
        so are several instances of one module.  Keyed by name alone they
        would share a position, i.e. sit exactly on top of each other with
        their links running to whichever box happened to be drawn last.

        So: nodes that share a name are numbered in id order.  The first keeps
        the bare name, which keeps every position saved by earlier versions
        valid, and the rest get "name#1", "name#2"...
        """
        self._keys = {}
        seen: dict[str, int] = {}
        for nid in sorted(self.g.nodes):
            name = self.g.nodes[nid].name
            n = seen.get(name, 0)
            seen[name] = n + 1
            self._keys[nid] = name if n == 0 else f'{name}#{n}'

    def key(self, node) -> str:
        """Position key for a node (falls back to its name if unknown)."""
        return self._keys.get(node.id, node.name)

    def is_visible(self, node) -> bool:
        if node.kind == 'midi' and not self.show_midi:
            return False
        return self.show_video or not node.is_video

    def visible_nodes(self):
        for n in self.g.nodes.values():
            if self.is_visible(n):
                yield n

    def _z_nodes(self):
        """Visible nodes bottom to top, as drawn."""
        for nid in self._z:
            n = self.g.nodes.get(nid)
            if n is not None and self.is_visible(n):
                yield n

    def raise_node(self, nid):
        if nid in self._z and self._z[-1] != nid:
            self._z.remove(nid)
            self._z.append(nid)
            self.queue_draw()

    def _visible_ports(self, node, direction):
        plist = node.inputs if direction == 'in' else node.outputs
        out = []
        for p in plist:
            if p.is_midi and not self.show_midi:
                continue
            if p.is_video and not self.show_video:
                continue
            # Monitor ports are hidden by default because most of them are
            # unused clutter — but a *connected* one carries real audio, and
            # chaining sinks through monitors is a normal way to build a
            # signal path.  Hiding those made a node that is fed audio look
            # completely unconnected, so connected monitors always show.
            if (p.is_monitor and not self.show_monitors
                    and p.id not in self._linked_ports):
                continue
            out.append(p)
        return out

    def set_shown(self, what: str, on: bool):
        """The Show menu: 'midi', 'monitors' or 'video'."""
        setattr(self, f'show_{what}', bool(on))
        self._ensure_positions()          # a node shown for the first time
        sel = self.g.nodes.get(self.selected_node)
        if sel is not None and not self.is_visible(sel):
            self.selected_node = None
        prefs.save(graph_view={'midi': self.show_midi,
                               'monitors': self.show_monitors,
                               'video': self.show_video})
        self._invalidate()

    def _ensure_positions(self):
        """Give every node a position, without ever covering an existing one.

        Nodes the user has already arranged keep their saved spot; anything
        new is dropped into the first gap in its column that nothing occupies.
        Placing blind (the old behaviour) meant a node appearing mid-session —
        or the second node of a same-named pair — could land exactly on top of
        an arranged one and look like it was missing.
        """
        order = sorted(self.visible_nodes(),
                       key=lambda n: (graph.KIND_COLUMN.get(n.kind, 1),
                                      n.label.lower()))

        # Rectangles already claimed.  Two saved positions that are *exactly*
        # equal are debris from the version that placed every node blind at
        # the top of its column — dragging can't produce pixel-identical
        # coordinates — so the later one is dropped and placed again.  Any
        # other saved position is left alone: it may be arranged deliberately.
        placed: list[tuple[float, float, float, float]] = []
        claimed: set[tuple[float, float]] = set()
        for n in order:
            key = self.key(n)
            if key not in self.pos:
                continue
            x, y = self.pos[key]
            if (x, y) in claimed:
                del self.pos[key]
                continue
            claimed.add((x, y))
            placed.append((x, y, x + NODE_W, y + _node_height(n)))

        for n in order:
            key = self.key(n)
            if key in self.pos:
                continue
            col = graph.KIND_COLUMN.get(n.kind, 1)
            x, h = COL_X[col], _node_height(n)
            y = 40.0
            while True:
                bottoms = [y1 for x0, y0, x1, y1 in placed
                           if x0 < x + NODE_W and x < x1
                           and y0 < y + h and y < y1]
                if not bottoms:
                    break
                y = max(bottoms) + 26       # strictly increasing: terminates
            self.pos[key] = [x, y]
            placed.append((x, y, x + NODE_W, y + h))
        self._geo = None

    def flow_layout(self) -> dict[int, tuple[float, float]]:
        """Where Arrange puts every visible node: one column per step of the
        signal, left to right (apps, then what they feed, then what that
        feeds, devices last), each column ordered to cross as few cables as
        a simple pass can.  A filter chain's input and output halves count
        as joined, so a chain sits between what feeds it and where it goes
        instead of its two halves landing at opposite ends."""
        nodes = {n.id: n for n in self.visible_nodes()}
        edges = [(l.out_node, l.in_node) for l in self.g.links
                 if l.out_node in nodes and l.in_node in nodes]
        groups = []
        by_group: dict[str, list] = {}
        for n in nodes.values():
            grp = n.props.get('node.link-group')
            if grp:
                by_group.setdefault(str(grp), []).append(n)
        for members in by_group.values():
            heads = [m for m in members if m.kind in ('sink', 'stream-in')]
            tails = [m for m in members
                     if m.kind in ('source', 'stream-out', 'duplex')]
            groups += [(h.id, t.id) for h in heads for t in tails]
        kind = {nid: ('video' if n.is_video else n.kind)
                for nid, n in nodes.items()}
        layer = geom.flow_columns(nodes, edges, kind.get, groups)
        order = geom.order_columns(layer, edges + groups,
                                   lambda nid: nodes[nid].label)
        feeds: dict[int, list] = {}
        for u, v in edges + groups:
            if u != v:
                feeds.setdefault(v, []).append(u)
        out = {}
        for col in sorted(order):
            cursor = 40.0
            for nid in order[col]:
                # Level with what feeds it, so a chain's two halves sit on
                # one row and their cables run straight, unless that would
                # overlap the node above.
                ups = [out[u][1] for u in feeds.get(nid, ()) if u in out]
                y = max(cursor, sum(ups) / len(ups) if ups else cursor)
                out[nid] = (COL_X[0] + col * COL_STEP, y)
                cursor = y + _node_height(nodes[nid]) + 26
        return out

    def auto_layout(self):
        for nid, (x, y) in self.flow_layout().items():
            self.pos[self.key(self.g.nodes[nid])] = [x, y]
        self._ensure_positions()
        self._save_positions()
        self._invalidate()
        self.fit()

    def _save_positions(self):
        # only keep positions for nodes that still exist to avoid growth
        live = {self.key(n) for n in self.g.nodes.values()}
        prefs.save(graph_positions={k: v for k, v in self.pos.items()
                                    if k in live})

    def fit(self):
        boxes = list(self._geom().boxes.values())
        if not boxes:
            return
        x0 = min(b[0] for b in boxes) - 40
        y0 = min(b[1] for b in boxes) - 40
        x1 = max(b[2] for b in boxes) + 40
        y1 = max(b[3] for b in boxes) + 40
        w, h = self.get_width(), self.get_height()
        if w < 10 or h < 10 or x1 <= x0 or y1 <= y0:
            return
        self.view.zoom = max(0.25, min(1.5, min(w / (x1 - x0), h / (y1 - y0))))
        self.view.ox = -x0 * self.view.zoom + (w - (x1 - x0) * self.view.zoom) / 2
        self.view.oy = -y0 * self.view.zoom + (h - (y1 - y0) * self.view.zoom) / 2
        self.queue_draw()

    def center_on(self, node):
        box = self._geom().boxes.get(node.id)
        if not box:
            return
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        self.view.ox = self.get_width() / 2 - cx * self.view.zoom
        self.view.oy = self.get_height() / 2 - cy * self.view.zoom
        self.queue_draw()

    # -------------------------------------------------------- geometry -----
    def _geom(self) -> _Geo:
        if self._geo is not None:
            return self._geo
        port_xy, boxes, sides = {}, {}, {}
        for n in self.visible_nodes():
            key = self.key(n)
            if key not in self.pos:
                continue
            x, y = self.pos[key]
            boxes[n.id] = (x, y, x + NODE_W, y + _node_height(n))
            for direction in ('in', 'out'):
                plist = self._visible_ports(n, direction)
                sides[(n.id, direction)] = plist
                px = x if direction == 'in' else x + NODE_W
                for i, p in enumerate(plist):
                    port_xy[p.id] = (px, y + HEADER_H + i * ROW_H + ROW_H / 2)
        paths = {}
        # A cable is drawn only when both of its ports are: what is drawn
        # always matches what has a visible port to attach to.
        for link in self.g.links:
            a, b = port_xy.get(link.out_port), port_xy.get(link.in_port)
            if a is None or b is None:
                continue
            paths[link.id] = geom.cable_path(a, b, boxes.get(link.out_node),
                                             boxes.get(link.in_node))
        self._geo = _Geo(port_xy, boxes, sides, paths)
        return self._geo

    def _port_pos(self, port):
        """World coordinates of a port's connection point."""
        return self._geom().port_xy.get(port.id)

    def _reach(self):
        return PORT_R * 2.2 / min(self.view.zoom, 1.0)

    def _hit_port(self, wx, wy):
        geo, reach = self._geom(), self._reach()
        for node in reversed(list(self._z_nodes())):    # top-most first
            for direction in ('in', 'out'):
                for p in geo.sides.get((node.id, direction), ()):
                    px, py = geo.port_xy[p.id]
                    if math.hypot(px - wx, py - wy) <= reach:
                        return p
        return None

    def _hit_node(self, wx, wy):
        """The node under a point: the top-most one, as it is drawn."""
        boxes = self._geom().boxes
        for node in reversed(list(self._z_nodes())):
            b = boxes.get(node.id)
            if b and b[0] <= wx <= b[2] and b[1] <= wy <= b[3]:
                return node
        return None

    def _hit_link(self, wx, wy):
        best, best_d = None, 8.0 / min(self.view.zoom, 1.0)
        for link in self.g.links:
            path = self._geom().paths.get(link.id)
            if not path:
                continue
            d = geom.distance(path, wx, wy, 20 if len(path) == 1 else 8)
            if d < best_d:
                best, best_d = link, d
        return best

    @staticmethod
    def compatible(src, p) -> bool:
        """Can a cable dragged from `src` end on `p`?  Opposite direction,
        same kind of port (pw-link refuses MIDI into audio)."""
        return (p.id != src.id and p.direction != src.direction
                and p.media == src.media)

    def _drop_target(self, wx, wy):
        """What a cable being dragged would land on here: a port it can take
        (near its dot, or anywhere on its row), or a node's title, which
        means every channel."""
        src = self.drag_port
        if src is None:
            return None
        geo = self._geom()
        want = 'in' if src.direction == 'out' else 'out'
        reach = self._reach()
        for node in reversed(list(self._z_nodes())):
            x0, y0, x1, y1 = geo.boxes.get(node.id, (0, 0, -1, -1))
            if not (x0 - reach <= wx <= x1 + reach and y0 <= wy <= y1):
                continue
            if wy <= y0 + HEADER_H:
                if (node.id != src.node_id
                        and self.page.plan_drop(src, node)):
                    return ('node', node)
                return None
            both = (geo.sides.get((node.id, 'in'))
                    and geo.sides.get((node.id, 'out')))
            if want == 'in':
                lo, hi = x0 - reach, (x0 + x1) / 2 if both else x1
            else:
                lo, hi = (x0 + x1) / 2 if both else x0, x1 + reach
            if not lo <= wx <= hi:
                return None
            for p in geo.sides.get((node.id, want), ()):
                py = geo.port_xy[p.id][1]
                if abs(wy - py) <= ROW_H / 2 and self.compatible(src, p):
                    return ('port', p)
            return None
        p = self._hit_port(wx, wy)
        if p is not None and self.compatible(src, p):
            return ('port', p)
        return None

    def _refusal(self, wx, wy) -> str:
        """Why the port under the pointer can't take this cable, if it can't."""
        src = self.drag_port
        p = self._hit_port(wx, wy)
        if src is None or p is None or p.id == src.id or self.compatible(src, p):
            return ''
        if p.direction == src.direction:
            both = 'inputs' if p.direction == 'in' else 'outputs'
            return f'Both are {both}: a cable runs from an output to an input'
        return (f'{MEDIA_NAME[src.media]} and {MEDIA_NAME[p.media]} ports '
                'can’t be connected')

    def will_remove(self) -> bool:
        """Dropping on this port removes the cable that is already there."""
        if not self.drop or self.drop[0] != 'port' or not self.drag_port:
            return False
        a, b = self.drag_port, self.drop[1]
        o, i = (a, b) if a.direction == 'out' else (b, a)
        return self.g.find_link(o.id, i.id) is not None

    # -------------------------------------------------------- interaction --
    def select_node(self, nid):
        self.selected_node = nid
        self.selected_link = None
        if nid is not None:
            self.raise_node(nid)
        self.page.update_selection_label()
        self.queue_draw()

    def select_link(self, link):
        self.selected_link = link
        self.selected_node = None
        self.page.update_selection_label()
        self.queue_draw()

    def _pressed(self, gesture, n_press, x, y):
        self.grab_focus()
        wx, wy = self.view.to_world(x, y)
        node = self._hit_node(wx, wy)
        if n_press == 2 and node:
            self.page.show_node_details(node)
            return
        if self._hit_port(wx, wy):
            return
        if node:
            self.select_node(node.id)
        else:
            self.select_link(self._hit_link(wx, wy))

    def _secondary(self, _gesture, _n, x, y):
        self.grab_focus()
        wx, wy = self.view.to_world(x, y)
        port = self._hit_port(wx, wy)
        node = (self.g.nodes.get(port.node_id) if port
                else self._hit_node(wx, wy))
        link = None if node else self._hit_link(wx, wy)
        if node:
            self.select_node(node.id)
        elif link:
            self.select_link(link)
        self.page.context_menu(x, y, node=node, link=link)

    def popup_at(self, model, x, y):
        self.menu.set_menu_model(model)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.menu.set_pointing_to(rect)
        self.menu.popup()
        return self.menu

    def _drag_begin(self, gesture, x, y):
        wx, wy = self.view.to_world(x, y)
        port = self._hit_port(wx, wy)
        if port:
            self.drag_port = port
            self.drag_xy = (wx, wy)
            self.drop = None
            self.refusal = ''
            self.page.drag_hint()
            self.queue_draw()
            return
        node = self._hit_node(wx, wy)
        if node:
            self.raise_node(node.id)
            px, py = self.pos[self.key(node)]
            self.drag_node = (node, wx - px, wy - py)
            return
        self.drag_node = None
        self.drag_port = None
        self._pan_start = (self.view.ox, self.view.oy)

    def _drag_update(self, gesture, dx, dy):
        ok, sx, sy = gesture.get_start_point()
        if self.drag_port:
            self.drag_xy = self.view.to_world(sx + dx, sy + dy)
            self.drop = self._drop_target(*self.drag_xy)
            self.refusal = '' if self.drop else self._refusal(*self.drag_xy)
            self.page.drag_hint()
        elif self.drag_node:
            node, gx, gy = self.drag_node
            wx, wy = self.view.to_world(sx + dx, sy + dy)
            self.pos[self.key(node)] = [wx - gx, wy - gy]
            self._geo = None
        else:
            self.view.ox = self._pan_start[0] + dx
            self.view.oy = self._pan_start[1] + dy
        self.queue_draw()

    def _drag_end(self, gesture, dx, dy):
        if self.drag_port:
            src, drop, refusal = self.drag_port, self.drop, self.refusal
            self.drag_port = None
            self.drop = None
            self.refusal = ''
            self.page.update_selection_label()
            if drop and drop[0] == 'port':
                if src.direction == 'out':
                    self.page.patch(src, drop[1])
                else:
                    self.page.patch(drop[1], src)
            elif drop and drop[0] == 'node':
                self.page.patch_pairs(self.page.plan_drop(src, drop[1]))
            elif refusal:
                self.page.window.toast(refusal)
        elif self.drag_node:
            self.drag_node = None
            self._save_positions()
        self.queue_draw()

    def cancel_drag(self):
        if self.drag_port:
            self.drag_port = None
            self.drop = None
            self.refusal = ''
            self.page.update_selection_label()
            self.queue_draw()
            return True
        return False

    def _pan_begin(self, _g, _x, _y):
        self._pan_start = (self.view.ox, self.view.oy)

    def _pan_update(self, _g, dx, dy):
        self.view.ox = self._pan_start[0] + dx
        self.view.oy = self._pan_start[1] + dy
        self.queue_draw()

    def _pinch_begin(self, _g, _seq):
        self._pinch_zoom = self.view.zoom

    def _pinch(self, gesture, scale):
        ok, cx, cy = gesture.get_bounding_box_center()
        self.set_zoom(self._pinch_zoom * scale, (cx, cy) if ok else None)

    def _tooltip(self, port, node, link):
        if port:
            n = self.g.nodes.get(port.node_id)
            return (f'{n.label if n else "?"} · {port.name}'
                    + ('' if port.media == 'audio'
                       else f' ({MEDIA_NAME[port.media]})'))
        if link:
            o, i = self._ports.get(link.out_port), self._ports.get(link.in_port)
            on, inn = self.g.nodes.get(link.out_node), self.g.nodes.get(
                link.in_node)
            return (f'{on.label if on else "?"} · {o.name if o else "?"}  →  '
                    f'{inn.label if inn else "?"} · {i.name if i else "?"}')
        if node and self._hover_header:
            return (f'{node.label}\n{node.media_class or node.kind} · '
                    f'{node.state}')
        return None

    def _motion(self, _c, x, y):
        self._pointer = (x, y)
        if self.drag_port or self.drag_node:
            return
        wx, wy = self.view.to_world(x, y)
        port = self._hit_port(wx, wy)
        node = (self.g.nodes.get(port.node_id) if port
                else self._hit_node(wx, wy))
        link = None if node else self._hit_link(wx, wy)
        header = bool(node and not port and wy <= self.pos[
            self.key(node)][1] + HEADER_H)
        state = (port, node.id if node else None, link.id if link else None,
                 header)
        if state != (self.hover_port, self.hover_node, self.hover_link,
                     self._hover_header):
            (self.hover_port, self.hover_node, self.hover_link,
             self._hover_header) = state
            self.set_tooltip_text(self._tooltip(port, node, link))
            self.queue_draw()

    def _leave(self, _c):
        self._pointer = None
        if self.hover_port or self.hover_node or self.hover_link:
            self.hover_port = self.hover_node = self.hover_link = None
            self.set_tooltip_text(None)
            self.queue_draw()

    def _scroll(self, controller, dx, dy):
        state = controller.get_current_event_state()
        # A touchpad scrolls in pixels, a wheel in clicks: 40 px per click,
        # but never 40 px per touchpad pixel.
        pixels = (hasattr(controller, 'get_unit')
                  and controller.get_unit() == Gdk.ScrollUnit.SURFACE)
        if state & Gdk.ModifierType.CONTROL_MASK:
            factor = math.exp(-dy * 0.01) if pixels else 1.1 ** -dy
            self.set_zoom(self.view.zoom * factor, self._pointer)
        else:
            step = 1.0 if pixels else 40.0
            self.view.ox -= dx * step
            self.view.oy -= dy * step
            self.queue_draw()
        return True

    def set_zoom(self, zoom, anchor=None):
        """Zoom, keeping the point under `anchor` (screen) still: the
        pointer for Ctrl+scroll and a pinch, else the middle."""
        if anchor is None:
            anchor = (self.get_width() / 2, self.get_height() / 2)
        ax, ay = anchor
        wx, wy = self.view.to_world(ax, ay)
        self.view.zoom = max(0.25, min(2.5, zoom))
        self.view.ox = ax - wx * self.view.zoom
        self.view.oy = ay - wy * self.view.zoom
        self.queue_draw()

    def _key(self, _c, keyval, _code, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            if self.cancel_drag():
                return True
            if self.page.close_search():
                return True
            if self.selected_link or self.selected_node:
                self.select_node(None)
                return True
            return False
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete, Gdk.KEY_BackSpace) \
                and self.selected_link:
            self.page.disconnect_selected()
            return True
        if ctrl and keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.page.open_search()
            return True
        if ctrl and keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.set_zoom(self.view.zoom * 1.2)
            return True
        if ctrl and keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.set_zoom(self.view.zoom / 1.2)
            return True
        if ctrl and keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self.fit()
            return True
        if keyval == Gdk.KEY_Menu or (keyval == Gdk.KEY_F10 and state
                                      & Gdk.ModifierType.SHIFT_MASK):
            self._menu_from_keyboard()
            return True
        return False

    def _menu_from_keyboard(self):
        node = self.g.nodes.get(self.selected_node) \
            if self.selected_node else None
        link = self.selected_link
        geo = self._geom()
        if node and node.id in geo.boxes:
            b = geo.boxes[node.id]
            x, y = self.view.to_screen(b[0] + 16, b[1] + HEADER_H)
        elif link and link.id in geo.paths:
            seg = geo.paths[link.id][len(geo.paths[link.id]) // 2]
            x, y = self.view.to_screen(*geom.bezier(seg, 0.5))
        else:
            x, y = self.get_width() / 2, self.get_height() / 2
        self.page.context_menu(x, y, node=node, link=link)

    def set_search(self, text: str) -> list:
        """Light up the nodes whose name holds `text`; the rest fade.
        Returns the matches top to bottom, left to right."""
        text = (text or '').strip().casefold()
        if not text:
            self.matches = None
            self.queue_draw()
            return []
        found = [n for n in self.visible_nodes()
                 if text in n.label.casefold() or text in n.name.casefold()]
        self.matches = {n.id for n in found}
        self.queue_draw()
        boxes = self._geom().boxes
        return sorted((n for n in found if n.id in boxes),
                      key=lambda n: (boxes[n.id][0], boxes[n.id][1]))

    def _tick(self, _widget, _clock):
        # Redraw every frame only while a cable on screen carries sound: a
        # hidden camera or screen share would otherwise keep the page busy.
        if self.get_mapped():
            drawn, nodes = self._geom().paths, self.g.nodes
            if any(l.id in drawn
                   and nodes.get(l.out_node) and nodes.get(l.in_node)
                   and nodes[l.out_node].is_running
                   and nodes[l.in_node].is_running
                   for l in self.g.links):
                self.queue_draw()
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------ drawing --
    def _link_focus(self, link) -> bool:
        """Is this cable one the user is looking at right now?"""
        if self.selected_link and link.id == self.selected_link.id:
            return True
        if link.id == self.hover_link:
            return True
        if self.hover_port and self.hover_port.id in (link.out_port,
                                                      link.in_port):
            return True
        focus = {self.selected_node, self.hover_node} - {None}
        return link.out_node in focus or link.in_node in focus

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
        geo = self._geom()

        anim = (time.monotonic() - self._anim_start) * 30.0
        # Fade the cables nobody is looking at while a node is selected, a
        # search is on, or a new cable is being dragged.
        quiet = (self.selected_node is not None or self.matches is not None
                 or self.drag_port is not None)
        front = []
        for link in self.g.links:
            path = geo.paths.get(link.id)
            if not path:
                continue
            if not self.drag_port and self._link_focus(link):
                front.append((link, path))
            else:
                self._draw_link(cr, link, path, fg, anim,
                                'dim' if quiet else 'normal')
        for link, path in front:
            self._draw_link(cr, link, path, fg, anim, 'focus')

        if self.drag_port:
            self._draw_drag(cr, fg)

        for node in self._z_nodes():
            self._draw_node(cr, node, fg, dark, geo)
        cr.restore()

    def _draw_drag(self, cr, fg):
        a = self._port_pos(self.drag_port)
        if not a:
            return
        b = self.drag_xy
        if self.drop and self.drop[0] == 'port':
            b = self._port_pos(self.drop[1]) or b
        if self.drag_port.direction == 'in':
            a, b = b, a
        seg = geom.forward_path(a, b)[0]
        cr.move_to(*seg[0])
        cr.curve_to(*seg[1], *seg[2], *seg[3])
        if self.will_remove():
            cr.set_source_rgba(*REMOVE, 0.95)
            cr.set_line_width(2.4)
        elif self.drop:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
            cr.set_line_width(2.4)
        else:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
            cr.set_line_width(2.0)
            cr.set_dash([6, 4])
        cr.stroke()
        cr.set_dash([])

    def _draw_link(self, cr, link, path, fg, anim, mode):
        out_node = self.g.nodes.get(link.out_node)
        in_node = self.g.nodes.get(link.in_node)
        op = self._ports.get(link.out_port)
        is_midi = bool(op and op.is_midi)
        running = bool(out_node and in_node and out_node.is_running
                       and in_node.is_running)
        selected = self.selected_link and link.id == self.selected_link.id

        cr.move_to(*path[0][0])
        for seg in path:
            cr.curve_to(*seg[1], *seg[2], *seg[3])
        if is_midi:
            r, g_, b = KIND_TINT['midi']
        elif running:
            r, g_, b = RUNNING
        else:
            r, g_, b = fg.red, fg.green, fg.blue
        if selected:
            alpha, width = 0.95, 3.2
        elif mode == 'focus':
            alpha, width = (0.95, 2.8) if running else (0.8, 2.2)
        elif mode == 'dim':
            alpha, width = (0.22, 1.6) if running else (0.12, 1.2)
        else:
            alpha, width = (0.85, 2.2) if running else (0.35, 1.4)
        cr.set_source_rgba(r, g_, b, alpha)
        cr.set_line_width(width)
        if running and not selected:
            cr.set_dash([8, 6], -anim % 14)   # animated flow direction
        cr.stroke()
        cr.set_dash([])

    def _draw_node(self, cr, node, fg, dark, geo):
        box = geo.boxes.get(node.id)
        if box is None:
            return
        x, y = box[0], box[1]
        hgt = box[3] - box[1]
        tint = VIDEO_TINT if node.is_video else KIND_TINT.get(
            node.kind, KIND_TINT['other'])
        selected = self.selected_node == node.id
        drop_here = bool(self.drop and self.drop[0] == 'node'
                         and self.drop[1].id == node.id)
        matched = self.matches is not None and node.id in self.matches
        src = self.drag_port
        faded = (self.matches is not None and not matched) or bool(
            src and node.id != src.node_id and not any(
                self.compatible(src, p)
                for d in ('in', 'out')
                for p in geo.sides.get((node.id, d), ())))
        if faded:
            cr.push_group()

        # body
        self._rounded(cr, x, y, NODE_W, hgt, 8)
        if dark:
            cr.set_source_rgba(0.16, 0.17, 0.19, 0.96)
        else:
            cr.set_source_rgba(1, 1, 1, 0.96)
        cr.fill()
        # header
        self._rounded(cr, x, y, NODE_W, HEADER_H, 8, top_only=True)
        cr.set_source_rgba(*tint, 0.55 if drop_here else
                           (0.30 if node.is_running else 0.16))
        cr.fill()
        # outline
        self._rounded(cr, x, y, NODE_W, hgt, 8)
        if selected or drop_here or matched:
            cr.set_source_rgba(*tint, 1.0)
            cr.set_line_width(2.0)
        elif self.hover_node == node.id and not src:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.set_line_width(1.0)
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
        target = self.drop[1] if self.drop and self.drop[0] == 'port' \
            else None
        for direction in ('in', 'out'):
            for p in geo.sides.get((node.id, direction), ()):
                px, py = geo.port_xy[p.id]
                ok = src is None or p.id == src.id or self.compatible(src, p)
                fade = 1.0 if ok else 0.25
                if p.is_midi:
                    cr.set_source_rgba(*KIND_TINT['midi'], 0.95 * fade)
                elif p.is_monitor:
                    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35 * fade)
                else:
                    cr.set_source_rgba(*tint, 0.95 * fade)
                big = p is self.hover_port or (target is not None
                                               and p.id == target.id)
                r = PORT_R * (1.5 if big else (1.15 if src and ok
                                               and p.id != src.id else 1.0))
                cr.arc(px, py, r, 0, 2 * math.pi)
                cr.fill()
                if target is not None and p.id == target.id:
                    cr.arc(px, py, PORT_R * 2.3, 0, 2 * math.pi)
                    cr.set_source_rgba(*(REMOVE if self.will_remove()
                                         else (fg.red, fg.green, fg.blue)),
                                       0.8)
                    cr.set_line_width(1.5)
                    cr.stroke()

                layout = PangoCairo.create_layout(cr)
                layout.set_text(p.name, -1)
                layout.set_font_description(font)
                layout.set_width(int((NODE_W / 2 - 14) * Pango.SCALE))
                layout.set_ellipsize(Pango.EllipsizeMode.END)
                cr.set_source_rgba(fg.red, fg.green, fg.blue,
                                   (0.4 if p.is_monitor else 0.7) * fade)
                if direction == 'in':
                    cr.move_to(x + 10, py - 7)
                else:
                    ext = layout.get_pixel_extents()[1]
                    cr.move_to(x + NODE_W - 10 - min(ext.width,
                                                     NODE_W / 2 - 14),
                               py - 7)
                PangoCairo.show_layout(cr, layout)

        if faded:
            cr.pop_group_to_source()
            cr.paint_with_alpha(0.35)

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
        self._again = False      # a refresh was asked for while one ran
        self._found = []         # search matches, in order
        self._found_at = -1

        self.area = GraphArea(self)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.area)

        # Zoom sits on the canvas, as on a map, which is what lets the
        # toolbar fit a 360 px window (DR-013 layer 3).
        canvas = Gtk.Overlay()
        canvas.set_child(sw)
        zoom = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.END,
                       margin_end=12, margin_bottom=12)
        zoom.add_css_class('linked')
        for icon, tip, fn in (
                ('zoom-out-symbolic', 'Zoom out (Ctrl+−)',
                 lambda *_: self.area.set_zoom(self.area.view.zoom / 1.2)),
                ('zoom-in-symbolic', 'Zoom in (Ctrl++)',
                 lambda *_: self.area.set_zoom(self.area.view.zoom * 1.2)),
                ('zoom-fit-best-symbolic', 'Fit the whole graph (Ctrl+0)',
                 lambda *_: self.area.fit())):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.add_css_class('osd')
            b.connect('clicked', fn)
            zoom.append(b)
        canvas.add_overlay(zoom)

        self._actions = self._make_actions()

        bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8,
                      margin_start=12, margin_end=12)
        bar.append(icon_button('view-refresh-symbolic', 'Refresh now',
                               lambda *_: self.refresh()))
        bar.append(icon_button('view-grid-symbolic',
                               'Arrange by signal flow: sources on the left, '
                               'devices on the right',
                               lambda *_: self.area.auto_layout()))
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL,
                                 margin_start=4, margin_end=4))

        show = Gio.Menu()
        show.append('MIDI', 'pb.show-midi')
        show.append('Unused monitor ports', 'pb.show-monitors')
        show.append('Video (cameras, screen sharing)', 'pb.show-video')
        self.show_btn = Gtk.MenuButton(icon_name='view-reveal-symbolic',
                                       tooltip_text='What to show',
                                       menu_model=show)
        self.show_btn.add_css_class('flat')
        bar.append(self.show_btn)
        bar.append(icon_button('insert-link-symbolic',
                               'Connect channels: many cables at once, by '
                               'range',
                               lambda *_: self.connect_dialog()))
        self.search_btn = Gtk.ToggleButton(icon_name='system-search-symbolic',
                                           tooltip_text='Find a node (Ctrl+F)')
        self.search_btn.add_css_class('flat')
        bar.append(self.search_btn)

        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)

        # The hint has to be able to disappear.  Every page lives in the same
        # Gtk.Stack, so the stack's minimum width is the widest page's — an
        # un-ellipsized sentence here was making *all* pages demand 960 px and
        # get clipped in a tiled or half-screen window.
        self.hint = Gtk.Label(label=HINT, ellipsize=Pango.EllipsizeMode.END)
        self.hint.add_css_class('dim-label')
        self.hint.add_css_class('caption')
        bar.append(self.hint)

        self.disc_btn = icon_button('edit-delete-symbolic',
                                    'Remove the selected cable (Delete)',
                                    lambda *_: self.disconnect_selected())
        self.disc_btn.set_sensitive(False)
        bar.append(self.disc_btn)

        snap_btn = Gtk.MenuButton(icon_name='document-save-symbolic',
                                  tooltip_text='Routing snapshots')
        self._snap_popover = Gtk.Popover()
        self._snap_popover.connect('show', self._fill_snapshots)
        snap_btn.set_popover(self._snap_popover)
        bar.append(snap_btn)

        # Find a node: typing on the canvas opens it too.
        self.search = Gtk.SearchEntry(placeholder_text='Find a node',
                                      hexpand=True)
        self.search.connect('search-changed', self._search_changed)
        self.search.connect('activate', lambda *_: self._search_step(1))
        self.search.connect('next-match', lambda *_: self._search_step(1))
        self.search.connect('previous-match',
                            lambda *_: self._search_step(-1))
        self.search.connect('stop-search', lambda *_: self.close_search())
        self.found_label = Gtk.Label(xalign=1)
        self.found_label.add_css_class('dim-label')
        self.found_label.add_css_class('caption')
        sbox = Gtk.Box(spacing=8)
        sbox.append(self.search)
        sbox.append(self.found_label)
        clamp = Adw.Clamp(maximum_size=520, child=sbox)
        self.search_bar = Gtk.SearchBar(child=clamp, show_close_button=True)
        self.search_bar.connect_entry(self.search)
        self.search_bar.set_key_capture_widget(self.area)
        self.search_bar.bind_property(
            'search-mode-enabled', self.search_btn, 'active',
            GObject.BindingFlags.BIDIRECTIONAL
            | GObject.BindingFlags.SYNC_CREATE)
        self.search_bar.connect('notify::search-mode-enabled',
                                self._search_mode)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(bar)
        root.append(Gtk.Separator())
        root.append(self.search_bar)
        root.append(canvas)
        root.insert_action_group('pb', self._actions)
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
        # Skipped, not queued, when a snapshot is in flight: the next tick is
        # 3 s away, and queueing would make a slow machine poll nonstop.
        if not (self.area.drag_node or self.area.drag_port or self._busy):
            self.refresh()
        return True

    def refresh(self):
        if self._busy:
            # The snapshot in flight may predate the patch this call is about
            # (a link made or removed), so take one more when it lands rather
            # than drop the call until the next tick.
            self._again = True
            return
        self._busy = True

        def done(g, error):
            self._busy = False
            if self._again:
                self._again = False
                self.refresh()
            if error or g is None:
                return
            self.area.set_graph(g)
            if self.search_bar.get_search_mode():
                self._search_changed(self.search, keep=True)
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
    def _make_actions(self):
        group = Gio.SimpleActionGroup()
        for what in ('midi', 'monitors', 'video'):
            act = Gio.SimpleAction.new_stateful(
                f'show-{what}', None,
                GLib.Variant('b', getattr(self.area, f'show_{what}')))

            def flip(a, _p, what=what):
                on = not a.get_state().get_boolean()
                a.set_state(GLib.Variant('b', on))
                self.area.set_shown(what, on)
            act.connect('activate', flip)
            group.add_action(act)

        def add(name, fn, vtype=None):
            act = Gio.SimpleAction.new(
                name, GLib.VariantType.new(vtype) if vtype else None)
            act.connect('activate', lambda _a, p: fn(p.unpack() if p else None))
            group.add_action(act)

        add('details', lambda nid: self._with_node(nid, self.show_node_details),
            'x')
        add('connect-all', self._connect_all, 's')
        add('connect-dialog', lambda s: self._dialog_from_target(s), 's')
        add('disconnect-node',
            lambda nid: self._with_node(nid, self.disconnect_node), 'x')
        add('disconnect-link', self._disconnect_link_id, 'x')
        add('arrange', lambda _p: self.area.auto_layout())
        add('fit', lambda _p: self.area.fit())
        return group

    def _with_node(self, nid, fn):
        node = self.area.g.nodes.get(nid)
        if node is not None:
            fn(node)

    def names(self) -> dict[int, str]:
        """A name for every node that tells same-named ones apart."""
        nodes = sorted(self.area.g.nodes.values(), key=lambda n: n.id)
        count: dict[str, int] = {}
        for n in nodes:
            count[n.label] = count.get(n.label, 0) + 1
        seen: dict[str, int] = {}
        out = {}
        for n in nodes:
            if count[n.label] == 1:
                out[n.id] = n.label
                continue
            seen[n.label] = seen.get(n.label, 0) + 1
            out[n.id] = (f'{n.label} ({n.name})' if n.name != n.label
                         and sum(m.name == n.name for m in nodes) == 1
                         else f'{n.label} #{seen[n.label]}')
        return out

    def _targets(self, node, direction):
        """Nodes this one can be patched to as a whole, by name."""
        names = self.names()
        out = []
        for other in self.area.visible_nodes():
            if other.id == node.id:
                continue
            pairs = (graph.plan_nodes(node, other) if direction == 'out'
                     else graph.plan_nodes(other, node))
            if pairs:
                out.append((names[other.id], other))
        return sorted(out, key=lambda t: t[0].casefold())

    def context_menu(self, x, y, node=None, link=None):
        """The right-click / long-press menu for a node, a cable or the
        empty canvas."""
        menu = Gio.Menu()
        names = self.names()
        if node is not None:
            top = Gio.Menu()
            item = Gio.MenuItem.new('Details…', None)
            item.set_action_and_target_value('pb.details',
                                             GLib.Variant('x', node.id))
            top.append_item(item)
            menu.append_section(None, top)
            patch = Gio.Menu()
            for direction, title in (('out', 'Connect all channels to'),
                                     ('in', 'Connect all channels from')):
                targets = self._targets(node, direction)
                if not targets:
                    continue
                sub = Gio.Menu()
                for label, other in targets:
                    pair = (f'{node.id}>{other.id}' if direction == 'out'
                            else f'{other.id}>{node.id}')
                    it = Gio.MenuItem.new(_menu_label(label), None)
                    it.set_action_and_target_value('pb.connect-all',
                                                   GLib.Variant('s', pair))
                    sub.append_item(it)
                patch.append_submenu(title, sub)
            item = Gio.MenuItem.new('Connect channels…', None)
            item.set_action_and_target_value(
                'pb.connect-dialog', GLib.Variant(
                    's', f'{node.id}>' if node.outputs else f'>{node.id}'))
            patch.append_item(item)
            menu.append_section(None, patch)
            n_links = sum(1 for l in self.area.g.links
                          if node.id in (l.out_node, l.in_node))
            if n_links:
                end = Gio.Menu()
                item = Gio.MenuItem.new(
                    f'Remove all {n_links} cables' if n_links > 1
                    else 'Remove its cable', None)
                item.set_action_and_target_value('pb.disconnect-node',
                                                 GLib.Variant('x', node.id))
                end.append_item(item)
                menu.append_section(None, end)
        elif link is not None:
            sec = Gio.Menu()
            item = Gio.MenuItem.new('Remove cable', None)
            item.set_action_and_target_value('pb.disconnect-link',
                                             GLib.Variant('x', link.id))
            sec.append_item(item)
            item = Gio.MenuItem.new(_menu_label(
                f'Connect channels: {names.get(link.out_node, "?")} → '
                f'{names.get(link.in_node, "?")}…'), None)
            item.set_action_and_target_value(
                'pb.connect-dialog',
                GLib.Variant('s', f'{link.out_node}>{link.in_node}'))
            sec.append_item(item)
            menu.append_section(None, sec)
        else:
            sec = Gio.Menu()
            sec.append('Arrange by signal flow', 'pb.arrange')
            sec.append('Fit the whole graph', 'pb.fit')
            item = Gio.MenuItem.new('Connect channels…', None)
            item.set_action_and_target_value('pb.connect-dialog',
                                             GLib.Variant('s', '>'))
            sec.append_item(item)
            menu.append_section(None, sec)
        return self.area.popup_at(menu, x, y)

    # -------------------------------------------------------------- patching
    def patch(self, out_port, in_port):
        if out_port.media != in_port.media:
            self.window.toast(
                f'{MEDIA_NAME[out_port.media]} and '
                f'{MEDIA_NAME[in_port.media]} ports can’t be connected')
            return
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

    def plan_drop(self, src, node):
        """The cables a port dropped on a node's title makes: every channel
        of the port's side of its node, matched to the other node."""
        own = self.area.g.nodes.get(src.node_id)
        if own is None or own.id == node.id:
            return []
        if src.direction == 'out':
            return graph.plan_pairs(
                graph.side_ports(own, 'out', src.media, src.is_monitor),
                graph.side_ports(node, 'in', src.media))
        return graph.plan_pairs(graph.side_ports(node, 'out', src.media),
                                graph.side_ports(own, 'in', src.media))

    def _connect_all(self, pair: str):
        a, _, b = pair.partition('>')
        o = self.area.g.nodes.get(int(a))
        i = self.area.g.nodes.get(int(b))
        if o is None or i is None:
            self.window.toast('That node is gone')
            return
        self.patch_pairs(graph.plan_nodes(o, i))

    def patch_pairs(self, pairs, remove=False):
        """Make (or with `remove`, take away) many cables in one go.

        Pairs already in the state asked for are skipped; one pw-link per
        cable, off the UI thread."""
        g = self.area.g
        if remove:
            todo = [l for l in (g.find_link(o.id, i.id) for o, i in pairs)
                    if l is not None]
        else:
            todo = [(o, i) for o, i in pairs if o.media == i.media
                    and g.find_link(o.id, i.id) is None]
        if not todo:
            self.window.toast('Nothing to remove' if remove
                              else 'Already connected')
            return
        self.window.mark_user_patch()

        def work():
            done_n, errors = 0, []
            for item in todo:
                ok, err = (graph.disconnect(item.id) if remove
                           else graph.connect(item[0].id, item[1].id))
                done_n += ok
                if not ok and err:
                    errors.append(err)
            return done_n, errors

        def finished(res, e):
            made, errors = res if res else (0, [str(e)])
            verb = 'Removed' if remove else 'Connected'
            noun = 'cable' if made == 1 else 'cables'
            failed = len(todo) - made
            msg = f'{verb} {made} {noun}'
            if failed:
                msg += f', {failed} failed' + (f': {errors[0]}'
                                               if errors else '')
            self.window.toast(msg)
            self.refresh()
        async_call(work, finished)

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

    def _disconnect_link_id(self, lid):
        link = next((l for l in self.area.g.links if l.id == lid), None)
        if link is not None:
            self.area.selected_link = link
            self.disconnect_selected()

    def disconnect_node(self, node):
        links = [l for l in self.area.g.links
                 if node.id in (l.out_node, l.in_node)]
        if not links:
            return
        n = len(links)

        def go():
            self.window.mark_user_patch()

            def work():
                return sum(graph.disconnect(l.id)[0] for l in links)

            def done(res, e):
                self.window.toast(f'Removed {res or 0} of {n} cables')
                self.refresh()
            async_call(work, done)
        confirm(self.window, f'Remove all cables of “{node.label}”?',
                f'{n} cable{"s" if n > 1 else ""} to and from this node '
                'will be removed. Apps that lose their output usually go '
                'back to the default device.', 'Remove', go)

    def connect_dialog(self, out_node=None, in_node=None):
        from .patch_dialog import ConnectDialog
        dlg = ConnectDialog(self, out_node, in_node)
        dlg.present(self.window)
        return dlg

    def _dialog_from_target(self, spec: str):
        a, _, b = (spec or '>').partition('>')
        g = self.area.g.nodes
        self.connect_dialog(g.get(int(a)) if a else None,
                            g.get(int(b)) if b else None)

    # ----------------------------------------------------------- the hint --
    def drag_hint(self):
        """What releasing the cable here would do, said in the toolbar."""
        a = self.area
        if a.drop and a.drop[0] == 'port':
            src, p = a.drag_port, a.drop[1]
            o, i = (src, p) if src.direction == 'out' else (p, src)
            verb = 'Release to remove' if a.will_remove() else 'Release:'
            self.hint.set_label(f'{verb} {o.name} → {i.name}')
        elif a.drop and a.drop[0] == 'node':
            pairs = self.plan_drop(a.drag_port, a.drop[1])
            n = sum(a.g.find_link(o.id, i.id) is None for o, i in pairs)
            to = a.drop[1].label
            self.hint.set_label(
                f'Release to connect {n} more channel{"s" if n != 1 else ""}'
                f' with {to}' if n and n < len(pairs) else
                f'Release to connect {n} channel{"s" if n != 1 else ""} '
                f'with {to}' if n else f'Every channel is already connected '
                f'with {to}')
        elif a.refusal:
            self.hint.set_label(a.refusal)
        else:
            self.hint.set_label('Drop on a port, or on a node’s title to '
                                'connect every channel')

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
                cables = sum(1 for l in self.area.g.links
                             if n.id in (l.out_node, l.in_node))
                self.hint.set_label(
                    f'{n.label} · {n.media_class or n.kind} · {n.state}'
                    + (f' · latency {lat:.1f} ms' if lat else '')
                    + (f' · {cables} cable{"s" if cables != 1 else ""}'
                       if cables else ''))
        else:
            self.hint.set_label(HINT)

    # ----------------------------------------------------------- search ----
    def open_search(self):
        self.search_bar.set_search_mode(True)
        self.search.grab_focus()

    def close_search(self) -> bool:
        if not self.search_bar.get_search_mode():
            return False
        self.search_bar.set_search_mode(False)
        return True

    def _search_mode(self, bar, _p):
        if not bar.get_search_mode():
            self.search.set_text('')
            self.area.set_search('')
            self.found_label.set_label('')
            self.area.grab_focus()

    def _search_changed(self, entry, keep=False):
        self._found = self.area.set_search(entry.get_text())
        text = entry.get_text().strip()
        if not text:
            self.found_label.set_label('')
            return
        if not self._found:
            self.found_label.set_label('No match')
            return
        if keep:
            self._found_at = min(max(self._found_at, 0), len(self._found) - 1)
        else:
            self._found_at = 0
            self.area.center_on(self._found[0])
        self.found_label.set_label(f'{self._found_at + 1} of '
                                   f'{len(self._found)}')

    def _search_step(self, step):
        if not self._found:
            return
        self._found_at = (self._found_at + step) % len(self._found)
        node = self._found[self._found_at]
        self.area.center_on(node)
        self.area.select_node(node.id)
        self.found_label.set_label(f'{self._found_at + 1} of '
                                   f'{len(self._found)}')

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

            def write(existing=None):
                # Replacing goes through update() so it rewrites the file the
                # snapshot came from; save() would re-derive the path from the
                # name and could leave a duplicate behind (see routing.update).
                shown = existing.get('name', name) if existing else name
                async_call(
                    lambda: routing.update(existing) if existing
                    else routing.save(name),
                    lambda r, e: self.window.toast(
                        f'Snapshot “{shown}” saved' if not e
                        else f'Save failed: {e}'))

            clash = routing.find_by_name(name)
            if clash is None:
                write()
                return
            confirm(self.window, f'Replace “{clash.get("name", name)}”?',
                    f'A snapshot with that name already exists, holding '
                    f'{len(clash.get("links", []))} links. It is replaced by '
                    'the patchbay as it is right now. This cannot be undone.',
                    'Replace', lambda: write(clash))
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

        def do_update(*_a):
            name = snap.get('name', '?')
            # Down first: presenting the dialog takes focus, which autohides
            # the popover anyway — doing it explicitly keeps the order fixed
            # whether the user confirms or cancels.
            self._snap_popover.popdown()

            def go():
                async_call(lambda: routing.update(snap),
                           lambda r, e: self.window.toast(
                               f'Snapshot “{name}” updated' if not e
                               else f'Update failed: {e}'))
            confirm(self.window, f'Update “{name}”?',
                    f'The {len(snap.get("links", []))} links stored in this '
                    'snapshot are replaced by the patchbay as it is right '
                    'now. This cannot be undone.',
                    'Update', go)
        row.append(icon_button('document-save-symbolic',
                               'Update to match the current patchbay',
                               do_update))

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
        # A hidden object never comes back to the graph, so this dialog can't
        # be the way back — point at the page that lists them instead.
        manage_desc = ('Stored as WirePlumber rules; applied after a '
                       'WirePlumber restart. Hidden devices are listed — and '
                       'can be brought back — on the Devices page.') if is_hw \
            else 'Only available for hardware (ALSA/Bluetooth) nodes.'
        manage = Adw.PreferencesGroup(title='Persistent rules',
                                      description=manage_desc)
        rule = rules.node_rule(node.name) if is_hw else {}
        self.rename_row = Adw.EntryRow(title='Rename (description)')
        self.rename_row.set_text(rule.get('rename', ''))
        self.rename_row.set_sensitive(is_hw)
        manage.add(self.rename_row)
        self.hide_row = Adw.SwitchRow(
            title='Hide this output' if node.kind == 'sink' else
                  'Hide this input' if node.kind == 'source' else
                  'Hide this node',
            subtitle='Disables this endpoint — it disappears from every app. '
                     'The sound card itself stays listed in your desktop’s '
                     'sound settings.')
        self.hide_row.set_active(bool(rule.get('hide')))
        self.hide_row.set_sensitive(is_hw)
        manage.add(self.hide_row)
        # Filled in once the card behind this node is resolved (its name lives
        # on the Device object, not in the node's props).
        self.card = None
        self.card_row = Adw.SwitchRow(title='Hide the whole sound card',
                                      visible=False)
        manage.add(self.card_row)
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
        if is_hw:
            self._load_card()

    def _load_card(self):
        """Resolve this node's sound card, then offer the card-level hide."""
        try:
            dev_id = int(self.node.props.get('device.id'))
        except (TypeError, ValueError):
            return

        def work():
            card = pw.card_map().get(dev_id)
            if not card:
                return None
            return card, bool(rules.device_rule(card[0]).get('hide'))

        def done(res, e):
            if e or not res:
                return
            card, hidden = res
            self.card = card
            self.card_row.set_subtitle(
                f'Disables {esc(card[1])} and every input and output it '
                'provides, everywhere.')
            self.card_row.set_active(hidden)
            self.card_row.set_visible(True)
        async_call(work, done)

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
        label = self.node.label
        card = self.card
        card_hide = self.card_row.get_active() if card else None

        def work():
            rules.set_node_rule(name, rename=rename, hide=hide, desc=label)
            if card is not None:
                rules.set_device_rule(card[0], hide=card_hide, desc=card[1])
            return True
        async_call(work, lambda r, e: (
            self.window.toast('Rules saved' if not e else f'Failed: {e}'),
            self.window.flag_restart('wireplumber')))
