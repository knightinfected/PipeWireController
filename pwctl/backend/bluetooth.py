"""Per-device Bluetooth profile and codec control.

Profiles switch through wpctl (same as the Surround page); codecs use the
documented PulseAudio message API that pipewire-pulse implements
(`pactl send-message /card/<name>/bluez list-codecs | switch-codec`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .pw import pw_dump
from .system import run


@dataclass
class BtDevice:
    id: int                       # PipeWire device object id
    name: str                     # bluez_card.XX_XX_...
    description: str
    profiles: list = field(default_factory=list)   # [(index, desc, available)]
    active_profile: int | None = None
    codecs: list = field(default_factory=list)     # [(name, description)]
    active_codec: str = ''
    battery: str = ''


def list_devices(dump=None) -> list[BtDevice]:
    dump = dump if dump is not None else pw_dump()
    devices = []
    node_codec = {}               # device.id -> api.bluez5.codec on its nodes
    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = (obj.get('info') or {}).get('props') or {}
        codec = props.get('api.bluez5.codec')
        dev = props.get('device.id')
        if codec and dev is not None:
            node_codec[int(dev)] = str(codec)

    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Device':
            continue
        info = obj.get('info') or {}
        props = info.get('props') or {}
        if props.get('device.api') != 'bluez5':
            continue
        params = info.get('params') or {}
        profiles = [(p.get('index'), p.get('description', p.get('name', '?')),
                     p.get('available', 'unknown'))
                    for p in (params.get('EnumProfile') or [])]
        active = next((p.get('index') for p in (params.get('Profile') or [])),
                      None)
        dev = BtDevice(
            id=obj['id'], name=props.get('device.name', ''),
            description=props.get('device.description',
                                  props.get('device.alias', '?')),
            profiles=profiles, active_profile=active,
            active_codec=node_codec.get(obj['id'], ''),
            battery=str(props.get('api.bluez5.battery', '') or ''))
        dev.codecs = list_codecs(dev.name)
        devices.append(dev)
    return devices


def list_codecs(card_name: str) -> list[tuple[str, str]]:
    """Codecs the device supports in its current profile."""
    if not card_name:
        return []
    rc, out, _ = run(['pactl', 'send-message',
                      f'/card/{card_name}/bluez', 'list-codecs'])
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    codecs = []
    for entry in data if isinstance(data, list) else []:
        if isinstance(entry, dict) and entry.get('name'):
            codecs.append((str(entry['name']),
                           str(entry.get('description', entry['name']))))
    return codecs


def switch_codec(card_name: str, codec: str) -> tuple[bool, str]:
    rc, _, err = run(['pactl', 'send-message',
                      f'/card/{card_name}/bluez', 'switch-codec',
                      json.dumps(codec)])
    return rc == 0, err.strip()


def set_profile(device_id: int, profile_index: int) -> bool:
    rc, _, _ = run(['wpctl', 'set-profile', str(device_id),
                    str(profile_index)])
    return rc == 0
