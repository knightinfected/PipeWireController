"""Audio enhancements: parametric equalizer sinks and microphone cleanup.

Two kinds of enhancement, both built on native PipeWire modules and both run
as their own pwctl-chain@<id>.service instance (reusing the filter-chain unit
infrastructure), so creating, editing or removing one never interrupts the
main graph or other enhancements — exactly like virtual devices and chains.

  eq   parametric equalizer.  Publishes an Audio/Sink; anything played
       into it (or the default output, if the user makes it default) comes
       out equalized.  The curve is stored in our metadata (`eqcurve` is its
       model) and run by one of two engines:

       biquad    a filter-chain of PipeWire's bq_* builtins, one node per
                 band behind a `linear` preamp.  Its controls are live, so a
                 curve edit is a control write (`eqlive`), not a restart.
       param_eq  libpipewire-module-parametric-equalizer reading an
                 AutoEQ/APO text file at start.  What 0.6.1 and earlier
                 generate, and kept for a stored curve the biquad controls
                 cannot hold (a band above +20 dB or Q above 10).

       Both measured to produce the same response, 0.0000 dB apart
       (docs.local/probes/2026-10-27-eq-engine).  The AutoEQ text file is
       written for both: it is the engine's input for param_eq, and an
       export of the curve for biquad.

  mic  microphone cleanup, via libpipewire-module-echo-cancel (WebRTC).
       Publishes a clean Audio/Source (echo + noise removed) that apps can
       record from.  In "reference from system output" mode (the default) it
       taps the default sink's monitor as the echo reference, so it works
       with no routing changes.

The parametric-equalizer module is available since PipeWire 1.2.0 (it is not
in 1.0.x: docs.local/probes/2026-10-20-mint22-checkout/); the bq_* builtins
are older than that.  The WebRTC canceller needs the libspa-aec-webrtc SPA
plugin.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, asdict, replace
from pathlib import Path

from .. import spa_json
from . import eqcurve, eqlive, system
from .chains import GEN_DIR, ensure_unit, pick_targets
from .chains import would_loop as chain_would_loop
from .config import XDG_CONFIG

ENH_DIR = XDG_CONFIG / 'pipewire-controller' / 'enhance'

KINDS = {
    'eq': 'Parametric equalizer',
    'mic': 'Microphone cleanup (echo / noise cancel)',
}

# The filter types the AutoEQ-file engine understands (eqcurve.TYPES has
# every type the biquad engine runs).  Kept for callers of the old names.
FILTER_TYPES = [t.code for t in eqcurve.TYPES.values() if t.file_ok]
FILTER_TYPE_LABELS = {t.code: t.long for t in eqcurve.TYPES.values()
                      if t.file_ok}

ENGINE_BIQUAD = 'biquad'
ENGINE_FILE = 'param_eq'

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
    return eqcurve.default_curve().to_params()['bands']


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

    @property
    def live_path(self) -> Path:
        """Present while the running equalizer is not playing its saved
        curve (an editor preview, a bypass).  `reconcile()` reads it."""
        return ENH_DIR / f'{self.id}.live'


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


# Where an equalizer's curve came from, stored when it is created as
# params['origin'] = {'kind': ..., 'name': ...}.  Equalizers made before
# it existed have none, and nothing is guessed for them.
ORIGINS = {'made': 'Made here', 'file': 'Imported', 'preset': 'Preset',
           'paste': 'Pasted', 'copy': 'Copy'}


def origin_of(enh: Enhancement) -> tuple[str, str] | None:
    """(what, detail) for the page, e.g. ('Imported', 'HD 600.txt')."""
    origin = enh.params.get('origin')
    if not isinstance(origin, dict) or origin.get('kind') not in ORIGINS:
        return None
    return ORIGINS[origin['kind']], str(origin.get('name') or '')


def duplicate(enh: Enhancement) -> Enhancement:
    """A stopped copy of an equalizer (its curve, output and layout), to try
    a variation on without touching the original."""
    params = json.loads(json.dumps(enh.params))
    if enh.kind == 'eq':
        params['origin'] = {'kind': 'copy', 'name': enh.name}
    copy = new_enhancement(f'{enh.name} (copy)', enh.kind, params=params)
    copy.enabled = False
    generate(copy)
    return copy


# --------------------------------------------------------- AutoEQ EQ file --
# Format (AutoEQ / Equalizer APO "ParametricEQ.txt"), one filter per line:
#   Preamp: -6.0 dB
#   Filter 1: ON PK Fc 105 Hz Gain -3.0 dB Q 0.70

def curve_of(enh: Enhancement) -> eqcurve.Curve:
    return eqcurve.Curve.from_params(enh.params)


def eq_text(params: dict) -> str:
    """Render the band list to AutoEQ/APO text (param_eq reads this)."""
    return eqcurve.to_text(eqcurve.Curve.from_params(params))


def parse_eq_file(path) -> dict:
    """Parse an AutoEQ/APO ParametricEQ.txt into {preamp, bands, notes}.

    `notes` says, in plain words, what was converted or left out.
    """
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    parsed = eqcurve.parse_text(text)
    return {**parsed.curve.to_params(), 'notes': parsed.notes}


def eq_engine(enh: Enhancement) -> str:
    """Which engine runs this equalizer's curve.

    Biquad whenever its controls can hold the curve -- every curve the
    editor makes.  A curve stored before the caps existed that goes past
    them stays on the file engine, so upgrading never changes its sound.
    `params['engine']` can pin one (a way out if an engine misbehaves).
    """
    curve = curve_of(enh)
    pinned = enh.params.get('engine')
    if pinned == ENGINE_FILE and curve.file_ok():
        return ENGINE_FILE
    if pinned == ENGINE_BIQUAD or curve.fits_live() or not curve.file_ok():
        return ENGINE_BIQUAD
    return ENGINE_FILE


def _db_to_mult(db: float) -> float:
    return 10 ** (db / 20.0)


def band_controls(band: eqcurve.Band, index: int) -> dict:
    """The `b<index>:<Port>` controls one band is written as."""
    name = f'b{index}'
    ctl = {'Freq': band.freq, 'Q': band.q}
    if band.kind.has_gain:
        ctl['Gain'] = band.gain
    if not band.on:
        ctl.update(dict(band.kind.identity))
    return {f'{name}:{k}': float(v) for k, v in ctl.items()}


def eq_controls(curve: eqcurve.Curve, bypass: bool = False,
                bypass_db: float = 0.0) -> dict:
    """Every live control of the biquad graph for this curve.

    `bypass=True` gives the same graph's exact pass-through: every band at
    its identity value (see `eqcurve.TYPES`) and the preamp at `bypass_db`,
    0 for unity, or the curve's own loudness for a level-matched compare.
    """
    if bypass:
        db = min(max(bypass_db, eqcurve.PREAMP_MIN), eqcurve.PREAMP_MAX)
        out = {'pre:Mult': 1.0 if abs(db) < 1e-9 else _db_to_mult(db)}
    else:
        out = {'pre:Mult': _db_to_mult(curve.preamp)}
    for i, b in enumerate(curve.bands, 1):
        out.update(band_controls(replace(b, on=False) if bypass
                                 else b, i))
    return out


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


def _eq_streams_props(enh: Enhancement):
    """capture.props / playback.props, the same for both engines, so apps,
    rules and pages see one node whichever engine runs it."""
    capture = {'node.name': enh.node_name, 'media.class': 'Audio/Sink',
               'node.description': enh.name}
    playback = {'node.name': f'{enh.node_name}.out',
                'node.description': f'{enh.name} output',
                'node.passive': True}
    target = enh.params.get('target')
    if target:
        playback['target.object'] = target
        playback['node.dont-reconnect'] = False
    return capture, playback


def _eq_conf_file(enh: Enhancement) -> dict:
    pos = list(enh.params.get('positions') or ['FL', 'FR'])
    capture, playback = _eq_streams_props(enh)
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


def _eq_conf_biquad(enh: Enhancement) -> dict:
    """preamp -> b1 -> b2 -> ... as one 1-in/1-out graph.

    filter-chain copies a 1-in/1-out graph once per channel and a control
    write reaches every copy, so `b3:Gain` is one write at any layout.  A
    band switched off stays in the graph at its pass-through value: turning
    it back on is then a write too.
    """
    pos = list(enh.params.get('positions') or ['FL', 'FR'])
    capture, playback = _eq_streams_props(enh)
    curve = curve_of(enh).clamped()
    controls = eq_controls(curve)
    nodes = [{'type': 'builtin', 'name': 'pre', 'label': 'linear',
              'control': {'Mult': controls['pre:Mult']}}]
    links = []
    prev = 'pre'
    for i, band in enumerate(curve.bands, 1):
        name = f'b{i}'
        prefix = f'{name}:'
        nodes.append({'type': 'builtin', 'name': name,
                      'label': band.kind.builtin,
                      'control': {k[len(prefix):]: v
                                  for k, v in controls.items()
                                  if k.startswith(prefix)}})
        links.append({'output': f'{prev}:Out', 'input': f'{name}:In'})
        prev = name
    graph = {'nodes': nodes, 'inputs': ['pre:In'], 'outputs': [f'{prev}:Out']}
    if links:
        graph['links'] = links
    args = {
        'node.description': enh.name,
        'media.name': enh.name,
        'audio.channels': len(pos),
        'audio.position': pos,
        'filter.graph': graph,
        'capture.props': capture,
        'playback.props': playback,
    }
    return _base([{'name': 'libpipewire-module-filter-chain', 'args': args}])


def _eq_conf(enh: Enhancement) -> dict:
    if eq_engine(enh) == ENGINE_FILE:
        return _eq_conf_file(enh)
    return _eq_conf_biquad(enh)


def conf_shape(conf: dict):
    """A conf with every live control value taken out.

    Two biquad confs with the same shape differ only in values a running
    graph can be written, so moving from one to the other needs no restart.
    Anything else -- a band added, a type, the output, the name, the
    layout, the engine -- is in the shape and does.  None when the conf has
    no live controls at all (the file engine).
    """
    if not isinstance(conf, dict):
        return None
    shape = json.loads(json.dumps(conf))
    found = False
    for module in shape.get('context.modules') or []:
        if module.get('name') != 'libpipewire-module-filter-chain':
            continue
        graph = (module.get('args') or {}).get('filter.graph') or {}
        for node in graph.get('nodes') or []:
            node.pop('control', None)
            found = True
    return shape if found else None


def running_conf(enh: Enhancement) -> dict | None:
    """The conf on disk: what the unit runs, or will run when it starts."""
    try:
        return spa_json.loads(enh.conf_path.read_text())
    except (OSError, spa_json.SpaJsonError, ValueError):
        return None


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


def build_conf(enh: Enhancement) -> dict:
    if enh.kind == 'eq':
        return _eq_conf(enh)
    if enh.kind == 'mic':
        return _mic_conf(enh)
    raise ValueError(f'unknown enhancement kind {enh.kind!r}')


def generate(enh: Enhancement, save: bool = True) -> dict:
    """Write the conf (and an equalizer's AutoEQ file); returns the conf.

    `save=False` leaves the stored meta alone: an editor preview writes the
    conf the unit is about to restart with, while the saved curve stays
    what Cancel goes back to.
    """
    ensure_dirs()
    conf = build_conf(enh)
    header = (f'{enh.name}\nGenerated by PipeWire Controller '
              f'(enhancement: {enh.kind}). Do not edit by hand.')
    text = spa_json.dumps(conf, header=header)
    spa_json.loads(text)          # sanity check before writing
    if enh.kind == 'eq':
        system.atomic_write(enh.eq_file, eq_text(enh.params))
    system.atomic_write(enh.conf_path, text)
    if save:
        save_meta(enh)
    return conf


def same_shape(a: dict | None, b: dict | None) -> bool:
    """Whether a running conf `a` can become `b` by control writes alone."""
    if a is None or b is None:
        return False
    norm = spa_json.loads(spa_json.dumps(b))
    sa, sb = conf_shape(a), conf_shape(norm)
    return sa is not None and sa == sb


def _active(enh: Enhancement) -> bool:
    return system.unit_state(enh.unit) in ('active', 'activating',
                                           'reloading')


# --------------------------------------------------------------- lifecycle --

def apply(enh: Enhancement, live: bool = True) -> tuple[bool, str]:
    """Regenerate config and make the unit match `enabled`.

    When the unit already runs a biquad graph of the same shape, the new
    curve is written into it and nothing restarts, so the sound never
    gaps.  Otherwise it restarts, and the apps that had been sent into the
    equalizer are sent back once it is up (`restart()`).
    """
    before = running_conf(enh)
    try:
        conf = generate(enh)
    except (spa_json.SpaJsonError, ValueError) as e:
        return False, str(e)
    ensure_unit()
    if not enh.enabled:
        system.sysctl_user('disable', '--now', enh.unit)
        clear_live(enh)
        return True, ''
    was_running = _active(enh)
    if (live and enh.kind == 'eq' and was_running
            and same_shape(before, conf) and write_curve(enh)):
        clear_live(enh)
        return True, ''
    args = ('enable', '--now', enh.unit) if enh.persistent \
        else ('start', enh.unit)
    rc, _, err = system.sysctl_user(*args)
    if rc != 0:
        return False, err.strip() or 'failed to start unit'
    ok, err = restart(enh) if was_running else (True, '')
    if ok:
        clear_live(enh)
    return ok, err


def set_enabled(enh: Enhancement, enabled: bool) -> tuple[bool, str]:
    enh.enabled = enabled
    save_meta(enh)
    return apply(enh)


def status(enh: Enhancement) -> str:
    return system.unit_state(enh.unit)


def delete(enh: Enhancement):
    system.sysctl_user('disable', '--now', enh.unit)
    for p in (enh.conf_path, enh.eq_file, enh.meta_path, enh.live_path):
        if p.is_file():
            p.unlink()


# ------------------------------------------------- restarts keep the apps --
# Measured (docs.local/probes/2026-10-27-eq-engine, result 7): an app that
# `pw.move_stream` sent into an equalizer falls to the default output when
# the equalizer's process stops, and stays there once it is back, because
# its `target.object` metadata still names the old node's serial.  So a
# restart reads who was sent here first, and sends them back after.  Apps
# that only followed the default output are left to WirePlumber, which
# moves them back by name; pinning them here would stop them following it.

def _sent_here(dump, eq) -> list[int]:
    """Streams whose `default` metadata target names this node."""
    names = {str(eq.serial), str(eq.id), eq.name}
    out = []
    for obj in dump or []:
        if obj.get('type') != 'PipeWire:Interface:Metadata':
            continue
        if (obj.get('props') or {}).get('metadata.name') != 'default':
            continue
        for entry in obj.get('metadata') or []:
            if entry.get('key') not in ('target.object', 'target.node'):
                continue
            value = entry.get('value')
            if isinstance(value, dict):
                value = value.get('name', '')
            if str(value) in names and isinstance(entry.get('subject'), int):
                out.append(entry['subject'])
    return out


def wait_for_node(name: str, not_serial=None, timeout: float = 5.0):
    """The node called `name`, once it is there (and new, if `not_serial`)."""
    import time
    from . import pw
    end = time.monotonic() + timeout
    while True:
        node = next((n for n in pw.list_audio_nodes(pw.pw_dump())
                     if n.name == name
                     and (not_serial is None or n.serial != not_serial)),
                    None)
        if node is not None or time.monotonic() >= end:
            return node
        time.sleep(0.1)


def restart(enh: Enhancement, wait: float = 5.0) -> tuple[bool, str]:
    from . import pw
    dump = pw.pw_dump()
    eq = next((n for n in pw.list_audio_nodes(dump)
               if n.name == enh.node_name), None)
    keep = _sent_here(dump, eq) if eq is not None else []
    rc, _, err = system.sysctl_user('restart', enh.unit, timeout=30)
    if rc != 0:
        return False, err.strip() or 'failed to restart'
    if keep:
        node = wait_for_node(enh.node_name,
                             eq.serial if eq is not None else None, wait)
        if node is not None:
            live_ids = {s.id for s in pw.list_streams(pw.pw_dump())}
            for sid in keep:
                if sid in live_ids:
                    pw.move_stream(sid, node.serial)
    return True, ''


# ----------------------------------------------------------- live curves --
# A biquad-engine equalizer's curve can be written while it plays.  The
# stored meta is always the saved curve; whatever plays instead (an editor
# preview, a bypass) is announced by `live_path`, so a crash in the middle
# is put right the next time the app starts (`reconcile()`).

def write_curve(enh: Enhancement, curve: eqcurve.Curve | None = None,
                bypass: bool = False, node_id: int | None = None) -> bool:
    """Write a curve (the saved one by default) into the running graph."""
    if node_id is None:
        node = _find_node(enh.node_name)
        if node is None:
            return False
        node_id = node.id
    curve = (curve or curve_of(enh)).clamped()
    return eqlive.write_now(node_id, eq_controls(curve, bypass=bypass))


def is_live(enh: Enhancement, dump=None) -> bool:
    """The running equalizer has controls (a biquad graph is playing)."""
    from . import pw
    return eqlive.read_controls(dump if dump is not None else pw.pw_dump(),
                                enh.node_name) is not None


def mark_live(enh: Enhancement, why: str):
    try:
        ensure_dirs()
        system.atomic_write(enh.live_path, why + '\n')
    except OSError:
        pass


def clear_live(enh: Enhancement):
    try:
        enh.live_path.unlink()
    except OSError:
        pass


def preview(enh: Enhancement, curve: eqcurve.Curve) -> tuple[bool, str]:
    """Restart the equalizer on `curve` without saving it -- for a change
    a control write cannot make (a band added, removed or retyped)."""
    trial = Enhancement(**{**asdict(enh), 'params': {
        **enh.params, **curve.clamped().to_params()}})
    mark_live(enh, 'preview')
    try:
        generate(trial, save=False)
    except (spa_json.SpaJsonError, ValueError) as e:
        return False, str(e)
    if not _active(enh):
        return True, ''
    return restart(enh)


def reconcile(enh: Enhancement) -> str:
    """Put an equalizer back on its saved curve if a preview or a bypass was
    left playing (the app closed or crashed mid-way).  '' when there was
    nothing to do, else what happened."""
    if not enh.live_path.exists():
        return ''
    if enh.kind != 'eq' or not enh.enabled:
        clear_live(enh)
        return ''
    ok, err = apply(enh)
    return 'restored' if ok else (err or 'could not restore')


def reconcile_all() -> list[tuple[str, str]]:
    return [(e.name, r) for e in list_enhancements()
            if (r := reconcile(e))]


def _find_node(name: str):
    from . import pw
    return next((n for n in pw.list_audio_nodes() if n.name == name), None)


# ------------------------------------------------------------- chaining --
# An equalizer's output is just a stream, so it can be pointed at another
# equalizer's sink — or at a plugin rack, or at a virtual device.  The ring
# check lives in `chains` because a ring can run through objects of every
# kind (EQ -> rack -> EQ), so it cannot be decided from enhancements alone.

def target_edges(items: list[Enhancement]) -> dict[str, str]:
    """{sink we publish -> the node it currently feeds} for these items."""
    return {e.node_name: e.params.get('target', '') for e in items}


def would_loop(enh: Enhancement, target: str,
               all_enh: list[Enhancement]) -> bool:
    """True if sending `enh` to `target` closes a ring of enhancements."""
    return chain_would_loop(enh.node_name, target, target_edges(all_enh))


def eq_targets(enh: Enhancement | None, sinks: list,
               all_enh: list[Enhancement], edges: dict | None = None) -> list:
    """The sinks `enh` may send its output to, in the given order.

    Other equalizers stay in the list (that is the whole point of chaining);
    what is dropped is this equalizer itself and any chain that would come
    back round to it.  Pass `edges` from `chains.target_edges()` as well to
    catch rings that run through plugin racks.
    """
    known = dict(edges or {})
    known.update(target_edges(all_enh))
    return pick_targets(enh.node_name if enh else '', sinks, known)


# ---------------------------------------------------------- the output --
# Comparing with and without the equalizer is the editing session's job
# (eqsession: Compare writes pass-through controls, never stops the unit or
# moves a stream).  What is left here is where the equalizer's sound goes.

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
