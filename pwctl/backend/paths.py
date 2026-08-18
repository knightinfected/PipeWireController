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
breaks it.  Lanes are also what makes a **crossover** stage possible: it reads
some lanes and writes others, so a strip can take stereo in and put the low
band on lanes of its own.  A strip that does that declares `out_positions`,
and then its sink and its playback stream no longer have the same layout.

Rings are impossible between a source and a mix (a mix only ever outputs to a
device), but a mix output is a name the user picked, so it is still checked
with the same walker the chains and equalizers use.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .. import spa_json
from . import pw, system, virtual
from .chains import GEN_DIR, ensure_unit, pick_targets, would_loop
from .config import XDG_CONFIG

PATH_DIR = XDG_CONFIG / 'pipewire-controller' / 'paths'

ROLES = ('source', 'mix', 'xover')

# A crossover is not a stage inside one strip but a thing of its own, sitting
# between what plays and what it comes out of.  It has to be, because its
# whole job is to send *different frequencies to different destinations*: as a
# stage it could only ever filter the one path it was standing in, so a
# two-way split meant building the path twice and keeping the two halves in
# step by hand.
#
# One crossover owns a list of bands.  A band is a frequency range and the
# destinations that range is played on.  Two bands can name the same
# destination (they are summed) and one band can name several.
#
# It generates a single unit holding one filter-chain per destination: the
# destination it is inserted into is served by the insert itself, and every
# other destination by a tap reading the insert's input.  That keeps the
# endpoints exactly as they were — the crossover is intermediate, never a
# replacement for either end.
DEFAULT_BAND_SLOPE = 24

# How a strip shows itself to the rest of the session.
#
#   sink    it publishes a selectable output of its own, and audio reaches it
#           because something was pointed at it.  The original model, and
#           still the right one for a bus other apps have to *choose* — the
#           capture sink OBS records from.
#
#   insert  it publishes nothing selectable and attaches itself to a device
#           instead: WirePlumber links every stream heading for that device
#           through the strip first (`filter.smart`).  The device stays the
#           output everyone selects, so nothing new appears in anyone's list
#           and no app has to be repointed.
#
#   tap     it takes a copy of what another strip is being fed and plays that
#           somewhere else.  A capture stream and a playback stream, neither
#           of them a sink — the only way to get one band of a crossover onto
#           a *second* card without publishing an output to carry it.
#
# `sink` is the dataclass default so that strips written before this existed
# keep the behaviour they were built with; `new_strip` prefers `insert`.
MODES = ('sink', 'insert', 'tap')
SOURCE_KINDS = {
    'app': 'An application',
    'mic': 'A microphone',
    'everything': 'Everything (this strip becomes the default output)',
}
STAGE_KINDS = ('eq', 'effect', 'convolver', 'xover')

# Crossover bands.  A band is a filter *and* a route: which lanes it listens
# to and which lanes it comes out on.  Leaving the route empty filters in
# place, which is what a plain "cut everything under 80 Hz" wants; setting it
# is what splits one input into several speaker feeds.
XOVER_MODES = {
    'lowpass': 'Low band (everything below the crossover)',
    'highpass': 'High band (everything above the crossover)',
    'bandpass': 'Middle band (between the two crossovers)',
}

