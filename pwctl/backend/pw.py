"""Live PipeWire state: pw-dump, pw-metadata settings, wpctl devices."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .system import run

# ------------------------------------------------------------- pw-metadata --

_META_RE = re.compile(r"key:'([^']+)'\s+value:'([^']*)'")

SETTINGS_KEYS = (
    'log.level', 'clock.rate', 'clock.allowed-rates', 'clock.quantum',
    'clock.min-quantum', 'clock.max-quantum', 'clock.force-quantum',
    'clock.force-rate',
)


def read_settings() -> dict:
    """Runtime settings from the `settings` metadata (pw-metadata -n settings)."""
    rc, out, _ = run(['pw-metadata', '-n', 'settings'])
    vals = {}
    if rc == 0:
        for m in _META_RE.finditer(out):
            vals[m.group(1)] = m.group(2)
    return vals


def set_setting(key: str, value) -> bool:
    rc, _, _ = run(['pw-metadata', '-n', 'settings', '0', key, str(value)])
    return rc == 0


def read_default_names() -> dict:
    """Default sink/source node names from the `default` metadata."""
    rc, out, _ = run(['pw-metadata', '-n', 'default'])
    res = {}
    if rc == 0:
        for m in re.finditer(r"key:'(default\.[\w.]+)'\s+value:'({[^']*})'", out):
            try:
                res[m.group(1)] = json.loads(m.group(2)).get('name')
            except (ValueError, AttributeError):
                pass
    return res


# ----------------------------------------------------------------- pw-dump --

def pw_dump():
    rc, out, _ = run(['pw-dump'], timeout=15)
    if rc != 0 or not out.strip():
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


@dataclass
class AudioNode:
    id: int
    name: str
    description: str
    media_class: str
    props: dict = field(default_factory=dict)
    is_default: bool = False
    volume: float | None = None
    muted: bool = False
    serial: int = -1
    ports: list = field(default_factory=list)   # [(route_index, description, available)]
    active_port: int | None = None

    @property
    def is_sink(self):
        return self.media_class == 'Audio/Sink'

    @property
    def is_virtual(self):
        # Filter-chain / loopback nodes have no device.api
        return 'device.api' not in self.props


def _device_routes(dump) -> dict:
    """device object id -> (EnumRoute list, active Route list)."""
    routes = {}
    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Device':
            continue
        info = obj.get('info') or {}
        props = info.get('props') or {}
        if props.get('media.class') != 'Audio/Device':
            continue
        params = info.get('params') or {}
        routes[obj['id']] = (params.get('EnumRoute') or [],
                            params.get('Route') or [])
    return routes


def _attach_ports(node: AudioNode, routes: dict):
    """Fill node.ports / node.active_port from its device's Route params."""
    try:
        dev_id = int(node.props['device.id'])
        card_dev = int(node.props['card.profile.device'])
    except (KeyError, TypeError, ValueError):
        return
    enum, active = routes.get(dev_id, ([], []))
    want = 'Output' if node.is_sink else 'Input'
    node.ports = [(r['index'], r.get('description', f'Route {r["index"]}'),
                   r.get('available', 'unknown'))
                  for r in enum
                  if r.get('direction') == want
                  and card_dev in (r.get('devices') or [])]
    node.active_port = next((r['index'] for r in active
                             if r.get('device') == card_dev
                             and r.get('direction') == want), None)


def list_audio_nodes(dump=None) -> list[AudioNode]:
    dump = dump if dump is not None else pw_dump()
    defaults = read_default_names()
    def_sink = defaults.get('default.audio.sink')
    def_source = defaults.get('default.audio.source')
    routes = _device_routes(dump)
    nodes = []
    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = (obj.get('info') or {}).get('props') or {}
        mclass = props.get('media.class', '')
        if mclass not in ('Audio/Sink', 'Audio/Source'):
            continue
        name = props.get('node.name', '')
        node = AudioNode(
            id=obj['id'], name=name,
            description=props.get('node.description') or props.get('node.nick') or name,
            media_class=mclass, props=props,
            serial=props.get('object.serial', -1),
            is_default=(name == def_sink if mclass == 'Audio/Sink'
                        else name == def_source))
        _attach_ports(node, routes)
        vol = get_volume(node.id)
        if vol:
            node.volume, node.muted = vol
        nodes.append(node)
    return nodes


# ----------------------------------------------------------------- streams --

STREAM_CLASSES = ('Stream/Output/Audio', 'Stream/Input/Audio')


