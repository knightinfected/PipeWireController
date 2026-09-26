"""How loud a convolver chain comes out, measured from its own IR files.

Issue #17.  A convolver multiplies the signal by whatever its impulse response
does, and HRIR sets are not normalised for unity playback: one HeSuVi path is
already +2 to +14 dB, and the virtual-surround templates then sum eight of them
into each ear.  Across a whole HeSuVi collection every file overloads at gain
1.0, by +5 to +19 dB, and the right gain spans 0.12 - 0.55, so no single
default is right.  The number has to come from the file.

Nothing here guesses the graph.  It walks the `filter.graph` the chain really
emits — a template's own builder, or an imported config's text — so a template
that changes shape is measured as it now is, and a hand-written chain is
measured as written.  Anything on the path it cannot model (a plugin, a biquad,
a SOFA spatializer) makes it say so rather than return a number.

The measure: per graph output, the peak over frequency of the power sum of each
input's transfer function — every speaker channel treated as independent,
which is what surround content mostly is.  The coherent sum (one signal in
every input at once) is the true worst case, but sizing the gain off it leaves
ordinary content 6-13 dB too quiet, so it is reported, not used.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from .. import spa_json
from . import templates

MIN_NFFT = 16384        # the resolution issue #17's collection was measured at
CLIP_DB = 0.1           # above this a chain is "too loud"; the suggestion is
#                         floored to 0.01, so it always lands at or under 0 dB
_FLAT = ('/dirac', '/hilbert')     # |H| = 1 by construction
# the `needs` of every template built from builtin convolvers
CONVOLVING = ('hesuvi', 'true-stereo', 'stereo')


class Unmeasurable(Exception):
    """This chain's level cannot be worked out here; str() says why."""


@dataclass(frozen=True)
class Headroom:
    unity: float            # peak with every convolver at gain 1.0 (linear)
    peak: float             # peak at the gains the graph actually carries
    coherent: float         # worst case at those gains: one signal everywhere
    paths: int              # convolvers summed into the loudest output
    gain: float | None      # the convolvers' shared gain, None if they differ

    @property
    def peak_db(self) -> float:
        return db(self.peak)

    @property
    def over_db(self) -> float:
        return max(0.0, self.peak_db)

    @property
    def clips(self) -> bool:
        return self.peak_db > CLIP_DB

    @property
    def suggested(self) -> float:
        """The convolver gain that brings typical content to full scale.

        Floored to the dialog's two decimals so it never rounds up past 0 dB,
        and capped at 1.0: the fix is for clipping, so a quiet IR is left as
        quiet as it was rather than boosted.
        """
        if self.unity <= 0:
            return 1.0
        return max(0.01, min(1.0, math.floor(100.0 / self.unity) / 100.0))

    def peak_at(self, gain: float) -> float:
        """The peak if every convolver carried `gain` (dB)."""
        return db(self.unity * gain)


def db(x: float) -> float:
    return 20.0 * math.log10(x) if x > 0 else float('-inf')


# ------------------------------------------------------------ the IR files --

def _np():
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as e:
        raise Unmeasurable(f'{e.name or e} is not installed') from None
    return np, sf


@lru_cache(maxsize=16)
def _load(path: str, _mtime: int, _size: int):
    np, sf = _np()
    try:
        data, _rate = sf.read(path, always_2d=True, dtype='float64')
    except Exception as e:              # soundfile raises RuntimeError kinds
        raise Unmeasurable(f'{Path(path).name}: {e}') from None
    return data


