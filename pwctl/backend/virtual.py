"""Virtual devices: null sinks/sources, aggregate (combine) devices, buses.

Follows the filter-chain design exactly: each virtual device is JSON metadata
plus a generated standalone PipeWire config, running as its own
pwctl-chain@<id>.service instance.  Creating, renaming or deleting one never
interrupts the main graph or other virtual devices.

Kinds:
  null-sink      loopback-free virtual output (apps play into it, other
                 tools record its monitor)
  null-source    virtual microphone (feed it via the patchbay)
  combine-sink   one sink that plays on several real outputs at once
  combine-source one source that records several real inputs at once
  bus            loopback sink whose output shows up as a routable stream —
                 a group/sub-mix with its own volume, feeding any device
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, asdict

from .. import spa_json
from . import system
from .chains import GEN_DIR, ensure_unit
from .config import XDG_CONFIG

VIRT_DIR = XDG_CONFIG / 'pipewire-controller' / 'virtual'

KINDS = {
    'null-sink': 'Virtual output (null sink)',
    'null-source': 'Virtual microphone (null source)',
    'combine-sink': 'Combined output (plays on several devices)',
    'combine-source': 'Combined input (records several devices)',
    'bus': 'Bus / sub-mix (routable group sink)',
}

DEFAULT_POSITIONS = ['FL', 'FR']


@dataclass
class VirtualDevice:
    id: str
    name: str
    kind: str = 'null-sink'
    positions: list = field(default_factory=lambda: list(DEFAULT_POSITIONS))
    members: list = field(default_factory=list)   # node.names for combine-*
    target: str = ''                              # bus output target (node.name)
    enabled: bool = False
    persistent: bool = True                       # False = gone after reboot

    @property
    def node_name(self) -> str:
        return f'pwctl.{self.id}'

    @property
    def unit(self) -> str:
        return f'pwctl-chain@{self.id}.service'

    @property
    def conf_path(self):
        return GEN_DIR / f'{self.id}.conf'

    @property
    def meta_path(self):
        return VIRT_DIR / f'{self.id}.json'


def _slug(name: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'virtual'
    return 'vd-' + s[:36]


def ensure_dirs():
    VIRT_DIR.mkdir(parents=True, exist_ok=True)
    GEN_DIR.mkdir(parents=True, exist_ok=True)


def list_devices() -> list[VirtualDevice]:
    ensure_dirs()
    out = []
    known = set(VirtualDevice.__dataclass_fields__)
    for f in sorted(VIRT_DIR.glob('*.json')):
        try:
            data = json.loads(f.read_text())
            out.append(VirtualDevice(
                **{k: v for k, v in data.items() if k in known}))
        except (ValueError, TypeError):
            continue
    return out


def save_meta(dev: VirtualDevice):
    ensure_dirs()
    system.atomic_write(dev.meta_path, json.dumps(asdict(dev), indent=2))


def new_device(name: str, kind: str, **kw) -> VirtualDevice:
    base = _slug(name)
    vid = base
    existing = {d.id for d in list_devices()}
    while vid in existing:
        vid = f'{base}-{uuid.uuid4().hex[:4]}'
    return VirtualDevice(id=vid, name=name, kind=kind, **kw)


# -------------------------------------------------------------- generation --

def _base(modules: list) -> dict:
    return {
        'context.properties': {'log.level': 2},
        'context.spa-libs': {
            'audio.convert.*': 'audioconvert/libspa-audioconvert',
            'support.*': 'support/libspa-support',
        },
        'context.modules': [
            {'name': 'libpipewire-module-rt', 'args': {},
             'flags': ['ifexists', 'nofail']},
            {'name': 'libpipewire-module-protocol-native'},
            {'name': 'libpipewire-module-client-node'},
            {'name': 'libpipewire-module-adapter'},
            *modules,
        ],
    }


def _null_conf(dev: VirtualDevice) -> dict:
    """Null sink / virtual source, built from module-loopback.

    A helper process injects nodes into the running daemon only through
    client modules (loopback / filter-chain); a context.objects adapter
    would stay local to this process and never appear in the main graph.

    null-sink   : apps play into an Audio/Sink; the loopback's playback side
                  auto-connect is off, so the audio is discarded while the
                  sink's monitor ports stay available for recording.
    null-source : patch audio into the Audio/Sink input; it comes back out of
                  the Audio/Source/Virtual node that apps record from.
    """
    pos = list(dev.positions)
    if dev.kind == 'null-sink':
        capture = {'node.name': dev.node_name, 'media.class': 'Audio/Sink',
                   'node.description': dev.name, 'audio.position': pos}
        playback = {'node.name': f'{dev.node_name}.discard',
                    'node.description': f'{dev.name} (discarded)',
                    'audio.position': pos, 'node.passive': True,
                    'node.autoconnect': False}
    else:
        capture = {'node.name': f'{dev.node_name}.in',
                   'media.class': 'Audio/Sink',
                   'node.description': f'{dev.name} input',
                   'audio.position': pos}
        playback = {'node.name': dev.node_name,
                    'media.class': 'Audio/Source',
                    'node.description': dev.name, 'audio.position': pos}
    args = {'node.description': dev.name,
            'capture.props': capture, 'playback.props': playback}
    return _base([{'name': 'libpipewire-module-loopback', 'args': args}])


def _combine_conf(dev: VirtualDevice) -> dict:
    sink = dev.kind == 'combine-sink'
    member_class = 'Audio/Sink' if sink else 'Audio/Source'
    matches = [{'media.class': member_class, 'node.name': m}
               for m in dev.members]
    args = {
        'combine.mode': 'sink' if sink else 'source',
        'node.name': dev.node_name,
        'node.description': dev.name,
        'combine.latency-compensate': False,
        'combine.props': {'audio.position': list(dev.positions)},
        'stream.props': {},
        'stream.rules': [{'matches': matches,
                          'actions': {'create-stream': {}}}],
    }
    return _base([{'name': 'libpipewire-module-combine-stream',
                   'args': args}])


def _bus_conf(dev: VirtualDevice) -> dict:
    playback = {
        'node.name': f'{dev.node_name}.out',
        'node.description': f'{dev.name} out',
        'node.passive': True,
        'audio.position': list(dev.positions),
    }
    if dev.target:
        playback['target.object'] = dev.target
        playback['node.dont-reconnect'] = False
    args = {
        'node.description': dev.name,
        'capture.props': {
            'node.name': dev.node_name,
            'media.class': 'Audio/Sink',
            'audio.position': list(dev.positions),
        },
        'playback.props': playback,
    }
    return _base([{'name': 'libpipewire-module-loopback', 'args': args}])


def generate(dev: VirtualDevice):
    ensure_dirs()
    if dev.kind in ('null-sink', 'null-source'):
        conf = _null_conf(dev)
    elif dev.kind in ('combine-sink', 'combine-source'):
        conf = _combine_conf(dev)
    elif dev.kind == 'bus':
        conf = _bus_conf(dev)
    else:
        raise ValueError(f'unknown virtual device kind {dev.kind!r}')
    header = (f'{dev.name}\nGenerated by PipeWire Controller '
              f'(virtual device: {dev.kind}). Do not edit by hand.')
    text = spa_json.dumps(conf, header=header)
    spa_json.loads(text)          # sanity check before writing
    system.atomic_write(dev.conf_path, text)
    save_meta(dev)


# --------------------------------------------------------------- lifecycle --

def apply(dev: VirtualDevice) -> tuple[bool, str]:
    """Regenerate and (re)start/stop the unit to match `enabled`."""
    try:
        generate(dev)
    except (spa_json.SpaJsonError, ValueError) as e:
        return False, str(e)
    ensure_unit()
    if dev.enabled:
        verb = 'enable' if dev.persistent else 'start'
        args = ('enable', '--now', dev.unit) if dev.persistent \
            else ('start', dev.unit)
        rc, _, err = system.sysctl_user(*args)
        if rc != 0:
            return False, err.strip() or f'failed to {verb} unit'
        rc, _, err = system.sysctl_user('restart', dev.unit, timeout=30)
        return (rc == 0), (err.strip() if rc else '')
    system.sysctl_user('disable', '--now', dev.unit)
    return True, ''


def set_enabled(dev: VirtualDevice, enabled: bool) -> tuple[bool, str]:
    dev.enabled = enabled
    save_meta(dev)
    return apply(dev)


def status(dev: VirtualDevice) -> str:
    return system.unit_state(dev.unit)


def delete(dev: VirtualDevice):
    system.sysctl_user('disable', '--now', dev.unit)
    for p in (dev.conf_path, dev.meta_path):
        if p.is_file():
            p.unlink()