@dataclass
class Stream:
    id: int
    serial: int
    name: str            # application.name / media.name / node.name
    media: str           # media.name ("what is playing")
    media_class: str
    icon: str | None = None
    binary: str | None = None
    props: dict = field(default_factory=dict)
    volume: float | None = None
    muted: bool = False
    target_id: int | None = None   # node id of the device it is linked to

    @property
    def is_playback(self):
        return self.media_class == 'Stream/Output/Audio'


def list_streams(dump=None) -> list[Stream]:
    """Application playback/recording streams with their current device.

    The connected device is derived from the live Link objects (ground
    truth), not from target.* metadata which may be stale or absent.
    """
    dump = dump if dump is not None else pw_dump()
    classes = {}
    links = []
    for obj in dump:
        t = obj.get('type')
        if t == 'PipeWire:Interface:Node':
            p = (obj.get('info') or {}).get('props') or {}
            classes[obj['id']] = p.get('media.class', '')
        elif t == 'PipeWire:Interface:Link':
            i = obj.get('info') or {}
            links.append((i.get('output-node-id'), i.get('input-node-id')))
    streams = []
    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = (obj.get('info') or {}).get('props') or {}
        mclass = props.get('media.class', '')
        if mclass not in STREAM_CLASSES:
            continue
        if props.get('media.name') == 'Peak detect':   # pavucontrol probes
            continue
        name = (props.get('application.name') or props.get('media.name')
                or props.get('node.name', ''))
        s = Stream(id=obj['id'], serial=props.get('object.serial', -1),
                   name=name, media=props.get('media.name', ''),
                   media_class=mclass,
                   icon=props.get('application.icon-name'),
                   binary=props.get('application.process.binary'),
                   props=props)
        if s.is_playback:
            s.target_id = next((dst for src, dst in links
                                if src == s.id
                                and classes.get(dst) == 'Audio/Sink'), None)
        else:
            # capture from a source, or from a sink's monitor ports
            s.target_id = next((src for src, dst in links
                                if dst == s.id
                                and classes.get(src) in ('Audio/Sink',
                                                         'Audio/Source')), None)
        vol = get_volume(s.id)
        if vol:
            s.volume, s.muted = vol
        streams.append(s)
    streams.sort(key=lambda s: (s.name.lower(), s.id))
    return streams


def move_stream(stream_id: int, target_serial: int) -> bool:
    """Route a stream to another device; WirePlumber moves it live."""
    rc, _, _ = run(['pw-metadata', str(stream_id), 'target.object',
                    str(target_serial), 'Spa:Id'])
    return rc == 0


def driver_clock(dump=None) -> dict:
    """Current rate/quantum from the running driver node, if any."""
    dump = dump if dump is not None else pw_dump()
    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Node':
            continue
        info = obj.get('info') or {}
        state = info.get('state')
        props = info.get('props') or {}
        if state == 'running' and props.get('media.class') in ('Audio/Sink', 'Audio/Source'):
            rate = props.get('clock.rate') or 0
            quantum = props.get('clock.quantum') or 0
            if rate and quantum:
                return {'rate': int(rate), 'quantum': int(quantum)}
    return {}


# ------------------------------------------------------------------- wpctl --

_VOL_RE = re.compile(r'Volume:\s*([\d.]+)\s*(\[MUTED\])?')


def get_volume(node_id):
    rc, out, _ = run(['wpctl', 'get-volume', str(node_id)])
    if rc != 0:
        return None
    m = _VOL_RE.search(out)
    if not m:
        return None
    return float(m.group(1)), bool(m.group(2))


def set_volume(node_id, value: float) -> bool:
    rc, _, _ = run(['wpctl', 'set-volume', str(node_id), f'{value:.2f}'])
    return rc == 0


def set_mute(node_id, mute: bool) -> bool:
    rc, _, _ = run(['wpctl', 'set-mute', str(node_id), '1' if mute else '0'])
    return rc == 0


def set_default(node_id) -> bool:
    rc, _, _ = run(['wpctl', 'set-default', str(node_id)])
    return rc == 0


def set_route(node_id, route_index: int) -> bool:
    """Switch a device node's port (route), e.g. speakers -> headphones."""
    rc, _, _ = run(['wpctl', 'set-route', str(node_id), str(route_index)])
    return rc == 0


def pipewire_version() -> str:
    rc, out, _ = run(['pipewire', '--version'])
    m = re.search(r'libpipewire\s+([\d.]+)', out)
    return m.group(1) if m else '?'
