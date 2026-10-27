"""Full PipeWire graph model: every node, port and link (audio and MIDI).

Built from a single pw-dump pass.  Patching goes through pw-link (connect by
port id, disconnect by link id) and metadata edits through pw-metadata, so
the module stays daemon-version agnostic — no bindings required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import levels
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
    id: int                   # global object id — what pw-link takes
    node_id: int
    name: str
    direction: str            # 'in' | 'out'
    is_midi: bool = False
    is_monitor: bool = False
    channel: str = ''
    index: int = -1           # `port.id`: this port's place in its own node,
    #                           i.e. which channel of the layout it carries.
    #                           NOT `id`, which is creation-ordered and puts a
    #                           card's FR before its FL often enough to matter.
    #                           -1 only when the port has no `port.id` at all.
    is_video: bool = False

    @property
    def media(self) -> str:
        """'audio', 'midi' or 'video': only ports of one kind can be linked
        (pw-link refuses MIDI into audio: exit 255, "failed to link ports:
        Success", measured 2026-10-26)."""
        if self.is_midi:
            return 'midi'
        return 'video' if self.is_video else 'audio'


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
    def is_video(self) -> bool:
        return self.media_class.startswith('Video')

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


_DIGITS = re.compile(r'(\d+)')


def natural_key(text: str) -> tuple:
    """Sort key that reads digit runs as numbers: `in2` before `in10`,
    `AUX2` before `AUX10`.  Text and numbers alternate in the tuple, so two
    keys always compare like with like."""
    parts = _DIGITS.split(str(text).casefold())
    return tuple(int(p) if i % 2 else p for i, p in enumerate(parts))


def port_order(ports) -> list[Port]:
    """One side of a node, in the order a user reads its channels.

    By the node's own numbering (`port.id`), which is the channel order of an
    audio layout (FL FR FC LFE…, AUX0 AUX1 … AUX31) and, for a JACK client,
    the order it registered its ports (measured 2026-10-26: a client
    registering Kick, Snare, in_10, in_2 gets port.id 0-3 in that order,
    while the object ids came out scrambled).  Ties and ports without a
    number go by name, digits as numbers.

    A side made only of MIDI ports is a bridge of devices, not a layout:
    its numbering is the order the devices appeared in, so it is sorted by
    name instead, which keeps it stable across replugs.  (Issue #26: this
    used to be a plain text sort, so 2 came after 128 and a 7.1 sink read
    FC FL FR.)
    """
    ports = list(ports)
    if ports and all(p.is_midi for p in ports):
        return sorted(ports, key=lambda p: natural_key(p.name))
    return sorted(ports, key=lambda p: (p.index < 0, p.index,
                                        natural_key(p.name)))


def _int_or(value, default=-1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
            channel=str(props.get('audio.channel', '')),
            # Not `int(... or -1)`: port.id is the integer 0 for every
            # channel-0 port, and `0 or -1` made all of them -1.
            index=_int_or(props.get('port.id')))
        ports_by_node.setdefault(node_id, []).append(port)

    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Node':
            continue
        info = obj.get('info') or {}
        props = info.get('props') or {}
        # Our own level meters are capture streams like any other, so without
        # this the patchbay fills with one phantom node per metered device the
        # moment a page with meters is open.
        if props.get(levels.MARKER):
            continue
        name = props.get('node.name', str(obj['id']))
        plist = ports_by_node.get(obj['id'], [])
        if not plist and not include_hidden:
            continue
        mclass = props.get('media.class', '')
        if mclass.startswith('Video'):
            for p in plist:
                p.is_video = True
        ins = port_order(p for p in plist if p.direction == 'in')
        outs = port_order(p for p in plist if p.direction == 'out')
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
