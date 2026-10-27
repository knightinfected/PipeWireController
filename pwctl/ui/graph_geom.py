"""Patchbay geometry: cable shapes and the signal-flow layout.

No GTK here, so a harness can check the shapes and the layout directly.
A cable is a list of cubic Bézier segments, each (p0, p1, p2, p3).
"""

from __future__ import annotations

import math

# How far a cable that has to go round stands off the boxes, and how tight
# its corners are.
GAP = 24.0
CORNER = 12.0
_KAPPA = 0.5523          # cubic handle length for a quarter circle


def forward_path(a, b):
    """The plain S-curve every cable used to be: out to the right, in from
    the left.  Kept point for point, so ordinary cables look as they did."""
    dx = max(50.0, abs(b[0] - a[0]) * 0.45)
    return [(a, (a[0] + dx, a[1]), (b[0] - dx, b[1]), b)]


def needs_detour(a, b) -> bool:
    """True when the input sits left of the output, so an S-curve would run
    back through the boxes: a node into itself, as every MIDI
    device-to-device cable through the Midi-Bridge is (issue #31), or into a
    node drawn to its left.  Two boxes merely side by side keep the short
    ordinary curve: there is no room to go round them."""
    return b[0] < a[0]


def _corridor(a, b, obox, ibox):
    """The height a cable going round runs along: in the gap between the two
    boxes if there is one, else just below or above both, whichever is the
    shorter way."""
    ox0, oy0, ox1, oy1 = obox
    ix0, iy0, ix1, iy1 = ibox
    if oy1 + 2 * GAP <= iy0:
        return (oy1 + iy0) / 2
    if iy1 + 2 * GAP <= oy0:
        return (iy1 + oy0) / 2
    below = max(oy1, iy1) + GAP
    above = min(oy0, iy0) - GAP
    cost_below = abs(a[1] - below) + abs(b[1] - below)
    cost_above = abs(a[1] - above) + abs(b[1] - above)
    return below if cost_below <= cost_above else above


def _crosses(box, x, y0, y1) -> bool:
    """Does a vertical run at x from y0 to y1 pass through the box?"""
    lo, hi = min(y0, y1), max(y0, y1)
    return box[0] < x < box[2] and lo < box[3] and box[1] < hi


def _line(p, q):
    return (p, (p[0] + (q[0] - p[0]) / 3, p[1] + (q[1] - p[1]) / 3),
            (p[0] + 2 * (q[0] - p[0]) / 3, p[1] + 2 * (q[1] - p[1]) / 3), q)


def rounded(points, radius=CORNER):
    """A polyline with rounded corners, as cubic segments."""
    pts = []
    for p in points:                         # drop repeats and straight joins
        if pts and math.isclose(p[0], pts[-1][0]) \
                and math.isclose(p[1], pts[-1][1]):
            continue
        pts.append(p)
        if len(pts) >= 3:
            (x0, y0), (x1, y1), (x2, y2) = pts[-3:]
            if math.isclose((x1 - x0) * (y2 - y1), (y1 - y0) * (x2 - x1),
                            abs_tol=1e-6):
                del pts[-2]
    if len(pts) < 2:
        return []
    segs = []
    cur = pts[0]
    for i in range(1, len(pts) - 1):
        p0, p1, p2 = pts[i - 1], pts[i], pts[i + 1]
        l_in = math.dist(p0, p1)
        l_out = math.dist(p1, p2)
        r = min(radius, l_in / 2, l_out / 2)
        u_in = ((p1[0] - p0[0]) / l_in, (p1[1] - p0[1]) / l_in)
        u_out = ((p2[0] - p1[0]) / l_out, (p2[1] - p1[1]) / l_out)
        cs = (p1[0] - u_in[0] * r, p1[1] - u_in[1] * r)
        ce = (p1[0] + u_out[0] * r, p1[1] + u_out[1] * r)
        if math.dist(cur, cs) > 1e-6:
            segs.append(_line(cur, cs))
        k = _KAPPA * r
        segs.append((cs, (cs[0] + u_in[0] * k, cs[1] + u_in[1] * k),
                     (ce[0] - u_out[0] * k, ce[1] - u_out[1] * k), ce))
        cur = ce
    if math.dist(cur, pts[-1]) > 1e-6:
        segs.append(_line(cur, pts[-1]))
    return segs


def detour_path(a, b, obox, ibox):
    """Out to the right of the output's box, round the boxes, and in from the
    left of the input's box, never crossing either.  Boxes are
    (x0, y0, x1, y1).  For a node into itself the two boxes are the same."""
    yc = _corridor(a, b, obox, ibox)
    right = max(a[0], obox[2]) + GAP
    if _crosses(ibox, right, a[1], yc):       # the input's box is in the way
        right = ibox[2] + GAP
    left = min(b[0], ibox[0]) - GAP
    if _crosses(obox, left, yc, b[1]):        # the output's box is in the way
        left = obox[0] - GAP
    return rounded([a, (right, a[1]), (right, yc), (left, yc),
                    (left, b[1]), b])