def _ir(path: str, channel: int, offset: int, length: int):
    """The IR exactly as the convolver would load it (offset/length/channel)."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        raise Unmeasurable(f'{p.name} is not there') from None
    data = _load(str(p), st.st_mtime_ns, st.st_size)
    if not 0 <= channel < data.shape[1]:
        raise Unmeasurable(f'{p.name} has no channel {channel}')
    ir = data[max(0, offset):, channel]
    return ir[:length] if length > 0 else ir


# ---------------------------------------------------------- the graph walk --

_DEFAULT_IN = {'copy': 'In', 'convolver': 'In', 'mixer': 'In 1'}


def _config_ir(name, cfg):
    fn = cfg.get('filename')
    if isinstance(fn, str) and fn.startswith(_FLAT):
        return None                                     # flat: contributes 1
    if not isinstance(fn, str) or not fn:
        raise Unmeasurable(f'{name} has no IR file it can be measured from')
    if fn.startswith('/ir:'):
        raise Unmeasurable(f'{name} uses an inline IR')
    try:
        return (fn, int(cfg.get('channel', 0)), int(cfg.get('offset', 0)),
                int(cfg.get('length', 0)))
    except (TypeError, ValueError):
        raise Unmeasurable(f'{name} has a config this cannot read') from None


def _terms(graph):
    """{output port: [(input port, ir keys, convolver gain, other gain)]}.

    One term per route from a graph input to an output.  `ir keys` is every
    convolver on the route in order (their spectra multiply), and the two
    gains are kept apart so the same walk answers "as it is" and "at 1.0".
    """
    nodes = {n['name']: n for n in graph.get('nodes') or []
             if isinstance(n, dict) and isinstance(n.get('name'), str)}

    def port(ref, direction):
        if not isinstance(ref, str):
            raise Unmeasurable('the graph has a port it cannot read')
        node, _, name = ref.partition(':')
        if node not in nodes:
            raise Unmeasurable(f'the graph names a node "{node}" it lacks')
        if not name:
            label = nodes[node].get('label')
            name = 'Out' if direction == 'out' else _DEFAULT_IN.get(label, '')
        return node, name

    feeds = {port(l.get('input'), 'in'): port(l.get('output'), 'out')
             for l in graph.get('links') or [] if isinstance(l, dict)}
    inputs = {port(p, 'in') for p in graph.get('inputs') or [] if p}
    outputs = [port(p, 'out') for p in graph.get('outputs') or [] if p]
    if not inputs or not outputs:
        # filter-chain can infer them, but then this would be guessing too
        raise Unmeasurable('the graph does not list its inputs and outputs')

    def upstream(node, name, depth):
        key = (node, name)
        if key in feeds:
            return trace(feeds[key], depth + 1)
        if key in inputs:
            return [(key, (), 1.0, 1.0)]
        return []                                       # unconnected: silence

    def trace(out, depth):
        if depth > 64:
            raise Unmeasurable('the graph loops')
        name = out[0]
        node = nodes[name]
        label = node.get('label')
        if node.get('type') != 'builtin':
            raise Unmeasurable(f'{name} is a {node.get("type")} plugin, '
                               'whose level cannot be worked out here')
        if label == 'copy':
            return upstream(name, 'In', depth)
        if label == 'convolver':
            cfg = node.get('config') or {}
            ir = _config_ir(name, cfg)
            try:
                g = float(cfg.get('gain', 1.0))
            except (TypeError, ValueError):
                raise Unmeasurable(f'{name} has a gain this cannot read') \
                    from None
            return [(src, irs + ((ir,) if ir else ()), cg * g, og)
                    for src, irs, cg, og in upstream(name, 'In', depth)]
        if label == 'mixer':
            ctl = node.get('control') or {}
            out_terms = []
            for i in range(1, 9):
                try:
                    g = float(ctl.get(f'Gain {i}', 1.0))
                except (TypeError, ValueError):
                    raise Unmeasurable(f'{name} has a gain this cannot read') \
                        from None
                out_terms += [(src, irs, cg, og * g) for src, irs, cg, og
                              in upstream(name, f'In {i}', depth)]
            return out_terms
        raise Unmeasurable(f'{name} ({label}) changes the level in a way '
                           'this does not model')

    return {out: trace(out, 0) for out in outputs}, nodes


def measure_graph(graph) -> Headroom:
    """Measure one `filter.graph` dict.  Raises Unmeasurable."""
    terms, nodes = _terms(graph)
    np, _sf = _np()
    keys = {ir for ts in terms.values() for _s, irs, _c, _o in ts for ir in irs}
    if not keys:
        raise Unmeasurable('there is no convolver in this chain')
    irs = {k: _ir(*k) for k in keys}
    longest = max(len(v) for v in irs.values())
    nfft = max(MIN_NFFT, 1 << max(0, longest - 1).bit_length())
    spectra = {k: np.fft.rfft(v, nfft) for k, v in irs.items()}
    ones = np.ones(nfft // 2 + 1)

    unity = peak = coherent = 0.0
    paths = 0
    for ts in terms.values():
        if not ts:
            continue
        by_src_unity, by_src = defaultdict(lambda: 0j), defaultdict(lambda: 0j)
        for src, ir_keys, cg, og in ts:
            h = ones
            for k in ir_keys:
                h = h * spectra[k]
            by_src_unity[src] = by_src_unity[src] + og * h
            by_src[src] = by_src[src] + cg * og * h

        def power(d):
            return float(np.sqrt(sum(np.abs(t) ** 2 for t in d.values())).max())
        u, p = power(by_src_unity), power(by_src)
        c = float(np.abs(sum(by_src.values())).max())
        if p > peak or (p == peak and u > unity):
            paths = sum(1 for _s, k, _c, _o in ts if k)
        unity, peak, coherent = max(unity, u), max(peak, p), max(coherent, c)

    gains = {float((n.get('config') or {}).get('gain', 1.0))
             for n in nodes.values() if n.get('label') == 'convolver'}
    return Headroom(unity=unity, peak=peak, coherent=coherent, paths=paths,
                    gain=gains.pop() if len(gains) == 1 else None)


# ---------------------------------------------------------------- entries --

def for_template(template: str, hrir: str, hrir_channels: int = 0,
                 gain: float = 1.0) -> Headroom:
    """A template chain that may not exist yet (the dialog's live answer)."""
    tpl = templates.TEMPLATES.get(template)
    if tpl is None:
        raise Unmeasurable(f'unknown template {template}')
    if tpl['needs'] == 'sofa':
        raise Unmeasurable('SOFA files are rendered by libmysofa, whose '
                           'level cannot be measured here')
    if tpl['needs'] not in CONVOLVING:
        raise Unmeasurable('this template has no convolver')
    if not hrir:
        raise Unmeasurable('no impulse response chosen')
    if not hrir_channels:
        from .hrir import analyze
        hrir_channels = analyze(hrir).channels
    meta = SimpleNamespace(id='measure', name='measure', template=template,
                           hrir=hrir, hrir_channels=hrir_channels, target='',
                           params={'gain': gain})
    try:
        graph, _cap, _play = tpl['build'](meta)
    except (KeyError, ValueError) as e:
        raise Unmeasurable(str(e)) from None
    return measure_graph(graph)


def for_raw(raw_text: str) -> Headroom:
    """An imported config, measured as written."""
    try:
        data = spa_json.loads(raw_text)
    except spa_json.SpaJsonError as e:
        raise Unmeasurable(f'the config does not parse: {e}') from None
    graphs = [(m.get('args') or {}).get('filter.graph')
              for m in data.get('context.modules') or []
              if isinstance(m, dict)
              and m.get('name') == 'libpipewire-module-filter-chain']
    if len(graphs) != 1 or not isinstance(graphs[0], dict):
        raise Unmeasurable('the config does not hold exactly one filter chain')
    return measure_graph(graphs[0])


def for_chain(meta) -> Headroom:
    """A saved chain, template or imported, at the gains it really runs at."""
    if meta.template == 'raw':
        return for_raw(meta.params.get('raw_text', ''))
    return for_template(meta.template, meta.hrir, meta.hrir_channels,
                        meta.params.get('gain', 1.0))


def check(fn, *args, **kw) -> tuple[Headroom | None, str]:
    """(headroom, '') or (None, why not) — for callers that only report."""
    try:
        return fn(*args, **kw), ''
    except Unmeasurable as e:
        return None, str(e)
