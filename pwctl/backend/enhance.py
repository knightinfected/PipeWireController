"""Audio enhancements: parametric equalizer sinks and microphone cleanup.

Two kinds of enhancement, both built on native PipeWire modules and both run
as their own pwctl-chain@<id>.service instance (reusing the filter-chain unit
infrastructure), so creating, editing or removing one never interrupts the
main graph or other enhancements — exactly like virtual devices and chains.

  eq   parametric equalizer, via libpipewire-module-parametric-equalizer.
       Publishes an Audio/Sink; anything played into it (or the default
       output, if the user makes it default) comes out equalized.  The band
       list is stored in our metadata and written to an AutoEQ/APO-format
       text file that the module reads at instantiation — so editing the
       curve regenerates the file and restarts only that one unit.

  mic  microphone cleanup, via libpipewire-module-echo-cancel (WebRTC).
       Publishes a clean Audio/Source (echo + noise removed) that apps can
       record from.  In "reference from system output" mode (the default) it
       taps the default sink's monitor as the echo reference, so it works
       with no routing changes.

The parametric-equalizer module is available since PipeWire 1.0.6; the WebRTC
canceller needs the libspa-aec-webrtc SPA plugin.
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

ENH_DIR = XDG_CONFIG / 'pipewire-controller' / 'enhance'

KINDS = {
    'eq': 'Parametric equalizer',
    'mic': 'Microphone cleanup (echo / noise cancel)',
}

# AutoEQ / APO ParametricEQ.txt filter types the module understands.
FILTER_TYPES = ['PK', 'LSC', 'HSC']
FILTER_TYPE_LABELS = {
    'PK': 'Peaking (bell)',
    'LSC': 'Low shelf',
    'HSC': 'High shelf',
}

# WebRTC canceller toggles we expose, with the module's real defaults.
# (advanced ones are only surfaced while the app's Advanced switch is on.)
WEBRTC_DEFAULTS = {
    'noise_suppression': True,
    'gain_control': False,          # automatic gain control (AGC)
    'high_pass_filter': True,
    'voice_detection': True,
    'extended_filter': True,        # advanced
    'delay_agnostic': True,         # advanced
    'transient_suppression': True,  # advanced
}
WEBRTC_ADVANCED = {'extended_filter', 'delay_agnostic', 'transient_suppression'}


def _default_bands() -> list[dict]:
    """A neutral 5-band starting point (all flat) for a fresh equalizer."""
    freqs = [60, 250, 1000, 4000, 12000]
    return [{'on': True, 'type': 'PK', 'freq': f, 'gain': 0.0, 'q': 1.0}
            for f in freqs]


@dataclass
class Enhancement:
    id: str
    name: str
    kind: str = 'eq'
    enabled: bool = False
    persistent: bool = True
    params: dict = field(default_factory=dict)

    @property
    def node_name(self) -> str:
        return f'pwctl.{self.kind}.{self.id}'

    @property
    def unit(self) -> str:
        return f'pwctl-chain@{self.id}.service'

    @property
    def conf_path(self) -> Path:
        return GEN_DIR / f'{self.id}.conf'

    @property
    def eq_file(self) -> Path:
        return GEN_DIR / f'{self.id}.eq.txt'

    @property
    def meta_path(self) -> Path:
        return ENH_DIR / f'{self.id}.json'


def _slug(name: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'enh'
    return 'enh-' + s[:36]


def ensure_dirs():
    ENH_DIR.mkdir(parents=True, exist_ok=True)
    GEN_DIR.mkdir(parents=True, exist_ok=True)


def list_enhancements() -> list[Enhancement]:
    ensure_dirs()
    out = []
    known = set(Enhancement.__dataclass_fields__)
    for f in sorted(ENH_DIR.glob('*.json')):
        try:
            data = json.loads(f.read_text())
            out.append(Enhancement(
                **{k: v for k, v in data.items() if k in known}))
        except (ValueError, TypeError):
            continue
    return out


def save_meta(enh: Enhancement):
    ensure_dirs()
    system.atomic_write(enh.meta_path, json.dumps(asdict(enh), indent=2))


def new_enhancement(name: str, kind: str, **kw) -> Enhancement:
    base = _slug(name)
    eid = base
    existing = {e.id for e in list_enhancements()}
    while eid in existing:
        eid = f'{base}-{uuid.uuid4().hex[:4]}'
    params = dict(kw.pop('params', {}))
    if kind == 'eq' and 'bands' not in params:
        params['bands'] = _default_bands()
        params.setdefault('preamp', 0.0)
    if kind == 'mic':
        for k, v in WEBRTC_DEFAULTS.items():
            params.setdefault(k, v)
        params.setdefault('monitor_mode', True)
    return Enhancement(id=eid, name=name, kind=kind, params=params, **kw)


# --------------------------------------------------------- AutoEQ EQ file --
# Format (AutoEQ / Equalizer APO "ParametricEQ.txt"), one filter per line:
#   Preamp: -6.0 dB
#   Filter 1: ON PK Fc 105 Hz Gain -3.0 dB Q 0.70

_PREAMP_RE = re.compile(r'Preamp:\s*(-?\d+(?:\.\d+)?)\s*dB', re.I)
_FILTER_RE = re.compile(
    r'Filter\s*\d+:\s*(ON|OFF)\s+([A-Z]+)\s+Fc\s+(\d+(?:\.\d+)?)\s*Hz\s+'
    r'Gain\s+(-?\d+(?:\.\d+)?)\s*dB\s+Q\s+(\d+(?:\.\d+)?)', re.I)


def eq_text(params: dict) -> str:
    """Render the band list to AutoEQ/APO text the module parses."""
    preamp = float(params.get('preamp', 0.0))
    lines = [f'Preamp: {preamp:.1f} dB']
    for i, b in enumerate(params.get('bands') or [], 1):
        state = 'ON' if b.get('on', True) else 'OFF'
        ftype = str(b.get('type', 'PK')).upper()
        if ftype not in FILTER_TYPES:
            ftype = 'PK'
        lines.append(
            f'Filter {i}: {state} {ftype} '
            f'Fc {float(b.get("freq", 1000)):g} Hz '
            f'Gain {float(b.get("gain", 0.0)):g} dB '
            f'Q {float(b.get("q", 1.0)):g}')
    return '\n'.join(lines) + '\n'


def parse_eq_file(path) -> dict:
    """Parse an AutoEQ/APO ParametricEQ.txt into {preamp, bands}.

    Only PK/LSC/HSC filters are kept (the module supports no others);
    unknown filter types are silently skipped.
    """
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    preamp = 0.0
    m = _PREAMP_RE.search(text)
    if m:
        preamp = float(m.group(1))
    bands = []
    for line in text.splitlines():
        fm = _FILTER_RE.search(line)
        if not fm:
            continue
        ftype = fm.group(2).upper()
        if ftype not in FILTER_TYPES:
            continue
        bands.append({'on': fm.group(1).upper() == 'ON', 'type': ftype,
                      'freq': float(fm.group(3)), 'gain': float(fm.group(4)),
                      'q': float(fm.group(5))})
    return {'preamp': preamp, 'bands': bands}


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


def _eq_conf(enh: Enhancement) -> dict:
    pos = list(enh.params.get('positions') or ['FL', 'FR'])
    capture = {'node.name': enh.node_name, 'media.class': 'Audio/Sink',
               'node.description': enh.name}
    playback = {'node.name': f'{enh.node_name}.out',
                'node.description': f'{enh.name} output',
                'node.passive': True}
    target = enh.params.get('target')
    if target:
        playback['target.object'] = target
        playback['node.dont-reconnect'] = False
    args = {
        'equalizer.filepath': str(enh.eq_file),
        'equalizer.description': enh.name,
        'audio.channels': len(pos),
        'audio.position': pos,
        'capture.props': capture,
        'playback.props': playback,
    }
    return _base([{'name': 'libpipewire-module-parametric-equalizer',
                   'args': args}])


def _mic_conf(enh: Enhancement) -> dict:
    p = enh.params
    aec_args = {f'webrtc.{k}': bool(p.get(k, WEBRTC_DEFAULTS[k]))
                for k in WEBRTC_DEFAULTS}
    source = {'node.name': enh.node_name, 'node.description': enh.name}
    capture = {'node.name': f'{enh.node_name}.capture',
               'node.description': f'{enh.name} (raw capture)'}
    mic = p.get('source_target')
    if mic:
        capture['target.object'] = mic
        capture['node.dont-reconnect'] = False
    args = {
        'library.name': 'aec/libspa-aec-webrtc',
        'aec.args': aec_args,
        'monitor.mode': bool(p.get('monitor_mode', True)),
        'capture.props': capture,
        'source.props': source,
        'sink.props': {'node.name': f'{enh.node_name}.sink',
                       'node.description': f'{enh.name} (echo reference)'},
        'playback.props': {'node.name': f'{enh.node_name}.playback',
                           'node.description': f'{enh.name} (playback)'},
    }
    return _base([{'name': 'libpipewire-module-echo-cancel', 'args': args}])


def generate(enh: Enhancement):
    ensure_dirs()
    if enh.kind == 'eq':
        conf = _eq_conf(enh)
        system.atomic_write(enh.eq_file, eq_text(enh.params))
    elif enh.kind == 'mic':
        conf = _mic_conf(enh)
    else:
        raise ValueError(f'unknown enhancement kind {enh.kind!r}')
    header = (f'{enh.name}\nGenerated by PipeWire Controller '
              f'(enhancement: {enh.kind}). Do not edit by hand.')
    text = spa_json.dumps(conf, header=header)
    spa_json.loads(text)          # sanity check before writing
    system.atomic_write(enh.conf_path, text)
    save_meta(enh)


# --------------------------------------------------------------- lifecycle --

def apply(enh: Enhancement) -> tuple[bool, str]:
    """Regenerate config and (re)start/stop the unit to match `enabled`."""
    try:
        generate(enh)
    except (spa_json.SpaJsonError, ValueError) as e:
        return False, str(e)
    ensure_unit()
    if enh.enabled:
        args = ('enable', '--now', enh.unit) if enh.persistent \
            else ('start', enh.unit)
        rc, _, err = system.sysctl_user(*args)
        if rc != 0:
            return False, err.strip() or 'failed to start unit'
        rc, _, err = system.sysctl_user('restart', enh.unit, timeout=30)
        return (rc == 0), (err.strip() if rc else '')
    system.sysctl_user('disable', '--now', enh.unit)
    return True, ''


def set_enabled(enh: Enhancement, enabled: bool) -> tuple[bool, str]:
    enh.enabled = enabled
    save_meta(enh)
    return apply(enh)


def status(enh: Enhancement) -> str:
    return system.unit_state(enh.unit)


def delete(enh: Enhancement):
    system.sysctl_user('disable', '--now', enh.unit)
    for p in (enh.conf_path, enh.eq_file, enh.meta_path):
        if p.is_file():
            p.unlink()


def _find_node(name: str):
    from . import pw
    return next((n for n in pw.list_audio_nodes() if n.name == name), None)


# ------------------------------------------------------------- chaining --
# An equalizer's output is just a stream, so it can be pointed at another
# equalizer's sink: EQ -> EQ -> ... -> device, which is how people stack
# filters (a room-correction curve in front of a taste curve, say).  The only
# thing that must not be allowed is a ring, where audio would be fed back into
# a sink further up its own path and never reach a device.

def would_loop(enh: Enhancement, target: str,
               all_enh: list[Enhancement]) -> bool:
    """True if sending `enh` to `target` closes a ring of enhancements."""
    if not target:
        return False                        # "follow default" ends outside
    by_node = {e.node_name: e for e in all_enh}
    by_node[enh.node_name] = enh            # in case it isn't saved yet
    seen = {enh.id}
    node = target
    while node:
        nxt = by_node.get(node)
        if nxt is None:
            return False                    # ends at a device: no ring
        if nxt.id in seen:
            return True
        seen.add(nxt.id)
        node = nxt.params.get('target')
    return False


def eq_targets(enh: Enhancement | None, sinks: list,
               all_enh: list[Enhancement]) -> list:
    """The sinks `enh` may send its output to, in the given order.

    Other equalizers stay in the list (that is the whole point of chaining);
    what is dropped is this equalizer itself and any chain that would come
    back round to it.
    """
    if enh is None:
        return list(sinks)                  # nothing saved yet, nothing to ring
    return [n for n in sinks
            if n.name != enh.node_name and not would_loop(enh, n.name, all_enh)]


# ------------------------------------------------------------ live A/B --
# Comparing "with EQ" against "without EQ" must never stop the unit: stopping
# it removes the sink, and every stream playing into it is torn down (apps
# pause, some stop outright).  Instead we leave the equalizer running and move
# the streams around it — a target.object metadata change, which PipeWire
# relinks without interrupting the stream.

def eq_output_node(enh: Enhancement, dump=None):
    """The sink the equalizer is actually feeding right now.

    Ground truth is the live link from our `.out` playback stream, so this is
    right even when the target is "Follow default".  Falls back to the
    configured target, then to the default sink.
    """
    from . import pw
    dump = dump if dump is not None else pw.pw_dump()
    nodes = pw.list_audio_nodes(dump)
    out = next((s for s in pw.list_streams(dump)
                if s.props.get('node.name') == f'{enh.node_name}.out'), None)
    if out is not None and out.target_id is not None:
        node = next((n for n in nodes if n.id == out.target_id), None)
        if node is not None:
            return node
    target = enh.params.get('target')
    if target:
        node = next((n for n in nodes if n.name == target), None)
        if node is not None:
            return node
    return next((n for n in nodes if n.is_sink and n.is_default
                 and n.name != enh.node_name), None)


def _eq_streams(enh: Enhancement, dump):
    """Real app streams currently playing into this equalizer."""
    from . import pw
    nodes = pw.list_audio_nodes(dump)
    eq = next((n for n in nodes if n.name == enh.node_name), None)
    if eq is None:
        return None, []
    streams = [s for s in pw.list_streams(dump)
               if s.is_playback and s.target_id == eq.id
               and not s.props.get('node.name', '').startswith('pwctl.')]
    return eq, streams


def bypass(enh: Enhancement) -> tuple[bool, str, dict]:
    """Route everything playing into the equalizer straight to its output.

    Returns (ok, message, state); pass `state` back to `unbypass()` to put the
    streams (and the default sink, if we changed it) back.
    """
    from . import pw
    dump = pw.pw_dump()
    eq, streams = _eq_streams(enh, dump)
    if eq is None:
        return False, 'The equalizer is not running.', {}
    dest = eq_output_node(enh, dump)
    if dest is None or dest.name == enh.node_name:
        return False, 'No output device to compare against.', {}
    moved = [s.id for s in streams if pw.move_stream(s.id, dest.serial)]
    # If the EQ is the system output, new streams would keep landing in it —
    # move the default too, and remember to put it back.
    was_default = eq.is_default
    if was_default:
        pw.set_default(dest.id)
    return True, dest.description, {'streams': moved, 'was_default': was_default,
                                    'dest': dest.name}


def unbypass(enh: Enhancement, state: dict) -> tuple[bool, str]:
    """Send the streams `bypass()` moved back into the equalizer."""
    from . import pw
    dump = pw.pw_dump()
    nodes = pw.list_audio_nodes(dump)
    eq = next((n for n in nodes if n.name == enh.node_name), None)
    if eq is None:
        return False, 'The equalizer is not running.'
    for sid in state.get('streams') or []:
        pw.move_stream(sid, eq.serial)
    if state.get('was_default'):
        pw.set_default(eq.id)
    return True, ''


def make_default_output(enh: Enhancement) -> tuple[bool, str]:
    """Insert an equalizer into the active path by making it the default sink.

    An equalizer only affects audio that plays *into* it, so "use this EQ"
    means selecting it as the system output.  To avoid a feedback loop (the
    EQ's own output following the default straight back into itself) we first
    pin its output to the current hardware default when it's set to "Follow
    default", then switch the system default to the EQ sink.

    Returns (ok, target-name-or-error).
    """
    import time
    from . import pw
    if enh.kind != 'eq':
        return False, 'Only equalizers can be set as the output.'
    eq = _find_node(enh.node_name)
    if eq is None:
        return False, 'Enable the equalizer first.'
    if not enh.params.get('target'):
        cur = next((n for n in pw.list_audio_nodes()
                    if n.is_default and n.is_sink
                    and not n.name.startswith('pwctl.eq.')), None)
        if cur is None:
            return False, ('Set the equalizer’s Output device to a hardware '
                           'output first.')
        enh.params['target'] = cur.name
        ok, err = apply(enh)                 # regenerate with explicit target
        if not ok:
            return False, err
        for _ in range(20):                  # wait for the node to reappear
            time.sleep(0.25)
            eq = _find_node(enh.node_name)
            if eq is not None:
                break
        if eq is None:
            return False, 'Equalizer did not come back after routing.'
    if not pw.set_default(eq.id):
        return False, 'Could not set the default output.'
    return True, enh.params.get('target', '')