def cable_path(a, b, obox=None, ibox=None):
    if obox is not None and ibox is not None and needs_detour(a, b):
        return detour_path(a, b, obox, ibox)
    return forward_path(a, b)


def bezier(seg, t):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = seg
    mt = 1 - t
    x = mt**3 * x0 + 3 * mt**2 * t * x1 + 3 * mt * t**2 * x2 + t**3 * x3
    y = mt**3 * y0 + 3 * mt**2 * t * y1 + 3 * mt * t**2 * y2 + t**3 * y3
    return x, y


def samples(path, per_seg=12):
    """Points along a cable, for hit-testing and for the harness."""
    out = []
    for seg in path:
        for i in range(per_seg + 1):
            out.append(bezier(seg, i / per_seg))
    return out


def distance(path, x, y, per_seg=12) -> float:
    return min((math.hypot(px - x, py - y)
                for px, py in samples(path, per_seg)), default=math.inf)


# ------------------------------------------------------------- the layout --

def flow_columns(nodes, edges, kind_of, groups=()):
    """Columns that follow the signal, left to right.

    `nodes`: ids.  `edges`: (from, to) pairs, from the links.  `groups`:
    extra (from, to) pairs for the two halves of one object that no link
    joins, such as a filter chain's input and output (the same
    `node.link-group`).  `kind_of(id)` gives the graph kind.

    Longest-path layering, with every cycle broken where a depth-first walk
    meets it, so a loop can't push a node off to the right forever.  A node
    that only listens (a device, a recorder) goes in the last column, and a
    node nothing touches goes by its kind: producers first, devices last.
    """
    ids = list(nodes)
    idset = set(ids)
    succ = {n: [] for n in ids}
    pred = {n: [] for n in ids}
    for u, v in list(edges) + list(groups):
        if u in idset and v in idset and u != v and v not in succ[u]:
            succ[u].append(v)
            pred[v].append(u)

    # Break cycles: drop every edge a depth-first walk finds going back.
    state = {}
    back = set()
    for root in sorted(ids, key=lambda n: (len(pred[n]) > 0, n)):
        if root in state:
            continue
        stack = [(root, iter(succ[root]))]
        state[root] = 1
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
                continue
            if state.get(nxt) == 1:
                back.add((node, nxt))
            elif nxt not in state:
                state[nxt] = 1
                stack.append((nxt, iter(succ[nxt])))

    dag_pred = {n: [u for u in pred[n] if (u, n) not in back] for n in ids}
    dag_succ = {n: [v for v in succ[n] if (n, v) not in back] for n in ids}
    layer = {}

    def depth(n, seen=()):
        if n in layer:
            return layer[n]
        d = 0
        for u in dag_pred[n]:
            if u not in seen:
                d = max(d, depth(u, seen + (n,)) + 1)
        layer[n] = d
        return d

    for n in ids:
        depth(n)

    touched = {n for n in ids if pred[n] or succ[n]}
    last = max((layer[n] for n in touched), default=0)
    for n in ids:
        kind = kind_of(n)
        if n not in touched:
            if kind in ('source', 'stream-out', 'midi', 'video'):
                layer[n] = 0
            elif kind in ('sink', 'stream-in'):
                layer[n] = max(last, 2)
            else:
                layer[n] = 1
        elif not dag_succ[n] and kind in ('sink', 'stream-in'):
            layer[n] = max(layer[n], last)
    # No empty column in the middle: renumber the columns in use.
    used = {c: i for i, c in enumerate(sorted(set(layer.values())))}
    return {n: used[c] for n, c in layer.items()}


def order_columns(layer, edges, label_of):
    """Order each column so cables cross as little as a simple pass can
    manage: each node sits near the average height of what feeds it, and
    anything unconnected goes below, by name."""
    cols = {}
    for n, c in layer.items():
        cols.setdefault(c, []).append(n)
    pred = {}
    for u, v in edges:
        if u != v:
            pred.setdefault(v, []).append(u)
    rank = {}
    order = {}
    for c in sorted(cols):
        def key(n):
            ups = [rank[u] for u in pred.get(n, ()) if u in rank
                   and layer.get(u, c) < c]
            if ups:
                return (0, sum(ups) / len(ups), label_of(n).casefold())
            return (1, 0, label_of(n).casefold())
        order[c] = sorted(cols[c], key=key)
        for i, n in enumerate(order[c]):
            rank[n] = i
    return order
