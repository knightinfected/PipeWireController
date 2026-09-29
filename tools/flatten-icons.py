#!/usr/bin/env python3
"""Flatten GNOME icon-development-kit SVGs into plain filled symbolic icons.

The kit's icons are stroke-based ("grappa"), which needs GTK's 4.20 symbolic
renderer.  On anything older GTK recolours by injecting

    rect,circle,path { fill: <fg> }

and a CSS rule outranks the ``fill="none"`` presentation attribute, so an
outline path gets filled: the icon turns into a solid blob and the stroke keeps
its baked-in black, invisible on a dark theme.  Debian 13 ships GTK 4.18, which
is inside our supported range, so the kit's SVGs cannot be shipped verbatim.

This converts the stroke into a filled path once, offline, so the result is an
ordinary symbolic icon -- a single ``<path fill="#2e3436">``, byte-for-byte the
shape adwaita-icon-theme itself still ships, working on every GTK version.

Dev-only.  Its output is committed, so this never runs at build or run time.
Needs ``skia-pathops`` and ``svgelements``::

    uv venv .venv && uv pip install --python .venv/bin/python skia-pathops svgelements
    .venv/bin/python tools/flatten-icons.py <kit-clone>/icons pwctl/icons/hicolor/scalable/actions

With no explicit name list it reads ``tools/icon-map.txt``: one
``kit-name  pwctl-slot`` pair per line, ``#`` comments ignored.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path as FsPath

from pathops import LineCap, LineJoin, Path as SkPath, union
from svgelements import Arc, Close, CubicBezier, Line, Move, Path as SvgPath, QuadraticBezier

SVG_NS = 'http://www.w3.org/2000/svg'
FILL = '#2e3436'          # what adwaita-icon-theme ships
# Skia renders round caps and joins as conic sections, which no SVG path can
# express -- they have to become quadratics.  0.25 is the library default and
# is visibly coarse on a 16px canvas; this is tight enough to be exact there.
CONIC_TOLERANCE = 0.005
CAPS = {'butt': LineCap.BUTT_CAP, 'round': LineCap.ROUND_CAP, 'square': LineCap.SQUARE_CAP}
JOINS = {'miter': LineJoin.MITER_JOIN, 'round': LineJoin.ROUND_JOIN, 'bevel': LineJoin.BEVEL_JOIN}


def _paint(value: str | None) -> bool:
    """Is this a real paint, rather than 'none'?

    The kit writes ``stroke='url("#gpa:foreground") rgb(0,0,0)'`` -- a paint
    server with a plain fallback.  Anything that is not literally 'none' paints.
    """
    return bool(value) and value.strip().lower() != 'none'


def _draw(d: str, pen) -> None:
    """Feed one path's 'd' into a pathops pen, arcs approximated as cubics."""
    open_contour = False
    for seg in SvgPath(d).segments():
        if isinstance(seg, Move):
            if open_contour:
                pen.endPath()
            pen.moveTo((seg.end.x, seg.end.y))
            open_contour = True
        elif isinstance(seg, Line):
            pen.lineTo((seg.end.x, seg.end.y))
        elif isinstance(seg, CubicBezier):
            pen.curveTo((seg.control1.x, seg.control1.y),
                        (seg.control2.x, seg.control2.y),
                        (seg.end.x, seg.end.y))
        elif isinstance(seg, QuadraticBezier):
            pen.qCurveTo((seg.control.x, seg.control.y), (seg.end.x, seg.end.y))
        elif isinstance(seg, Arc):
            for cub in seg.as_cubic_curves():
                pen.curveTo((cub.control1.x, cub.control1.y),
                            (cub.control2.x, cub.control2.y),
                            (cub.end.x, cub.end.y))
        elif isinstance(seg, Close):
            pen.closePath()
            open_contour = False
    if open_contour:
        pen.endPath()


def flatten(svg_text: str) -> str:
    """Return the 'd' of one filled path covering everything the icon draws."""
    root = ET.fromstring(svg_text)
    pieces: list[SkPath] = []

    for el in root.iter(f'{{{SVG_NS}}}path'):
        if el.get('visibility') == 'hidden':
            continue                      # the kit's hidden 'filled' variant
        d = el.get('d')
        if not d:
            continue

        if _paint(el.get('stroke')):
            outline = SkPath()
            _draw(d, outline.getPen())
            outline.stroke(
                float(el.get('stroke-width', 1)),
                CAPS.get(el.get('stroke-linecap', 'butt'), LineCap.BUTT_CAP),
                JOINS.get(el.get('stroke-linejoin', 'miter'), LineJoin.MITER_JOIN),
                float(el.get('stroke-miterlimit', 4)),
            )
            outline.convertConicsToQuads(CONIC_TOLERANCE)
            pieces.append(outline)

        if _paint(el.get('fill')):
            filled = SkPath()
            _draw(d, filled.getPen())
            filled.simplify()             # closes the contour and resolves self-overlap
            pieces.append(filled)

    if not pieces:
        raise ValueError('nothing drawable in this icon')

    merged = SkPath()
    union(pieces, merged.getPen())
    merged.convertConicsToQuads(CONIC_TOLERANCE)

    out: list[str] = []
    for verb, pts in merged:
        name = verb.name if hasattr(verb, 'name') else str(verb)
        if name == 'MOVE':
            out.append('M%s' % _pt(pts[0]))
        elif name == 'LINE':
            out.append('L%s' % _pt(pts[0]))
        elif name == 'CUBIC':
            out.append('C%s %s %s' % (_pt(pts[0]), _pt(pts[1]), _pt(pts[2])))
        elif name == 'QUAD':
            out.append('Q%s %s' % (_pt(pts[0]), _pt(pts[1])))
        elif name == 'CLOSE':
            out.append('Z')
    return ' '.join(out)


def _pt(p) -> str:
    return '%s,%s' % (_num(p[0]), _num(p[1]))


def _num(v: float) -> str:
    return re.sub(r'\.?0+$', '', f'{v:.3f}') if '.' in f'{v:.3f}' else f'{v:.3f}'


def convert(src: FsPath, dest: FsPath) -> None:
    text = src.read_text()
    size = re.search(r'\bwidth="(\d+)"', text)
    box = int(size.group(1)) if size else 16
    d = flatten(text)
    dest.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d">\n  <path fill="%s" d="%s"/>\n</svg>\n'
        % (box, box, box, box, FILL, d)
    )


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    kit, out = FsPath(argv[1]), FsPath(argv[2])
    out.mkdir(parents=True, exist_ok=True)

    pairs: list[tuple[str, str]] = []
    if len(argv) > 3:
        pairs = [(n, n) for n in argv[3:]]
    else:
        mapping = FsPath(__file__).parent / 'icon-map.txt'
        for line in mapping.read_text().splitlines():
            line = line.split('#')[0].strip()
            if line:
                kit_name, _, slot = line.partition(' ')
                pairs.append((kit_name.strip(), (slot.strip() or kit_name.strip())))

    failed = 0
    for kit_name, slot in pairs:
        src = kit / f'{kit_name}.svg'
        if not src.exists():
            print(f'  MISSING in kit: {kit_name}')
            failed += 1
            continue
        target = out / f'{slot}-symbolic.svg'
        try:
            convert(src, target)
        except Exception as exc:                              # noqa: BLE001
            print(f'  FAILED {kit_name}: {exc}')
            failed += 1
            continue
        print(f'  {kit_name:22} -> {target.name:32} {target.stat().st_size:>6} B')

    print(f'\n{len(pairs) - failed} written, {failed} failed, into {out}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
