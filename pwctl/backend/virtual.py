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
from pathlib import Path

from .. import spa_json
from . import system
from .chains import GEN_DIR, ensure_unit
from .config import XDG_CONFIG

VIRT_DIR = XDG_CONFIG / 'pipewire-controller' / 'virtual'
SYSTEMD_USER = Path.home() / '.config' / 'systemd' / 'user'

KINDS = {
    'null-sink': 'Virtual output (null sink)',
    'null-source': 'Virtual microphone (null source)',
    'combine-sink': 'Combined output (plays on several devices)',
    'combine-source': 'Combined input (records several devices)',
    'bus': 'Bus / sub-mix (routable group sink)',
    'pro-map-sink': 'Pro Audio output map (channels → AUX)',
    'pro-map-source': 'Pro Audio input map (AUX → virtual mic)',
}

DEFAULT_POSITIONS = ['FL', 'FR']

# Channel names offered for the virtual side of a Pro Audio map.
POSITION_NAMES = ['MONO', 'FL', 'FR', 'FC', 'LFE', 'RL', 'RR', 'SL', 'SR',
                  'RC', 'TFL', 'TFR', 'TRL', 'TRR']

_AUX_RE = re.compile(r'^AUX\d+$')


@dataclass
class VirtualDevice:
    id: str
    name: str
    kind: str = 'null-sink'
    positions: list = field(default_factory=lambda: list(DEFAULT_POSITIONS))
    members: list = field(default_factory=list)   # node.names for combine-*
    target: str = ''                              # bus/pro-map target (node.name)
    target_positions: list = field(default_factory=list)  # AUX names for pro-map
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


def list_pro_targets(direction: str) -> list[tuple[str, str, list[str]]]:
    """Pro Audio devices exposing generic AUX channels.

    direction 'sink'   → Audio/Sink targets whose playback ports are AUX*
                         (map a virtual sink onto them).
    direction 'source' → Audio/Source targets whose capture ports are AUX*
                         (capture them into a virtual mic).
    Returns [(node.name, description, [AUX0, AUX1, …])], AUX list in numeric
    order.  Empty when no card is in the Pro Audio profile.
    """
    from . import graph
    want_sink = direction == 'sink'
    out = []
    for n in graph.snapshot().nodes.values():
        if n.kind != ('sink' if want_sink else 'source'):
            continue
        if n.name.startswith('pwctl.'):
            continue                      # never target our own virtuals
        ports = n.inputs if want_sink else n.outputs
        aux = sorted((p.channel for p in ports if _AUX_RE.match(p.channel)),
                     key=lambda c: int(c[3:]))
        if aux:
            out.append((n.name, n.label, aux))
    out.sort(key=lambda t: t[1].lower())
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


def _pro_map_conf(dev: VirtualDevice) -> dict:
    """Map a virtual sink/source onto specific Pro Audio AUX channels.

    When a card is in the "Pro Audio" profile it exposes every channel as a
    flat set of generic AUX ports (AUX0, AUX1, …) with no stereo/surround
    grouping.  This builds a small loopback whose *virtual* side carries the
    friendly layout (positions, e.g. [FL FR]) and whose *hardware* side
    declares the target's AUX names (target_positions, e.g. [AUX0 AUX1]).

    positions[i] pairs by index with target_positions[i]; stream.dont-remix
    keeps it a straight per-channel passthrough (no up/downmix, so FL doesn't
    get "interpreted" onto an unnamed AUX channel).

    WirePlumber will NOT auto-route a stream onto a Pro Audio node (those are
    meant for manual routing), so target.object is ignored and autoconnect
    falls back to the default sink.  We therefore set node.autoconnect=false
    here and create the exact links ourselves after the node appears (see
    apply()) — the loopback conf can't declare them because the ports don't
    exist yet at config-parse time.
    """
    pos = list(dev.positions)
    aux = list(dev.target_positions)
    if not pos or len(pos) != len(aux):
        raise ValueError('channel map must pair each virtual channel with '
                         'exactly one AUX channel')
    if not dev.target:
        raise ValueError('choose a target Pro Audio device')
    hw = {'audio.position': aux, 'stream.dont-remix': True,
          'node.passive': True, 'node.autoconnect': False}
    if dev.kind == 'pro-map-sink':
        hw['node.name'] = f'{dev.node_name}.out'
        hw['node.description'] = f'{dev.name} → {dev.target}'
        capture = {'node.name': dev.node_name, 'media.class': 'Audio/Sink',
                   'node.description': dev.name, 'audio.position': pos}
        playback = hw
    else:  # pro-map-source
        hw['node.name'] = f'{dev.node_name}.in'
        hw['node.description'] = f'{dev.name} ← {dev.target}'
        capture = hw
        playback = {'node.name': dev.node_name,
                    'media.class': 'Audio/Source',
                    'node.description': dev.name, 'audio.position': pos}
    args = {'node.description': dev.name,
            'capture.props': capture, 'playback.props': playback}
    return _base([{'name': 'libpipewire-module-loopback', 'args': args}])


