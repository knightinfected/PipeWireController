"""Signal paths: strips of processing, each one fused filter graph.

A path is not one object but two kinds of strip:

  * a **source** — an app, a microphone, or everything on the default output —
    carrying its own chain of stages, sending its result to one or more mixes;
  * a **mix** — a chain of its own, feeding one or more output devices.

Sources on the left, mixes on the right, sends between them.  With one source
and one mix that is just a straight line, which is what most people want; the
arrangement only earns its second dimension when a chain has to split (a
shared chain into two different reverbs into different devices, say).

Both kinds are the same dataclass with a `role`, because everything below —
storage, graph generation, the unit lifecycle — is identical for the two.

**One node per strip, not one per stage.**  Every enabled stage is compiled
into a single `filter.graph` running in one `pipewire -c` process, so a chain
of twenty plugins is one sink and one buffer hop rather than twenty of each.
That is the whole reason this module exists: hand-written configs force a sink
per plugin, which is how people end up with twenty entries in their device
list and twenty quanta of latency.

Channels are declared once, on the strip.  The graph is built as explicit
per-channel lanes rather than relying on PipeWire's "duplicate the graph to
match the channel count" shortcut, because that shortcut only works when every
stage has the same port count — mixing a mono equalizer with a stereo plugin
breaks it.  Lanes also leave room for per-stage channel rules later.

Rings are impossible between a source and a mix (a mix only ever outputs to a
device), but a mix output is a name the user picked, so it is still checked
with the same walker the chains and equalizers use.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .. import spa_json
from . import system, virtual
from .chains import GEN_DIR, ensure_unit, pick_targets, would_loop
from .config import XDG_CONFIG

PATH_DIR = XDG_CONFIG / 'pipewire-controller' / 'paths'

ROLES = ('source', 'mix')
SOURCE_KINDS = {
    'app': 'An application',
    'mic': 'A microphone',
    'everything': 'Everything (this strip becomes the default output)',
}
STAGE_KINDS = ('eq', 'effect', 'convolver')

# AutoEQ/APO band types -> the builtin biquad that implements them.  Biquad
# control ports (Freq/Q/Gain) can be written at runtime, which a param_eq
# node cannot, so an equalizer stage is built from these rather than from
# `param_eq`: the curve becomes editable while audio is playing.
BAND_FILTERS = {
    'PK': 'bq_peaking',
    'LSC': 'bq_lowshelf',
    'HSC': 'bq_highshelf',
}

DEFAULT_POSITIONS = ['FL', 'FR']


@dataclass
class Strip:
    id: str
    name: str
    role: str = 'source'                      # 'source' | 'mix'
    kind: str = 'app'                         # sources only, see SOURCE_KINDS
    positions: list = field(default_factory=lambda: list(DEFAULT_POSITIONS))
    stages: list = field(default_factory=list)     # stage dicts, see _stage_*
    sends: list = field(default_factory=list)      # source -> mix ids
    outputs: list = field(default_factory=list)    # mix -> device node.names
    match: dict = field(default_factory=dict)      # source -> app match rule
    enabled: bool = False
    persistent: bool = True

    @property
    def node_name(self) -> str:
        return f'pwctl.{self.role}.{self.id}'

    @property
    def unit(self) -> str:
        return f'pwctl-chain@{self.id}.service'

    @property
    def conf_path(self) -> Path:
        return GEN_DIR / f'{self.id}.conf'

    @property
    def meta_path(self) -> Path:
        return PATH_DIR / f'{self.id}.json'

    @property
    def fan_id(self) -> str:
        """Id of the combine device built when this strip feeds several."""
        return f'{self.id}-fan'

    @property
    def channels(self) -> int:
        return len(self.positions)

    def active_stages(self) -> list[dict]:
        return [s for s in self.stages if not s.get('bypass')]


# ------------------------------------------------------------------ store --

def _slug(name: str, role: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or role
    return f'p{role[:3]}-{s[:34]}'


def ensure_dirs():
    PATH_DIR.mkdir(parents=True, exist_ok=True)
    GEN_DIR.mkdir(parents=True, exist_ok=True)


def list_strips() -> list[Strip]:
    ensure_dirs()
    out = []
    known = set(Strip.__dataclass_fields__)
    for f in sorted(PATH_DIR.glob('*.json')):
        try:
            data = json.loads(f.read_text())
            out.append(Strip(**{k: v for k, v in data.items() if k in known}))
        except (ValueError, TypeError):
            continue
    return out


def sources(strips=None) -> list[Strip]:
    return [s for s in (strips if strips is not None else list_strips())
            if s.role == 'source']


def mixes(strips=None) -> list[Strip]:
    return [s for s in (strips if strips is not None else list_strips())
            if s.role == 'mix']


def save_meta(strip: Strip):
    ensure_dirs()
    system.atomic_write(strip.meta_path, json.dumps(asdict(strip), indent=2))


def new_strip(name: str, role: str, **kw) -> Strip:
    if role not in ROLES:
        raise ValueError(f'unknown role {role!r}')
    base = _slug(name, role)
    sid = base
    existing = {s.id for s in list_strips()}
    while sid in existing:
        sid = f'{base}-{uuid.uuid4().hex[:4]}'
    return Strip(id=sid, name=name, role=role, **kw)


def new_stage(kind: str, name: str = '', **params) -> dict:
    """A stage dict.  Kept as plain data so it round-trips through JSON."""
    if kind not in STAGE_KINDS:
        raise ValueError(f'unknown stage kind {kind!r}')
    return {'id': uuid.uuid4().hex[:8], 'kind': kind,
            'name': name or kind, 'bypass': False, 'params': dict(params)}


# ------------------------------------------------------------ graph build --
# Each stage is asked to instantiate itself across the strip's channels and
# hand back, per channel, the port audio enters on and the port it leaves on.
# The caller stitches consecutive stages together, so a stage never needs to
# know what sits either side of it.

def _node(nodes: list, name: str, ntype: str, label: str = '',
          plugin: str = '', control: dict | None = None,
          config: dict | None = None) -> str:
    d: dict = {'type': ntype, 'name': name}
    if plugin:
        d['plugin'] = plugin
    if label:
        d['label'] = label
    if config:
        d['config'] = dict(config)
    if control:
        d['control'] = dict(control)
    nodes.append(d)
    return name


def _preamp_mult(db) -> float:
    return round(10.0 ** (float(db) / 20.0), 6)


def _eq_lane(stage: dict, tag: str, chan: int, nodes: list,
             links: list) -> tuple[str, str]:
    """One channel of an equalizer: preamp, then a biquad per active band."""
    p = stage.get('params') or {}
    chain: list[str] = []
    preamp = float(p.get('preamp') or 0.0)
    if preamp:
        chain.append(_node(nodes, f'{tag}c{chan}pre', 'builtin', 'linear',
                           control={'Mult': _preamp_mult(preamp),
                                    'Add': 0.0}))
    for bi, band in enumerate(p.get('bands') or []):
        if not band.get('on', True):
            continue
        label = BAND_FILTERS.get(str(band.get('type', 'PK')).upper())
        if not label:
            continue
        chain.append(_node(
            nodes, f'{tag}c{chan}b{bi}', 'builtin', label,
            control={'Freq': float(band.get('freq', 1000.0)),
                     'Q': float(band.get('q', 1.0)),
                     'Gain': float(band.get('gain', 0.0))}))
    if not chain:
        # A curve with nothing switched on still has to pass audio.
        chain.append(_node(nodes, f'{tag}c{chan}flat', 'builtin', 'copy'))
    for a, b in zip(chain, chain[1:]):
        links.append({'output': f'{a}:Out', 'input': f'{b}:In'})
    return f'{chain[0]}:In', f'{chain[-1]}:Out'


def _plugin_ports(stage: dict) -> tuple[list, list]:
    p = stage.get('params') or {}
    return list(p.get('audio_in') or []), list(p.get('audio_out') or [])


def _effect_lanes(stage: dict, tag: str, channels: int, nodes: list,
                  links: list) -> tuple[list, list]:
    """A plugin across every channel.

    A mono plugin is instantiated once per channel.  A stereo plugin is
    instantiated once per channel *pair*, which is how a 7.1 strip ends up
    with four instances — and why an odd channel out (a lone LFE, say) is
    carried past the stage by a `copy` rather than being dropped.
    """
    p = stage.get('params') or {}
    ins, outs = _plugin_ports(stage)
    if not ins or not outs:
        raise ValueError(
            f"{stage.get('name') or p.get('plugin')}: audio ports unknown — "
            'this plugin can only be used on its own in a rack')
    width = 2 if (len(ins) >= 2 and len(outs) >= 2) else 1
    in_ports: list[str] = []
    out_ports: list[str] = []
    chan = 0
    idx = 0
    while chan < channels:
        if width == 2 and chan + 1 >= channels:
            name = _node(nodes, f'{tag}c{chan}pass', 'builtin', 'copy')
            in_ports.append(f'{name}:In')
            out_ports.append(f'{name}:Out')
            chan += 1
            continue
        name = _node(nodes, f'{tag}i{idx}', p.get('type', 'ladspa'),
                     label=p.get('label', ''), plugin=p.get('plugin', ''),
                     control=p.get('controls') or None)
        take = min(width, channels - chan)
        for k in range(take):
            in_ports.append(f'{name}:{ins[k]}')
            out_ports.append(f'{name}:{outs[k]}')
        chan += take
        idx += 1
    return in_ports, out_ports


def _convolver_lane(stage: dict, tag: str, chan: int, nodes: list,
                    _links: list) -> tuple[str, str]:
    p = stage.get('params') or {}
    filename = p.get('filename')
    if not filename:
        raise ValueError(f"{stage.get('name') or 'convolver'}: no impulse "
                         'response chosen')
    config = {'filename': str(filename)}
    if p.get('channel') is not None:
        config['channel'] = int(p['channel'])
    name = _node(nodes, f'{tag}c{chan}', 'builtin', 'convolver',
                 config=config,
                 control={'Gain': float(p.get('gain', 1.0))})
    return f'{name}:In', f'{name}:Out'


def build_graph(strip: Strip) -> dict:
    """The fused `filter.graph` for every enabled stage on this strip."""
    channels = strip.channels
    if channels < 1:
        raise ValueError('a strip needs at least one channel')
    nodes: list = []
    links: list = []
    entry: list = [None] * channels     # port the graph takes audio in on
    cur: list = [None] * channels       # port currently carrying each channel

    for si, stage in enumerate(strip.active_stages()):
        tag = f's{si}'
        kind = stage.get('kind')
        if kind == 'eq':
            lanes = [_eq_lane(stage, tag, c, nodes, links)
                     for c in range(channels)]
            ins = [a for a, _ in lanes]
            outs = [b for _, b in lanes]
        elif kind == 'convolver':
            lanes = [_convolver_lane(stage, tag, c, nodes, links)
                     for c in range(channels)]
            ins = [a for a, _ in lanes]
            outs = [b for _, b in lanes]
        elif kind == 'effect':
            ins, outs = _effect_lanes(stage, tag, channels, nodes, links)
        else:
            raise ValueError(f'unknown stage kind {kind!r}')

        for c in range(channels):
            if cur[c] is None:
                entry[c] = ins[c]
            else:
                links.append({'output': cur[c], 'input': ins[c]})
            cur[c] = outs[c]

    # Channels no stage touched — and the no-stages-at-all case — still have
    # to reach the other side, so give them a passthrough.
    for c in range(channels):
        if cur[c] is None:
            name = _node(nodes, f'thru{c}', 'builtin', 'copy')
            entry[c] = f'{name}:In'
            cur[c] = f'{name}:Out'

    return {'nodes': nodes, 'links': links,
            'inputs': entry, 'outputs': cur}


# ------------------------------------------------------------------- conf --

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


def resolve_target(strip: Strip, strips=None) -> str:
    """The node this strip plays into.

    One destination is targeted directly; several are targeted through a
    combine device built for the purpose, because a filter chain has a single
    playback stream and "play on both" really means "target one sink that
    fans out".  No destination means follow the default output.
    """
    if strip.role == 'mix':
        dests = list(strip.outputs)
    else:
        by_id = {s.id: s for s in (strips if strips is not None
                                   else list_strips())}
        dests = [by_id[m].node_name for m in strip.sends if m in by_id]
    if not dests:
        return ''
    if len(dests) == 1:
        return dests[0]
    return f'pwctl.{strip.fan_id}'


def _conf(strip: Strip, strips=None) -> dict:
    capture = {
        'node.name': strip.node_name,
        'media.class': 'Audio/Sink',
        'node.description': strip.name,
        'audio.position': list(strip.positions),
    }
    playback = {
        'node.name': f'{strip.node_name}.out',
        'node.description': f'{strip.name} output',
        'node.passive': True,
        'audio.position': list(strip.positions),
    }
    target = resolve_target(strip, strips)
    if target:
        playback['target.object'] = target
        playback['node.dont-reconnect'] = False
    args = {
        'node.description': strip.name,
        'filter.graph': build_graph(strip),
        'capture.props': capture,
        'playback.props': playback,
    }
    return _base([{'name': 'libpipewire-module-filter-chain', 'args': args}])


def generate(strip: Strip, strips=None):
    ensure_dirs()
    conf = _conf(strip, strips)
    header = (f'{strip.name}\nGenerated by PipeWire Controller '
              f'(signal path {strip.role}). Do not edit by hand.')
    text = spa_json.dumps(conf, header=header)
    spa_json.loads(text)              # sanity check before writing
    system.atomic_write(strip.conf_path, text)
    save_meta(strip)


# -------------------------------------------------------------- fan-out --

def _fan_members(strip: Strip, strips=None) -> list[str]:
    if strip.role == 'mix':
        return list(strip.outputs)
    by_id = {s.id: s for s in (strips if strips is not None
                               else list_strips())}
    return [by_id[m].node_name for m in strip.sends if m in by_id]


def sync_fan(strip: Strip, strips=None) -> tuple[bool, str]:
    """Create, update or remove the combine device this strip fans out to."""
    members = _fan_members(strip, strips)
    existing = next((d for d in virtual.list_devices()
                     if d.id == strip.fan_id), None)
    if len(members) < 2:
        if existing:
            virtual.delete(existing)
        return True, ''
    if existing:
        dev = existing
        dev.members = members
        dev.positions = list(strip.positions)
        dev.enabled = strip.enabled
    else:
        dev = virtual.VirtualDevice(
            id=strip.fan_id, name=f'{strip.name} fan-out',
            kind='combine-sink', members=members,
            positions=list(strip.positions), enabled=strip.enabled,
            persistent=strip.persistent)
    return virtual.apply(dev)


# --------------------------------------------------------------- lifecycle --

def apply(strip: Strip, strips=None) -> tuple[bool, str]:
    """Regenerate config and (re)start/stop the unit to match `enabled`."""
    try:
        generate(strip, strips)
    except (spa_json.SpaJsonError, ValueError) as e:
        return False, str(e)
    ok, err = sync_fan(strip, strips)
    if not ok:
        return False, f'fan-out device: {err}'
    ensure_unit()
    if strip.enabled:
        args = ('enable', '--now', strip.unit) if strip.persistent \
            else ('start', strip.unit)
        rc, _, err = system.sysctl_user(*args)
        if rc != 0:
            return False, err.strip() or 'failed to start unit'
        rc, _, err = system.sysctl_user('restart', strip.unit, timeout=30)
        return (rc == 0), (err.strip() if rc else '')
    system.sysctl_user('disable', '--now', strip.unit)
    return True, ''


def set_enabled(strip: Strip, enabled: bool) -> tuple[bool, str]:
    strip.enabled = enabled
    save_meta(strip)
    return apply(strip)


def status(strip: Strip) -> str:
    return system.unit_state(strip.unit)


def delete(strip: Strip):
    system.sysctl_user('disable', '--now', strip.unit)
    fan = next((d for d in virtual.list_devices() if d.id == strip.fan_id),
               None)
    if fan:
        virtual.delete(fan)
    for p in (strip.conf_path, strip.meta_path):
        if p.is_file():
            p.unlink()
    # A source that fed this mix would otherwise keep a dangling send.
    if strip.role == 'mix':
        for s in sources():
            if strip.id in s.sends:
                s.sends = [m for m in s.sends if m != strip.id]
                save_meta(s)
                if s.enabled:
                    apply(s)


# ---------------------------------------------------------------- routing --

def target_edges(strips=None) -> dict[str, str]:
    """{sink we publish -> the node it feeds}, for the shared ring check."""
    strips = strips if strips is not None else list_strips()
    return {s.node_name: resolve_target(s, strips) for s in strips}


def mix_targets(strip: Strip, all_mixes: list[Strip]) -> list[Strip]:
    """The mixes a source may send to (all of them — a mix cannot ring)."""
    return [m for m in all_mixes if m.id != strip.id]


def output_targets(strip: Strip, sinks: list, edges: dict | None = None) -> list:
    """The devices a mix may output to, minus anything that leads back."""
    known = dict(edges or {})
    known.update(target_edges())
    return pick_targets(strip.node_name, sinks, known)


__all__ = [
    'Strip', 'ROLES', 'SOURCE_KINDS', 'STAGE_KINDS', 'BAND_FILTERS',
    'PATH_DIR', 'list_strips', 'sources', 'mixes', 'save_meta', 'new_strip',
    'new_stage', 'build_graph', 'generate', 'resolve_target', 'sync_fan',
    'apply', 'set_enabled', 'status', 'delete', 'target_edges',
    'mix_targets', 'output_targets', 'would_loop',
]