# Linkwitz-Riley slopes, as the Q of each cascaded biquad section.  LR is the
# crossover filter: two complementary LR bands sum back to a flat response,
# which Butterworth sections on their own do not.  LR2 is one critically
# damped section, LR4 two Butterworth ones, LR8 a cascade of two 4th-order
# Butterworths.
XOVER_SLOPES = {
    12: (0.5,),
    24: (0.7071, 0.7071),
    48: (0.5412, 1.3066, 0.5412, 1.3066),
}
DEFAULT_SLOPE = 24
# Delay headroom compiled into a band's delay node; the control can be moved
# at runtime, the buffer size cannot.
MAX_DELAY_S = 0.2

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
    role: str = 'source'                      # see ROLES
    kind: str = 'app'                         # sources only, see SOURCE_KINDS
    bands: list = field(default_factory=list)  # xover only, see new_band
    insert_into: str = ''                     # xover: device it sits in front
    #                                           of; '' follows the default
    mode: str = 'sink'                        # see MODES
    tap_source: str = ''                      # tap mode: node.name to copy
    positions: list = field(default_factory=lambda: list(DEFAULT_POSITIONS))
    out_positions: list = field(default_factory=list)  # empty: same as above
    stages: list = field(default_factory=list)     # stage dicts, see _stage_*
    sends: list = field(default_factory=list)      # source -> mix ids
    outputs: list = field(default_factory=list)    # mix -> device node.names
    match: dict = field(default_factory=dict)      # source -> app match rule
    enabled: bool = False
    persistent: bool = True
    order: int = 0                            # position within its own column

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
    def link_group(self) -> str:
        """Ties the capture and playback halves together as one filter.

        WirePlumber needs this to see the two nodes as a single object it can
        splice into a link, rather than as a sink and an unrelated stream.
        """
        return f'pwctl-{self.id}'

    @property
    def fan_id(self) -> str:
        """Id of the combine device built when this strip feeds several."""
        return f'{self.id}-fan'

    @property
    def channels(self) -> int:
        return len(self.positions)

    @property
    def out_layout(self) -> list:
        """What comes out.  Wider than `positions` once a band is routed to
        lanes of its own — a stereo sink feeding a bi-amped 4-way output."""
        return list(self.out_positions or self.positions)

    @property
    def out_channels(self) -> int:
        return len(self.out_layout)

    def active_stages(self) -> list[dict]:
        return [s for s in self.stages if not s.get('bypass')]

    def insert_device(self) -> str:
        """The device an inserted strip attaches to; '' means follow default.

        A smart filter with no target follows whatever the default output is,
        which is exactly what "everything, corrected" means — and it keeps
        following it when the user changes their output.
        """
        return self.outputs[0] if self.outputs else ''

    def can_insert(self) -> bool:
        """Whether this strip could attach itself to a device.

        Inserting means becoming part of one device's path, so it needs a
        single destination and no second life as a sink other things were
        pointed at: a mix feeding two devices has to fan out through a sink,
        and a source capturing one app has to be a sink for that app to play
        into.
        """
        if self.role == 'mix':
            return len(self.outputs) <= 1
        return self.kind == 'everything' and not self.sends


# ------------------------------------------------------------------ store --

