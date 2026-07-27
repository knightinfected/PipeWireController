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
                  the Audio/Source node that apps record from.

    node.autoconnect=false on the playback sides is essential: a loopback
    playback stream is directionally an OUTPUT even when it carries
    media.class=Audio/Source, so with autoconnect left on WirePlumber links it
    to the *default sink* — the virtual mic then shows phantom activity routed
    to the speakers with nothing in the graph to explain it (same class of
    gotcha as the pro-map nodes).
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
                    'node.description': dev.name, 'audio.position': pos,
                    'node.autoconnect': False}
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


# ----------------------------------------------------------------- import --
# Users hand-write loopback / null-sink / combine drop-ins in
# pipewire.conf.d that the MAIN daemon loads at startup.  Importing adopts them
# into our per-process-unit model so they gain the page's edit/enable controls.
# We scan only the user dir (system files aren't ours to move) and skip our own
# generated confs (they live in GEN_DIR, never here).

IMPORT_DIRS = [XDG_CONFIG / 'pipewire' / 'pipewire.conf.d']
INACTIVE_DIR = 'inactive'


def _positions(*props) -> list:
    for p in props:
        ap = p.get('audio.position') if isinstance(p, dict) else None
        if isinstance(ap, list) and ap:
            return [str(x) for x in ap]
    return list(DEFAULT_POSITIONS)


def _is_ours(*props) -> bool:
    for p in props:
        nn = p.get('node.name', '') if isinstance(p, dict) else ''
        if isinstance(nn, str) and nn.startswith('pwctl.'):
            return True
    return False


def _classify_module(mod: dict) -> dict | None:
    """Map one context.modules entry to a VirtualDevice spec, or None."""
    name = mod.get('name')
    args = mod.get('args') if isinstance(mod.get('args'), dict) else {}
    if name == 'libpipewire-module-loopback':
        cap = args.get('capture.props') if isinstance(
            args.get('capture.props'), dict) else {}
        play = args.get('playback.props') if isinstance(
            args.get('playback.props'), dict) else {}
        if _is_ours(cap, play):
            return None                       # already one of ours
        desc = (args.get('node.description') or cap.get('node.description')
                or play.get('node.description') or 'Imported device')
        positions = _positions(cap, play)
        if play.get('media.class') == 'Audio/Source':
            return {'name': str(desc), 'kind': 'null-source',
                    'positions': positions}
        target = play.get('target.object')
        if isinstance(target, str) and target:
            return {'name': str(desc), 'kind': 'bus',
                    'positions': positions, 'target': target}
        return {'name': str(desc), 'kind': 'null-sink', 'positions': positions}
    if name == 'libpipewire-module-combine-stream':
        if _is_ours(args):
            return None
        kind = 'combine-source' if args.get('combine.mode') == 'source' \
            else 'combine-sink'
        desc = args.get('node.description') or 'Imported combined device'
        combine_props = args.get('combine.props') if isinstance(
            args.get('combine.props'), dict) else {}
        members = []
        for rule in args.get('stream.rules') or []:
            for m in (rule.get('matches') or []):
                nn = m.get('node.name') if isinstance(m, dict) else None
                if isinstance(nn, str) and nn not in members:
                    members.append(nn)
        return {'name': str(desc), 'kind': kind,
                'positions': _positions(combine_props), 'members': members}
    return None


def _classify_object(obj: dict) -> dict | None:
    """Map a context.objects null-audio-sink adapter to a spec, or None."""
    if obj.get('factory') != 'adapter':
        return None
    args = obj.get('args') if isinstance(obj.get('args'), dict) else {}
    if args.get('factory.name') != 'support.null-audio-sink':
        return None
    if _is_ours(args):
        return None
    desc = args.get('node.description') or args.get('node.name') or 'Null sink'
    kind = 'null-source' if 'Source' in str(args.get('media.class', '')) \
        else 'null-sink'
    return {'name': str(desc), 'kind': kind, 'positions': _positions(args)}


def sniff_conf(path) -> dict:
    """Inspect a drop-in for importable virtual devices.

    Recognises loopback null sink / virtual mic / bus, combine sink/source,
    and null-audio-sink adapters.  Returns {'name', 'devices': [spec, …],
    'valid'} — `valid` is True when at least one device was recognised.
    """
    info = {'name': Path(path).stem, 'devices': [], 'valid': False}
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
        data = spa_json.loads(text)
    except (OSError, spa_json.SpaJsonError):
        return info
    if not isinstance(data, dict):
        return info
    for mod in data.get('context.modules') or []:
        if isinstance(mod, dict):
            spec = _classify_module(mod)
            if spec:
                info['devices'].append(spec)
    for obj in data.get('context.objects') or []:
        if isinstance(obj, dict):
            spec = _classify_object(obj)
            if spec:
                info['devices'].append(spec)
    info['valid'] = bool(info['devices'])
    return info


def _archive_source(path) -> Path:
    """Move an imported drop-in into a sibling inactive/ folder so the main
    daemon stops loading it (subdirs aren't scanned).  Reversible."""
    p = Path(path)
    dest_dir = p.parent / INACTIVE_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / p.name
    if dest.exists():
        dest = dest_dir / f'{p.stem}-{uuid.uuid4().hex[:4]}{p.suffix}'
    p.rename(dest)
    return dest


def import_conf(path) -> list[VirtualDevice]:
    """Adopt a drop-in's virtual devices under app management.

    Creates a disabled VirtualDevice per recognised device (config + meta
    written, unit not started), then moves the original drop-in into an
    inactive/ folder so the main daemon won't also load it after the next
    PipeWire restart.  Returns the created devices ([] if none recognised).
    """
    info = sniff_conf(path)
    if not info['valid']:
        return []
    out = []
    for spec in info['devices']:
        dev = new_device(
            spec['name'], spec['kind'],
            positions=spec.get('positions', list(DEFAULT_POSITIONS)),
            members=spec.get('members', []),
            target=spec.get('target', ''))
        dev.enabled = False
        generate(dev)                 # writes conf + meta, no unit start
        out.append(dev)
    if out:
        _archive_source(path)
    return out


def scan_importable() -> list[Path]:
    """User pipewire.conf.d drop-ins that declare an importable device."""
    found = []
    for d in IMPORT_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob('*.conf')):
            if p.is_file() and sniff_conf(p)['valid']:
                found.append(p)
    return found
