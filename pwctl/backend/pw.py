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

    @property
    def is_sink(self):
        return self.media_class == 'Audio/Sink'

    @property
    def is_virtual(self):
        # Filter-chain / loopback nodes have no device.api
        return 'device.api' not in self.props


def list_audio_nodes(dump=None) -> list[AudioNode]:
    dump = dump if dump is not None else pw_dump()
    defaults = read_default_names()
    def_sink = defaults.get('default.audio.sink')
    def_source = defaults.get('default.audio.source')
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
            is_default=(name == def_sink if mclass == 'Audio/Sink'
                        else name == def_source))
        vol = get_volume(node.id)
        if vol:
            node.volume, node.muted = vol
        nodes.append(node)
    return nodes


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


def pipewire_version() -> str:
    rc, out, _ = run(['pipewire', '--version'])
    m = re.search(r'libpipewire\s+([\d.]+)', out)
    return m.group(1) if m else '?'