def _slug(name: str, role: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or role
    return f'p{role[:3]}-{s[:34]}'


def ensure_dirs():
    PATH_DIR.mkdir(parents=True, exist_ok=True)
    GEN_DIR.mkdir(parents=True, exist_ok=True)


def list_strips() -> list[Strip]:
    """Every strip, in the order the board shows them.

    `order` is per column and only ever set by the page.  Strips written
    before it existed all read back as 0, and the sort is stable, so they keep
    the filename order they have always had until something is dragged.
    """
    ensure_dirs()
    out = []
    known = set(Strip.__dataclass_fields__)
    for f in sorted(PATH_DIR.glob('*.json')):
        try:
            data = json.loads(f.read_text())
            strip = Strip(**{k: v for k, v in data.items() if k in known})
        except (ValueError, TypeError):
            continue
        # Also on the way in, so a crossover written before the rows were
        # ordered reads back in spectrum order instead of waiting for its
        # next edit to straighten itself out.
        if strip.role == 'xover':
            sort_bands(strip)
        out.append(strip)
    out.sort(key=lambda s: s.order)
    return out


def sources(strips=None) -> list[Strip]:
    return [s for s in (strips if strips is not None else list_strips())
            if s.role == 'source']


def crossovers(strips=None) -> list[Strip]:
    return [s for s in (strips if strips is not None else list_strips())
            if s.role == 'xover']


def mixes(strips=None) -> list[Strip]:
    return [s for s in (strips if strips is not None else list_strips())
            if s.role == 'mix']


def sort_bands(strip: Strip):
    """Put the rows in the order the spectrum runs, lowest band first.

    A crossover is read as a picture of the spectrum, so the rows have to
    follow it: a band added later but sitting between two existing ones
    belongs between them, not at the bottom of the list.  Sorting on save
    rather than only on screen means the stored file, the generated config
    and the card all agree.

    An edge of 0 means "no limit on that side", so it is the bottom of the
    range when it is the low edge and the top of it when it is the high one —
    which is why the high edge cannot simply be compared as a number.
    """
    strip.bands.sort(key=lambda b: (float(b.get('lo') or 0.0),
                                    float(b.get('hi') or 0.0) or float('inf')))


def save_meta(strip: Strip):
    ensure_dirs()
    if strip.role == 'xover':
        sort_bands(strip)
    system.atomic_write(strip.meta_path, json.dumps(asdict(strip), indent=2))


def new_strip(name: str, role: str, **kw) -> Strip:
    if role not in ROLES:
        raise ValueError(f'unknown role {role!r}')
    base = _slug(name, role)
    sid = base
    existing = list_strips()
    taken = {s.id for s in existing}
    while sid in taken:
        sid = f'{base}-{uuid.uuid4().hex[:4]}'
    # A new strip belongs at the bottom of its column, not the top.
    kw.setdefault('order', max((s.order for s in existing if s.role == role),
                               default=-1) + 1)
    if role == 'xover' and 'bands' not in kw:
        # A crossover with no bands splits nothing.  Two bands meeting at
        # 80 Hz is the split almost everyone wants first, and both rows are
        # visible straight away — the destinations are the only blanks left.
        kw['bands'] = [new_band('Low', hi=80.0), new_band('High', lo=80.0)]
    strip = Strip(id=sid, name=name, role=role, **kw)
    # Insert wherever it is possible: a new strip should disappear into the
    # path it is correcting rather than turn up as one more thing to choose.
    if 'mode' not in kw and insertable(strip, existing):
        strip.mode = 'insert'
    return strip


def insertable(strip: Strip, strips=None) -> bool:
    """Whether `strip` may attach itself to a device instead of publishing.

    On top of the shape the strip itself knows about, a mix something *sends*
    to has to stay a sink: a send is an explicit link to a named node, and
    the name has to keep meaning something selectable.
    """
    if strip.mode == 'tap' or not strip.can_insert():
        return False
    # An insert is a `filter.smart` one, and that is WirePlumber 0.5 and later.
    # On 0.4 the pair is still built but never attaches to its target: the
    # strip does nothing at all while the app claims it is in the path, which
    # is the kind of silence nobody thinks to go looking for.  Publish a sink
    # there instead — audible, selectable, and what the strip did before.
    if not pw.smart_filters_supported():
        return False
    if strip.role == 'mix':
        others = strips if strips is not None else list_strips()
        if any(strip.id in s.sends for s in others if s.role == 'source'):
            return False
    return True


def new_stage(kind: str, name: str = '', **params) -> dict:
    """A stage dict.  Kept as plain data so it round-trips through JSON."""
    if kind not in STAGE_KINDS:
        raise ValueError(f'unknown stage kind {kind!r}')
    if kind == 'xover':
        # A fresh band has to be buildable before it is edited, so it starts
        # as a plain 80 Hz low band across the whole strip.
        params = {'mode': 'lowpass', 'freq': 80.0, 'freq_hi': 2000.0,
                  'slope': DEFAULT_SLOPE, 'gain': 0.0, 'delay': 0.0,
                  'invert': False, 'channels': [], 'route': [], **params}
    return {'id': uuid.uuid4().hex[:8], 'kind': kind,
            'name': name or kind, 'bypass': False, 'params': dict(params)}


def new_band(name: str = '', lo: float = 0.0, hi: float = 0.0,
             outputs=None, **kw) -> dict:
    """One band of a crossover: a frequency range and where it is played.

    `lo` and `hi` are the edges in Hz.  Zero means "no edge on that side", so
    a subwoofer band is 0 - 80 and the band above it is 80 - 0; that way a
    two-way split reads as two rows that meet at one number, and nothing has
    to know about "lowpass" and "highpass" as separate ideas.
    """
    return {'id': uuid.uuid4().hex[:8], 'name': name or 'Band',
            'lo': float(lo), 'hi': float(hi),
            'slope': int(kw.get('slope', DEFAULT_BAND_SLOPE)),
            'gain': float(kw.get('gain', 0.0)),
            'delay': float(kw.get('delay', 0.0)),
            'invert': bool(kw.get('invert', False)),
            'mute': bool(kw.get('mute', False)),
            'outputs': list(outputs or [])}


def band_label(band: dict) -> str:
    """The range as a person would say it — '80 Hz and below', '80 Hz - 2 kHz'."""
    def hz(v):
        return f'{v / 1000:g} kHz' if v >= 1000 else f'{v:g} Hz'
    lo, hi = float(band.get('lo') or 0), float(band.get('hi') or 0)
    if lo and hi:
        return f'{hz(lo)} - {hz(hi)}'
    if hi:
        return f'{hz(hi)} and below'
    if lo:
        return f'{hz(lo)} and above'
    return 'Everything'


def band_destinations(strip: Strip) -> list[str]:
    """Every destination this crossover feeds, in the order bands name them."""
    out: list[str] = []
    for band in strip.bands:
        if band.get('mute'):
            continue
        for dev in band.get('outputs') or []:
            if dev not in out:
                out.append(dev)
    return out


def clone_stage(stage: dict, rename: bool = True) -> dict:
    """A copy of `stage` with an identity of its own.

    Ids have to be fresh: the page finds a stage by id when a chip is clicked
    or dropped, so two stages sharing one would be the same stage twice.
    """
    out = copy.deepcopy(stage)
    out['id'] = uuid.uuid4().hex[:8]
    if rename:
        out['name'] = f"{out.get('name') or out.get('kind') or 'stage'} copy"
    return out


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


def _xover_sections(params: dict) -> list[tuple[str, float, float]]:
    """The biquad sections one band is made of, as (label, freq, Q)."""
    mode = str(params.get('mode') or 'lowpass')
    if mode not in XOVER_MODES:
        raise ValueError(f'unknown crossover mode {mode!r}')
    slope = int(params.get('slope') or DEFAULT_SLOPE)
    qs = XOVER_SLOPES.get(slope)
    if qs is None:
        raise ValueError(f'unknown crossover slope {slope}')
    lo = float(params.get('freq') or 80.0)
    if mode == 'lowpass':
        return [('bq_lowpass', lo, q) for q in qs]
    if mode == 'highpass':
        return [('bq_highpass', lo, q) for q in qs]
    hi = float(params.get('freq_hi') or 0.0)
    if hi <= lo:
        raise ValueError('the upper crossover has to sit above the lower one')
    return ([('bq_highpass', lo, q) for q in qs]
            + [('bq_lowpass', hi, q) for q in qs])


def _xover_lane(stage: dict, tag: str, nodes: list,
                links: list) -> tuple[str, str]:
    """One copy of a band: its filter cascade, then delay and trim.

    Delay and polarity are here because a crossover is only half a filter
    problem — the other half is that the drivers it feeds are rarely the same
    distance away or wired the same way round.
    """
    p = stage.get('params') or {}
    chain: list[str] = []
    for i, (label, freq, q) in enumerate(_xover_sections(p)):
        chain.append(_node(nodes, f'{tag}f{i}', 'builtin', label,
                           control={'Freq': freq, 'Q': q}))
    delay_s = float(p.get('delay') or 0.0) / 1000.0
    if delay_s > 0:
        chain.append(_node(nodes, f'{tag}dly', 'builtin', 'delay',
                           config={'max-delay': MAX_DELAY_S},
                           control={'Delay (s)': min(delay_s, MAX_DELAY_S)}))
    mult = _preamp_mult(p.get('gain') or 0.0)
    if p.get('invert'):
        mult = -mult
    if abs(mult - 1.0) > 1e-9:
        chain.append(_node(nodes, f'{tag}trim', 'builtin', 'linear',
                           control={'Mult': round(mult, 6), 'Add': 0.0}))
    if not chain:
        chain.append(_node(nodes, f'{tag}thru', 'builtin', 'copy'))
    for a, b in zip(chain, chain[1:]):
        links.append({'output': f'{a}:Out', 'input': f'{b}:In'})
    return f'{chain[0]}:In', f'{chain[-1]}:Out'


def _lanes_for(names, layout: list, fallback) -> list[int]:
    """Position names -> lane indices, ignoring what this layout hasn't got.

    A strip re-laid out from 5.1 to stereo keeps its stages; the bands that
    named a lane which no longer exists fall back to the whole strip rather
    than refusing to build.
    """
    idx = [layout.index(n) for n in names if n in layout]
    return idx or list(fallback)


def _apply_xover(stage: dict, tag: str, strip: Strip, cur: list,
                 nodes: list, links: list):
    """Filter some lanes into some lanes, in place unless routed elsewhere."""
    p = stage.get('params') or {}
    layout = strip.out_layout
    read = _lanes_for(p.get('channels') or [], layout, range(len(cur)))
    dest = _lanes_for(p.get('route') or [], layout, read)
    # Read every source port before writing any of them: a band that swaps
    # two lanes over would otherwise pick up its own output halfway through.
    taps = [cur[c] for c in read]

    if len(dest) == 1 and len(taps) > 1:
        # Several lanes into one — the mono feed a single subwoofer wants.
        gain = round(1.0 / len(taps), 4)
        mix = _node(nodes, f'{tag}sum', 'builtin', 'mixer',
                    control={f'Gain {i + 1}': gain
                             for i in range(len(taps))})
        for i, port in enumerate(taps):
            links.append({'output': port, 'input': f'{mix}:In {i + 1}'})
        taps = [f'{mix}:Out']

    for i, lane in enumerate(dest):
        pin, pout = _xover_lane(stage, f'{tag}d{i}', nodes, links)
        links.append({'output': taps[i % len(taps)], 'input': pin})
        cur[lane] = pout


def _band_lane(band: dict, tag: str, nodes: list,
               links: list) -> tuple[str, str]:
    """One channel of one band: the range, then its level, delay and polarity.

    Linkwitz-Riley sections, so that two bands meeting at one frequency add
    back up flat instead of dipping or bulging where they cross.
    """
    qs = XOVER_SLOPES.get(int(band.get('slope') or DEFAULT_BAND_SLOPE)) \
        or XOVER_SLOPES[DEFAULT_BAND_SLOPE]
    lo, hi = float(band.get('lo') or 0), float(band.get('hi') or 0)
    if lo and hi and hi <= lo:
        raise ValueError(f"{band.get('name') or 'band'}: the top of the band "
                         'has to sit above the bottom')
    chain: list[str] = []
    for i, q in enumerate(qs if lo else ()):
        chain.append(_node(nodes, f'{tag}hp{i}', 'builtin', 'bq_highpass',
                           control={'Freq': lo, 'Q': q}))
    for i, q in enumerate(qs if hi else ()):
        chain.append(_node(nodes, f'{tag}lp{i}', 'builtin', 'bq_lowpass',
                           control={'Freq': hi, 'Q': q}))
    delay_s = float(band.get('delay') or 0.0) / 1000.0
    if delay_s > 0:
        chain.append(_node(nodes, f'{tag}dly', 'builtin', 'delay',
                           config={'max-delay': MAX_DELAY_S},
                           control={'Delay (s)': min(delay_s, MAX_DELAY_S)}))
    mult = _preamp_mult(band.get('gain') or 0.0)
    if band.get('invert'):
        mult = -mult
    if abs(mult - 1.0) > 1e-9:
        chain.append(_node(nodes, f'{tag}trim', 'builtin', 'linear',
                           control={'Mult': round(mult, 6), 'Add': 0.0}))
    if not chain:
        chain.append(_node(nodes, f'{tag}thru', 'builtin', 'copy'))
    for a, b in zip(chain, chain[1:]):
        links.append({'output': f'{a}:Out', 'input': f'{b}:In'})
    return f'{chain[0]}:In', f'{chain[-1]}:Out'


def build_dest_graph(strip: Strip, dest: str) -> dict:
    """The graph feeding one destination: every band that names it, summed.

    A destination no band names still needs a graph — it is the input side of
    the crossover and has to carry audio to the taps — so it gets silence
    rather than nothing, and the device simply plays nothing.
    """
    channels = strip.channels
    nodes: list = []
    links: list = []
    entry = [f'{_node(nodes, f"in{c}", "builtin", "copy")}:In'
             for c in range(channels)]
    bands = [b for b in strip.bands
             if not b.get('mute') and dest in (b.get('outputs') or [])]

    outs: list[str] = []
    for c in range(channels):
        taps = []
        for bi, band in enumerate(bands):
            pin, pout = _band_lane(band, f'b{bi}c{c}', nodes, links)
            links.append({'output': f'in{c}:Out', 'input': pin})
            taps.append(pout)
        if not taps:
            # Nothing routed here: hold the lane at silence.
            name = _node(nodes, f'mute{c}', 'builtin', 'linear',
                         control={'Mult': 0.0, 'Add': 0.0})
            links.append({'output': f'in{c}:Out', 'input': f'{name}:In'})
            outs.append(f'{name}:Out')
        elif len(taps) == 1:
            outs.append(taps[0])
        else:
            mix = _node(nodes, f'sum{c}', 'builtin', 'mixer',
                        control={f'Gain {i + 1}': 1.0
                                 for i in range(len(taps))})
            for i, port in enumerate(taps):
                links.append({'output': port, 'input': f'{mix}:In {i + 1}'})
            outs.append(f'{mix}:Out')

    return {'nodes': nodes, 'links': links, 'inputs': entry, 'outputs': outs}


def build_graph(strip: Strip) -> dict:
    """The fused `filter.graph` for every enabled stage on this strip."""
    channels = strip.channels
    width = strip.out_channels
    if channels < 1 or width < 1:
        raise ValueError('a strip needs at least one channel')
    nodes: list = []
    links: list = []

    # A copy per capture channel, because a graph input port can be named
    # only once and a crossover reading the same channel into two different
    # bands needs it twice.  It also gives every lane something to carry when
    # the strip has no stages at all.
    entry = [f'{_node(nodes, f"in{c}", "builtin", "copy")}:In'
             for c in range(channels)]
    # Output lanes start on the capture channel of the same index; a strip
    # that outputs wider than it captures repeats the layout, so the extra
    # lanes carry a copy of the front until a band is routed onto them.
    cur: list = [f'in{c % channels}:Out' for c in range(width)]

    for si, stage in enumerate(strip.active_stages()):
        tag = f's{si}'
        kind = stage.get('kind')
        if kind == 'xover':
            _apply_xover(stage, tag, strip, cur, nodes, links)
            continue
        if kind == 'eq':
            lanes = [_eq_lane(stage, tag, c, nodes, links)
                     for c in range(width)]
            ins = [a for a, _ in lanes]
            outs = [b for _, b in lanes]
        elif kind == 'convolver':
            lanes = [_convolver_lane(stage, tag, c, nodes, links)
                     for c in range(width)]
            ins = [a for a, _ in lanes]
            outs = [b for _, b in lanes]
        elif kind == 'effect':
            ins, outs = _effect_lanes(stage, tag, width, nodes, links)
        else:
            raise ValueError(f'unknown stage kind {kind!r}')

        for c in range(width):
            links.append({'output': cur[c], 'input': ins[c]})
            cur[c] = outs[c]

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


def _xover_conf(strip: Strip) -> dict:
    """One unit, one filter-chain per destination.

    The first module is the crossover itself: a smart-filter insert on the
    device audio was already going to, so sources keep playing where they
    always played and nothing new appears to be selected.  Its own graph
    carries whatever bands were routed back to that device.

    Every other destination is a tap: it reads the insert's *input* — the
    monitor of its sink, which is the signal before any band was applied —
    filters its own bands out of it and plays them on its own device.  A tap
    rather than a second sink, because a sink here would be an output nobody
    should ever pick, and it would come back to haunt the device list.
    """
    dests = band_destinations(strip)
    host = strip.insert_into
    # The insert's device is served by the insert.  With no device named, the
    # crossover follows the default output and the first destination is a tap
    # like all the others.
    modules = []

    insert_graph = build_dest_graph(strip, host) if host else \
        build_dest_graph(strip, '\0none')
    modules.append({
        'name': 'libpipewire-module-filter-chain',
        'args': {
            'node.description': strip.name,
            'filter.graph': insert_graph,
            'capture.props': {
                'node.name': strip.node_name,
                'media.class': 'Audio/Sink',
                'node.description': strip.name,
                'audio.position': list(strip.positions),
                'node.link-group': strip.link_group,
                'filter.smart': True,
                'filter.smart.name': strip.link_group,
                **({'filter.smart.target': {'node.name': host}} if host
                   else {}),
            },
            'playback.props': {
                'node.name': f'{strip.node_name}.out',
                'node.description': f'{strip.name} output',
                'node.passive': True,
                'audio.position': list(strip.positions),
                'node.link-group': strip.link_group,
            },
        },
    })

    for i, dest in enumerate(d for d in dests if d != host):
        modules.append({
            'name': 'libpipewire-module-filter-chain',
            'args': {
                'node.description': f'{strip.name} band {i + 1}',
                'filter.graph': build_dest_graph(strip, dest),
                'capture.props': {
                    'node.name': f'{strip.node_name}.t{i}',
                    'node.description': f'{strip.name} → {dest}',
                    'audio.position': list(strip.positions),
                    'stream.capture.sink': True,
                    'target.object': strip.node_name,
                    'node.dont-reconnect': False,
                },
                'playback.props': {
                    'node.name': f'{strip.node_name}.t{i}.out',
                    'node.description': f'{strip.name} → {dest}',
                    'audio.position': list(strip.positions),
                    'target.object': dest,
                    'node.dont-reconnect': False,
                },
            },
        })
    return _base(modules)


def _conf(strip: Strip, strips=None) -> dict:
    if strip.role == 'xover':
        return _xover_conf(strip)
    capture = {
        'node.name': strip.node_name,
        'node.description': strip.name,
        'audio.position': list(strip.positions),
    }
    playback = {
        'node.name': f'{strip.node_name}.out',
        'node.description': f'{strip.name} output',
        'node.passive': True,
        'audio.position': strip.out_layout,
    }

    if strip.mode == 'tap':
        # Not a sink at all: a capture stream reading what another strip is
        # being fed, and a playback stream carrying it to its own device.
        # `node.passive` would let it be suspended along with a target that
        # is idle, which is wrong here — this stream is the reason the target
        # has anything to play.
        capture['stream.capture.sink'] = True
        if strip.tap_source:
            capture['target.object'] = strip.tap_source
            capture['node.dont-reconnect'] = False
        playback.pop('node.passive', None)
        dest = strip.insert_device()
        if dest:
            playback['target.object'] = dest
            playback['node.dont-reconnect'] = False
    elif strip.mode == 'insert':
        # A sink, but one WirePlumber splices into the device's path instead
        # of offering as an output.  No `target.object` on the playback side:
        # the session manager links it to whatever the filter attached to,
        # and hard-wiring it would fight that.
        capture['media.class'] = 'Audio/Sink'
        capture['node.link-group'] = strip.link_group
        capture['filter.smart'] = True
        capture['filter.smart.name'] = strip.link_group
        dest = strip.insert_device()
        if dest:
            capture['filter.smart.target'] = {'node.name': dest}
        playback['node.link-group'] = strip.link_group
    else:
        capture['media.class'] = 'Audio/Sink'
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
    # Only a published sink ever fans out: the other two modes are defined by
    # having exactly one destination, and a combine device is a new output in
    # the user's list — the thing insert mode exists to avoid.
    members = [] if strip.mode != 'sink' else _fan_members(strip, strips)
    existing = next((d for d in virtual.list_devices()
                     if d.id == strip.fan_id), None)
    if len(members) < 2:
        if existing:
            virtual.delete(existing)
        return True, ''
    if existing:
        dev = existing
        dev.members = members
        dev.positions = strip.out_layout
        dev.enabled = strip.enabled
    else:
        dev = virtual.VirtualDevice(
            id=strip.fan_id, name=f'{strip.name} fan-out',
            kind='combine-sink', members=members,
            positions=strip.out_layout, enabled=strip.enabled,
            persistent=strip.persistent)
    return virtual.apply(dev)


# --------------------------------------------------------------- lifecycle --

def apply(strip: Strip, strips=None) -> tuple[bool, str]:
    """Regenerate config and (re)start/stop the unit to match `enabled`."""
    # An inserted strip that has since grown a second output, or gained a
    # source sending into it, no longer fits the mode.  Fall back rather than
    # write a config that would attach to a device and then be fed twice.
    if strip.mode == 'insert' and not insertable(strip, strips):
        strip.mode = 'sink'
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
    'Strip', 'ROLES', 'MODES', 'insertable', 'SOURCE_KINDS', 'STAGE_KINDS',
    'BAND_FILTERS',
    'XOVER_MODES', 'XOVER_SLOPES', 'DEFAULT_SLOPE', 'sort_bands',
    'PATH_DIR', 'list_strips', 'sources', 'mixes', 'save_meta', 'new_strip',
    'new_stage', 'clone_stage', 'build_graph', 'generate', 'resolve_target',
    'sync_fan', 'apply', 'set_enabled', 'status', 'delete', 'target_edges',
    'mix_targets', 'output_targets', 'would_loop',
]
