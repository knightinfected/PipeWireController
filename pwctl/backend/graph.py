"""Full PipeWire graph model: every node, port and link (audio and MIDI).

Built from a single pw-dump pass.  Patching goes through pw-link (connect by
port id, disconnect by link id) and metadata edits through pw-metadata, so
the module stays daemon-version agnostic — no bindings required.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .pw import pw_dump
from .system import run

# node kind -> patchbay column (0 producers, 1 processing, 2 consumers)
KIND_COLUMN = {
    'source': 0, 'stream-out': 0, 'midi': 0,
    'filter': 1, 'duplex': 1, 'other': 1,
    'sink': 2, 'stream-in': 2,
}


@dataclass
class Port:
    id: int
    node_id: int
    name: str
    direction: str            # 'in' | 'out'
    is_midi: bool = False
    is_monitor: bool = False
    channel: str = ''


@dataclass
class GNode:
    id: int
    name: str
    label: str
    media_class: str
    kind: str                  # source | sink | stream-out | stream-in |
    #                            filter | duplex | midi | other
    state: str = 'suspended'
    props: dict = field(default_factory=dict)
    inputs: list[Port] = field(default_factory=list)
    outputs: list[Port] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.state == 'running'

    @property
    def latency(self) -> str:
        """Best-effort declared latency, e.g. '256/48000'."""
        for key in ('node.latency', 'node.max-latency'):
            v = self.props.get(key)
            if v:
                return str(v)
        return ''

    def latency_ms(self) -> float | None:
        lat = self.latency
        if '/' in lat:
            try:
                num, den = lat.split('/', 1)
                den = float(den)
                return float(num) / den * 1000 if den else None
            except ValueError:
                return None
        return None


@dataclass
class GLink:
    id: int
    out_node: int
    out_port: int
    in_node: int
    in_port: int
    state: str = ''


@dataclass
class Graph:
    nodes: dict[int, GNode] = field(default_factory=dict)
    links: list[GLink] = field(default_factory=list)

    def port(self, port_id: int) -> Port | None:
        for n in self.nodes.values():
            for p in n.inputs + n.outputs:
                if p.id == port_id:
                    return p
        return None

    def find_link(self, out_port: int, in_port: int) -> GLink | None:
        return next((l for l in self.links
                     if l.out_port == out_port and l.in_port == in_port), None)


def _classify(mclass: str, props: dict, has_in: bool, has_out: bool) -> str:
    if mclass == 'Audio/Sink':
        return 'sink'
    if mclass == 'Audio/Source':
        return 'source'
    if mclass == 'Stream/Output/Audio':
        return 'stream-out'
    if mclass == 'Stream/Input/Audio':
        return 'stream-in'
    if mclass in ('Audio/Duplex', 'Audio/Source/Virtual'):
        return 'duplex'
    if 'Midi' in mclass or mclass.startswith('Midi'):
        return 'midi'
    if has_in and has_out:
        return 'filter'
    if has_out:
        return 'source'
    if has_in:
        return 'sink'
    return 'other'


def snapshot(dump=None, include_hidden=False) -> Graph:
    """Build the complete graph. Hidden = nodes without any ports."""
    dump = dump if dump is not None else pw_dump()
    g = Graph()
    ports_by_node: dict[int, list[Port]] = {}

    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Port':
            continue
        info = obj.get('info') or {}
        props = info.get('props') or {}
        try:
            node_id = int(props['node.id'])
        except (KeyError, TypeError, ValueError):
            continue
        fmt = str(props.get('format.dsp', ''))
        port = Port(
            id=obj['id'], node_id=node_id,
            name=props.get('port.name', str(obj['id'])),
            direction='in' if info.get('direction') == 'input' else 'out',
            is_midi='midi' in fmt or 'Midi' in str(props.get('port.alias', '')),
            is_monitor=bool(props.get('port.monitor')),
            channel=str(props.get('audio.channel', '')))
        ports_by_node.setdefault(node_id, []).append(port)

    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Node':
            continue
        info = obj.get('info') or {}
        props = info.get('props') or {}
        name = props.get('node.name', str(obj['id']))
        plist = ports_by_node.get(obj['id'], [])
        if not plist and not include_hidden:
            continue
        ins = sorted((p for p in plist if p.direction == 'in'),
                     key=lambda p: p.name)
        outs = sorted((p for p in plist if p.direction == 'out'),
                      key=lambda p: p.name)
        mclass = props.get('media.class', '')
        node = GNode(
            id=obj['id'], name=name,
            label=(props.get('node.description') or props.get('node.nick')
                   or props.get('application.name') or name),
            media_class=mclass,
            kind=_classify(mclass, props, bool(ins), bool(outs)),
            state=info.get('state', 'suspended'),
            props=props, inputs=ins, outputs=outs)
        g.nodes[node.id] = node

    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Link':
            continue
        info = obj.get('info') or {}
        link = GLink(id=obj['id'],
                     out_node=info.get('output-node-id', -1),
                     out_port=info.get('output-port-id', -1),
                     in_node=info.get('input-node-id', -1),
                     in_port=info.get('input-port-id', -1),
                     state=info.get('state', ''))
        if link.out_node in g.nodes and link.in_node in g.nodes:
            g.links.append(link)
    return g


# ---------------------------------------------------------------- patching --

def connect(out_port_id: int, in_port_id: int) -> tuple[bool, str]:
    rc, _, err = run(['pw-link', str(out_port_id), str(in_port_id)])
    return rc == 0, err.strip()


def disconnect(link_id: int) -> tuple[bool, str]:
    rc, _, err = run(['pw-link', '-d', str(link_id)])
    return rc == 0, err.strip()


def connect_nodes(out_node: GNode, in_node: GNode) -> int:
    """Best-effort node-to-node patch: match ports by channel, else by order.
    Returns the number of links created."""
    outs = [p for p in out_node.outputs if not p.is_midi]
    ins = [p for p in in_node.inputs if not p.is_midi]
    if not outs or not ins:            # fall back to MIDI if that's all there is
        outs = out_node.outputs
        ins = in_node.inputs
    made = 0
    used = set()
    for o in outs:
        target = None
        if o.channel:
            target = next((p for p in ins
                           if p.channel == o.channel and p.id not in used), None)
        if target is None:
            target = next((p for p in ins if p.id not in used), None)
        if target is None:
            break
        ok, _ = connect(o.id, target.id)
        if ok:
            used.add(target.id)
            made += 1
    return made


def disconnect_nodes(g: Graph, out_node_id: int, in_node_id: int) -> int:
    removed = 0
    for link in g.links:
        if link.out_node == out_node_id and link.in_node == in_node_id:
            ok, _ = disconnect(link.id)
            removed += ok
    return removed


# ---------------------------------------------------------------- metadata --

def object_metadata(object_id: int) -> dict:
    """All keys set for one object in the default metadata."""
    import re
    rc, out, _ = run(['pw-metadata', str(object_id)])
    vals = {}
    if rc == 0:
        for m in re.finditer(
                r"subject:" + str(object_id) +
                r"\s+key:'([^']+)'\s+value:'([^']*)'", out):
            vals[m.group(1)] = m.group(2)
    return vals


def set_metadata(object_id: int, key: str, value,
                 type_hint: str = '') -> tuple[bool, str]:
    """Set (or with value=None clear) a metadata key on an object."""
    if value is None:
        rc, _, err = run(['pw-metadata', '-d', str(object_id), key])
    else:
        cmd = ['pw-metadata', str(object_id), key, str(value)]
        if type_hint:
            cmd.append(type_hint)
        rc, _, err = run(cmd)
    return rc == 0, err.strip()