# --- pro-map explicit linking (WirePlumber won't auto-route to Pro Audio) ---
# The links can't live in the loopback conf (ports don't exist at parse time),
# so a per-instance systemd drop-in runs pw-link after the service starts —
# which reruns on every start, including boot and PipeWire restarts.

def _pro_link_pairs(dev: VirtualDevice) -> list[tuple[str, str]]:
    """(output_port, input_port) full names for the device's channel map."""
    if dev.kind == 'pro-map-sink':
        return [(f'{dev.node_name}.out:output_{a}',
                 f'{dev.target}:playback_{a}') for a in dev.target_positions]
    return [(f'{dev.target}:capture_{a}',
             f'{dev.node_name}.in:input_{a}') for a in dev.target_positions]


def _dropin_dir(dev: VirtualDevice) -> Path:
    return SYSTEMD_USER / f'pwctl-chain@{dev.id}.service.d'


def _write_pro_dropin(dev: VirtualDevice):
    aux0 = dev.target_positions[0]
    if dev.kind == 'pro-map-sink':
        ready = (f'pw-link -o 2>/dev/null | grep -q '
                 f'"{dev.node_name}.out:output_{aux0}"')
    else:
        ready = (f'pw-link -i 2>/dev/null | grep -q '
                 f'"{dev.node_name}.in:input_{aux0}"')
    links = '; '.join(f'pw-link "{o}" "{i}" 2>/dev/null'
                      for o, i in _pro_link_pairs(dev))
    # fixed iteration list (no $(), which systemd would try to expand)
    ticks = ' '.join(str(i) for i in range(1, 21))
    # the unit sets PIPEWIRE_CONFIG_DIR for the loopback; pw-link must NOT
    # inherit it (that dir has no client.conf, so pw-link can't connect).
    script = (f'unset PIPEWIRE_CONFIG_DIR; for _ in {ticks}; do if {ready}; '
              f'then {links}; break; fi; sleep 0.25; done')
    text = ('# Managed by PipeWire Controller — links the Pro Audio channel '
            'map after the loopback node appears.\n'
            f"[Service]\nExecStartPost=-/bin/sh -c '{script}'\n")
    d = _dropin_dir(dev)
    d.mkdir(parents=True, exist_ok=True)
    system.atomic_write(d / '50-pro-map.conf', text)


def _remove_pro_dropin(dev: VirtualDevice):
    d = _dropin_dir(dev)
    f = d / '50-pro-map.conf'
    if f.is_file():
        f.unlink()
    if d.is_dir():
        try:
            d.rmdir()
        except OSError:
            pass


def generate(dev: VirtualDevice):
    ensure_dirs()
    if dev.kind in ('null-sink', 'null-source'):
        conf = _null_conf(dev)
    elif dev.kind in ('combine-sink', 'combine-source'):
        conf = _combine_conf(dev)
    elif dev.kind == 'bus':
        conf = _bus_conf(dev)
    elif dev.kind in ('pro-map-sink', 'pro-map-source'):
        conf = _pro_map_conf(dev)
    else:
        raise ValueError(f'unknown virtual device kind {dev.kind!r}')
    header = (f'{dev.name}\nGenerated by PipeWire Controller '
              f'(virtual device: {dev.kind}). Do not edit by hand.')
    text = spa_json.dumps(conf, header=header)
    spa_json.loads(text)          # sanity check before writing
    system.atomic_write(dev.conf_path, text)
    if dev.kind in ('pro-map-sink', 'pro-map-source'):
        _write_pro_dropin(dev)
    else:
        _remove_pro_dropin(dev)
    save_meta(dev)


# --------------------------------------------------------------- lifecycle --

def apply(dev: VirtualDevice) -> tuple[bool, str]:
    """Regenerate and (re)start/stop the unit to match `enabled`."""
    try:
        generate(dev)
    except (spa_json.SpaJsonError, ValueError) as e:
        return False, str(e)
    ensure_unit()
    if dev.kind in ('pro-map-sink', 'pro-map-source'):
        system.daemon_reload()    # pick up the per-instance link drop-in
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
    _remove_pro_dropin(dev)
    for p in (dev.conf_path, dev.meta_path):
        if p.is_file():
            p.unlink()
    system.daemon_reload()
