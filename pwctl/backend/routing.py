"""Routing snapshots: save, recall, export and import the patch state.

A snapshot records every link as (node.name, port.name) pairs plus the
default sink/source and device volumes.  Applying resolves names against the
live graph, creates the missing links, optionally removes extra ones between
non-stream nodes, and restores defaults and volumes.  Name-based storage
keeps snapshots valid across reboots and between machines (export/import).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from . import graph, pw
from .config import XDG_CONFIG
from .system import atomic_write

ROUTING_DIR = XDG_CONFIG / 'pipewire-controller' / 'routing'
FORMAT = 'pwctl-routing-1'


def _slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'snapshot'


def ensure_dirs():
    ROUTING_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ApplyReport:
    created: int = 0
    removed: int = 0
    failed: int = 0
    defaults: int = 0
    volumes: int = 0

    def summary(self) -> str:
        parts = []
        if self.created:
            parts.append(f'{self.created} link(s) created')
        if self.removed:
            parts.append(f'{self.removed} removed')
        if self.defaults:
            parts.append('defaults restored')
        if self.volumes:
            parts.append(f'{self.volumes} volume(s) set')
        if self.failed:
            parts.append(f'{self.failed} failed')
        return ', '.join(parts) or 'nothing to do'


def capture(name: str, dump=None) -> dict:
    """Snapshot the current routing state as a serializable dict."""
    dump = dump if dump is not None else pw.pw_dump()
    g = graph.snapshot(dump)
    ports = {p.id: p for n in g.nodes.values() for p in n.inputs + n.outputs}
    links = []
    for link in g.links:
        op, ip = ports.get(link.out_port), ports.get(link.in_port)
        if not op or not ip:
            continue
        links.append({
            'out_node': g.nodes[link.out_node].name,
            'out_port': op.name,
            'in_node': g.nodes[link.in_node].name,
            'in_port': ip.name,
            'stream': g.nodes[link.out_node].kind.startswith('stream')
            or g.nodes[link.in_node].kind.startswith('stream'),
        })
    defaults = pw.read_default_names()
    volumes = {}
    for node in pw.list_audio_nodes(dump):
        if node.volume is not None:
            volumes[node.name] = {'volume': round(node.volume, 3),
                                  'muted': node.muted}
    return {
        'format': FORMAT,
        'name': name,
        'saved': time.strftime('%Y-%m-%d %H:%M'),
        'links': links,
        'defaults': {
            'sink': defaults.get('default.audio.sink'),
            'source': defaults.get('default.audio.source'),
        },
        'volumes': volumes,
    }


def save(name: str) -> dict:
    ensure_dirs()
    snap = capture(name)
    atomic_write(ROUTING_DIR / f'{_slug(name)}.json',
                 json.dumps(snap, indent=2) + '\n')
    return snap


def list_snapshots() -> list[dict]:
    ensure_dirs()
    out = []
    for f in sorted(ROUTING_DIR.glob('*.json')):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if data.get('format') == FORMAT:
            data['_path'] = str(f)
            out.append(data)
    return out


def delete(snap: dict):
    from pathlib import Path
    p = Path(snap.get('_path', ''))
    if p.is_file():
        p.unlink()


def export(snap: dict, dest_path):
    data = {k: v for k, v in snap.items() if not k.startswith('_')}
    atomic_write(dest_path, json.dumps(data, indent=2) + '\n')


def import_file(path) -> dict:
    from pathlib import Path
    data = json.loads(Path(path).read_text())
    if data.get('format') != FORMAT:
        raise ValueError('not a PipeWire Controller routing snapshot')
    ensure_dirs()
    name = data.get('name') or Path(path).stem
    dest = ROUTING_DIR / f'{_slug(name)}.json'
    if dest.is_file():
        name = f'{name} (imported)'
        dest = ROUTING_DIR / f'{_slug(name)}.json'
        data['name'] = name
    atomic_write(dest, json.dumps(data, indent=2) + '\n')
    data['_path'] = str(dest)
    return data


def apply(snap: dict, remove_extra=False, restore_volumes=True) -> ApplyReport:
    """Recreate a snapshot's routing on the live graph.

    remove_extra also disconnects links between non-stream nodes that are
    not part of the snapshot — application streams are never touched.
    """
    rep = ApplyReport()
    g = graph.snapshot()
    by_name = {n.name: n for n in g.nodes.values()}
    ports = {p.id: p for n in g.nodes.values() for p in n.inputs + n.outputs}

    def port_id(node_name, port_name, direction):
        node = by_name.get(node_name)
        if not node:
            return None
        plist = node.outputs if direction == 'out' else node.inputs
        return next((p.id for p in plist if p.name == port_name), None)

    wanted = set()
    for l in snap.get('links', []):
        op = port_id(l['out_node'], l['out_port'], 'out')
        ip = port_id(l['in_node'], l['in_port'], 'in')
        if op is None or ip is None:
            if not l.get('stream'):      # missing hardware/virtual endpoint
                rep.failed += 1
            continue
        wanted.add((op, ip))
        if g.find_link(op, ip):
            continue
        ok, _ = graph.connect(op, ip)
        rep.created += ok
        rep.failed += not ok

    if remove_extra:
        for link in g.links:
            if (link.out_port, link.in_port) in wanted:
                continue
            out_kind = g.nodes[link.out_node].kind
            in_kind = g.nodes[link.in_node].kind
            if out_kind.startswith('stream') or in_kind.startswith('stream'):
                continue
            ok, _ = graph.disconnect(link.id)
            rep.removed += ok

    defaults = snap.get('defaults') or {}
    for key, want in (('sink', defaults.get('sink')),
                      ('source', defaults.get('source'))):
        if not want or want not in by_name:
            continue
        if pw.set_default(by_name[want].id):
            rep.defaults += 1

    if restore_volumes:
        for name, vol in (snap.get('volumes') or {}).items():
            node = by_name.get(name)
            if not node:
                continue
            if pw.set_volume(node.id, float(vol.get('volume', 1.0))):
                rep.volumes += 1
            pw.set_mute(node.id, bool(vol.get('muted')))
    return rep
