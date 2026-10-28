"""Equalizer curves: the model, its exact response, and the APO text format.

Pure: no subprocess, no GTK, no files.  Everything that edits, draws, saves,
imports or exports an equalizer curve goes through here, so the editor, the
page's thumbnails, the presets and the generator can never disagree about
what a curve is or how it sounds.

**The response is PipeWire's own, not an approximation.**  `Band.coeffs()`
follows `spa/plugins/audioconvert/biquad.c` (1.6.9): RBJ cookbook biquads with
`w0 = pi * f / (rate / 2)`, `A = 10^(gain/40)`, and shelves taking Q
directly.  Measured against the daemon's impulse response for both engines:
within 0.005 dB from 20 Hz to 20 kHz (docs.local/probes/2026-10-27-eq-engine).

**Values are immutable.**  A `Curve` and its `Band`s are frozen, so an undo
history is a list of curves and "did anything change" is `==`.

**Ranges are the engine's, measured.**  The biquad controls clamp `Gain` to
+20 dB and `Q` to 10, and the preamp's `linear` node tops out at x10
(+20 dB).  A curve loaded from disk is kept exactly as stored (an equalizer
made before the caps existed may exceed them, and must keep sounding the
same); `Curve.clamped()` is what an edit produces.
"""

from __future__ import annotations

import cmath
import math
import re
from dataclasses import dataclass, field, replace

RATE = 48000                 # the rate a response is drawn at by default

FREQ_MIN, FREQ_MAX = 20.0, 20000.0
GAIN_MIN, GAIN_MAX = -24.0, 20.0       # +20: the bq `Gain` port's ceiling
Q_MIN, Q_MAX = 0.1, 10.0               # 10: the bq `Q` port's ceiling
PREAMP_MIN, PREAMP_MAX = -30.0, 20.0   # +20: `linear` `Mult` stops at 10
MAX_BANDS = 32


# ------------------------------------------------------------ filter types --

@dataclass(frozen=True)
class FilterType:
    code: str          # stored value and APO token: PK, LSC, HSC, LPQ, ...
    label: str         # short name for the editor: "Bell"
    long: str          # longer name for menus and tooltips
    builtin: str       # PipeWire filter-graph label
    has_gain: bool     # takes a gain (bells and shelves)
    file_ok: bool      # param_eq's file parser reads it (1.6.9: PK/LSC/HSC)
    offered: bool      # offered in the editor's type picker
    # Control values that make the filter an exact pass-through, from
    # biquad.c's own edge cases: a band switched off, or the whole equalizer
    # bypassed, is a control write instead of a restart.
    identity: tuple = ()


TYPES: dict[str, FilterType] = {t.code: t for t in (
    FilterType('PK', 'Bell', 'Bell (peaking)', 'bq_peaking',
               True, True, True, (('Gain', 0.0),)),
    FilterType('LSC', 'Low shelf', 'Low shelf', 'bq_lowshelf',
               True, True, True, (('Gain', 0.0),)),
    FilterType('HSC', 'High shelf', 'High shelf', 'bq_highshelf',
               True, True, True, (('Gain', 0.0),)),
    # "Low cut" is what a listener calls a high-pass filter.
    FilterType('HPQ', 'Low cut', 'Low cut (high-pass)', 'bq_highpass',
               False, False, True, (('Freq', 0.0),)),
    FilterType('LPQ', 'High cut', 'High cut (low-pass)', 'bq_lowpass',
               False, False, True, (('Freq', 1e6),)),
    FilterType('NO', 'Notch', 'Notch', 'bq_notch',
               False, False, True, (('Freq', 0.0),)),
    # Read from files and kept, but not offered: neither is an equalizer move.
    FilterType('BP', 'Band-pass', 'Band-pass', 'bq_bandpass',
               False, False, False, (('Q', 0.0),)),
    FilterType('AP', 'All-pass', 'All-pass (phase only)', 'bq_allpass',
               False, False, False, (('Freq', 0.0),)),
)}
OFFERED = [t for t in TYPES.values() if t.offered]

# Shown in the order a mixing desk reads them, low to high.
TYPE_ORDER = ['HPQ', 'LSC', 'PK', 'NO', 'HSC', 'LPQ', 'BP', 'AP']


def _num(value, default, lo=None, hi=None) -> float:
    """A finite float, or `default`; optionally clamped."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# ------------------------------------------------------------------- band --

@dataclass(frozen=True)
class Band:
    type: str = 'PK'
    freq: float = 1000.0
    gain: float = 0.0
    q: float = 1.0
    on: bool = True
    # Stored keys this version does not know, kept so a newer app's field
    # survives being opened and saved here.
    extra: tuple = field(default=(), compare=False)

    @property
    def kind(self) -> FilterType:
        return TYPES.get(self.type, TYPES['PK'])

    # -- conversion --
    @classmethod
    def from_dict(cls, d: dict) -> 'Band':
        """A band as stored, coerced but not clamped (see the module doc)."""
        d = d or {}
        code = str(d.get('type', 'PK')).upper()
        if code not in TYPES:
            code = 'PK'
        known = {'on', 'type', 'freq', 'gain', 'q'}
        return cls(type=code,
                   freq=_num(d.get('freq'), 1000.0, 1.0),
                   gain=_num(d.get('gain'), 0.0),
                   q=_num(d.get('q'), 1.0, 0.001),
                   on=bool(d.get('on', True)),
                   extra=tuple((k, v) for k, v in d.items() if k not in known))

    def to_dict(self) -> dict:
        # Same keys and rounding the old dialog wrote, so nothing reading
        # these files changes.
        out = {'on': self.on, 'type': self.type,
               'freq': round(self.freq, 3), 'gain': round(self.gain, 2),
               'q': round(self.q, 3)}
        for k, v in self.extra:
            out.setdefault(k, v)
        return out

    def clamped(self) -> 'Band':
        return replace(self,
                       freq=min(max(self.freq, FREQ_MIN), FREQ_MAX),
                       gain=min(max(self.gain, GAIN_MIN), GAIN_MAX),
                       q=min(max(self.q, Q_MIN), Q_MAX))

    def fits_live(self) -> bool:
        """Within what the biquad engine's controls can hold."""
        return self.gain <= GAIN_MAX + 1e-9 and self.q <= Q_MAX + 1e-9

    # -- what it sounds like --
    @property
    def is_identity(self) -> bool:
        """It changes nothing: off, or a bell/shelf at 0 dB."""
        return not self.on or (self.kind.has_gain and abs(self.gain) < 1e-9)

    def coeffs(self, rate: int = RATE):
        """(b0, b1, b2, a1, a2), normalised by a0, exactly as biquad.c."""
        f = max(0.0, min(self.freq * 2.0 / rate, 1.0))
        q = self.q
        t = self.type
        A = 10.0 ** (self.gain / 40.0)
        ident = (1.0, 0.0, 0.0, 0.0, 0.0)
        if t in ('PK', 'LSC', 'HSC'):
            if t == 'PK' and (f <= 0 or f >= 1):
                return ident
            if t == 'PK' and q <= 0:
                return (A * A, 0.0, 0.0, 0.0, 0.0)
            if t == 'LSC' and f >= 1:
                return (A * A, 0.0, 0.0, 0.0, 0.0)
            if t == 'HSC' and f <= 0:
                return (A * A, 0.0, 0.0, 0.0, 0.0)
            if f <= 0 or f >= 1:
                return ident
            if q <= 0:
                q = 1 / math.sqrt(2)
            w0 = math.pi * f
            alpha = math.sin(w0) / (2 * q)
            k = math.cos(w0)
            if t == 'PK':
                b = (1 + alpha * A, -2 * k, 1 - alpha * A)
                a = (1 + alpha / A, -2 * k, 1 - alpha / A)
            else:
                k2 = 2 * math.sqrt(A) * alpha
                ap, am = A + 1, A - 1
                if t == 'LSC':
                    b = (A * (ap - am * k + k2), 2 * A * (am - ap * k),
                         A * (ap - am * k - k2))
                    a = (ap + am * k + k2, -2 * (am + ap * k),
                         ap + am * k - k2)
                else:
                    b = (A * (ap + am * k + k2), -2 * A * (am + ap * k),
                         A * (ap + am * k - k2))
                    a = (ap - am * k + k2, 2 * (am - ap * k),
                         ap - am * k - k2)
        else:
            if q <= 0 and t in ('BP', 'NO', 'AP'):
                # biquad.c's limits as Q -> 0: 1, 0 and -1
                g = {'BP': 1.0, 'NO': 0.0, 'AP': -1.0}[t]
                if 0 < f < 1:
                    return (g, 0.0, 0.0, 0.0, 0.0)
            if q <= 0:
                q = 1 / math.sqrt(2)
            if t == 'LPQ':
                if f >= 1:
                    return ident
                if f <= 0:
                    return (0.0, 0.0, 0.0, 0.0, 0.0)
            elif t == 'HPQ':
                if f <= 0:
                    return ident
                if f >= 1:
                    return (0.0, 0.0, 0.0, 0.0, 0.0)
            elif f <= 0 or f >= 1:
                return (0.0, 0.0, 0.0, 0.0, 0.0) if t == 'BP' else ident
            w0 = math.pi * f
            alpha = math.sin(w0) / (2 * q)
            k = math.cos(w0)
            if t == 'LPQ':
                b = ((1 - k) / 2, 1 - k, (1 - k) / 2)
            elif t == 'HPQ':
                b = ((1 + k) / 2, -(1 + k), (1 + k) / 2)
            elif t == 'BP':
                b = (alpha, 0.0, -alpha)
            elif t == 'NO':
                b = (1.0, -2 * k, 1.0)
            else:                                   # AP
                b = (1 - alpha, -2 * k, 1 + alpha)
            a = (1 + alpha, -2 * k, 1 - alpha)
        a0 = a[0]
        return (b[0] / a0, b[1] / a0, b[2] / a0, a[1] / a0, a[2] / a0)

    def response(self, freqs, rate: int = RATE) -> list[float]:
        """Magnitude in dB at each frequency (0 when the band is off)."""
        if not self.on:
            return [0.0] * len(freqs)
        b0, b1, b2, a1, a2 = self.coeffs(rate)
        out = []
        for f in freqs:
            z1 = cmath.exp(-2j * math.pi * f / rate)
            z2 = z1 * z1
            num = b0 + b1 * z1 + b2 * z2
            den = 1 + a1 * z1 + a2 * z2
            mag = abs(num) / abs(den) if abs(den) > 1e-300 else 0.0
            out.append(20 * math.log10(max(mag, 1e-9)))
        return out

    # -- text --
    def describe(self) -> str:
        """"Bell · 1 kHz · +3.0 dB · Q 1.00", for tooltips and lists."""
        bits = [self.kind.label, format_hz(self.freq)]
        if self.kind.has_gain:
            bits.append(f'{self.gain:+.1f} dB')
        bits.append(f'Q {self.q:.2f}')
        return ' · '.join(bits)


def format_hz(f: float) -> str:
    """1000 -> "1 kHz", 1250 -> "1.25 kHz", 63 -> "63 Hz"."""
    if f >= 1000:
        return f'{f / 1000:.3g} kHz'
    if f >= 100:
        return f'{f:.0f} Hz'
    return f'{f:.3g} Hz'


def q_to_octaves(q: float) -> float:
    """A bell's bandwidth in octaves for a Q (RBJ's definition)."""
    q = max(q, 1e-6)
    return 2 / math.log(2) * math.asinh(1 / (2 * q))


def octaves_to_q(bw: float) -> float:
    bw = max(bw, 1e-6)
    p = 2 ** bw
    return math.sqrt(p) / (p - 1)


def slope_to_q(slope_db: float, gain_db: float) -> float:
    """A shelf's Q for a slope in dB/octave (RBJ's S, with S=1 = 12 dB/oct)."""
    s = max(min(slope_db / 12.0, 1.0), 0.05)
    A = 10 ** (gain_db / 40)
    inner = (A + 1 / A) * (1 / s - 1) + 2
    return 1 / math.sqrt(inner) if inner > 0 else 1 / math.sqrt(2)


def log_freqs(n: int = 240, lo: float = FREQ_MIN, hi: float = FREQ_MAX):
    """`n` frequencies spaced evenly on a log axis, lo..hi inclusive."""
    r = math.log(hi / lo)
    return [lo * math.exp(r * i / (n - 1)) for i in range(n)]


# ------------------------------------------------------------------ curve --

@dataclass(frozen=True)
class Curve:
    preamp: float = 0.0
    bands: tuple = ()

    @classmethod
    def from_params(cls, params: dict) -> 'Curve':
        params = params or {}
        bands = tuple(Band.from_dict(b) for b in params.get('bands') or []
                      if isinstance(b, dict))
        return cls(preamp=_num(params.get('preamp'), 0.0), bands=bands)

    def to_params(self) -> dict:
        """The two keys an equalizer's params carry; merge, don't replace."""
        return {'preamp': round(self.preamp, 2),
                'bands': [b.to_dict() for b in self.bands]}

    # -- edits (each returns a new curve) --
    def clamped(self) -> 'Curve':
        return Curve(min(max(self.preamp, PREAMP_MIN), PREAMP_MAX),
                     tuple(b.clamped() for b in self.bands[:MAX_BANDS]))

    def with_band(self, i: int, **changes) -> 'Curve':
        bands = list(self.bands)
        bands[i] = replace(bands[i], **changes).clamped()
        return replace(self, bands=tuple(bands))

    def with_preamp(self, db: float) -> 'Curve':
        return replace(self, preamp=min(max(db, PREAMP_MIN), PREAMP_MAX))

    def added(self, band: Band) -> 'Curve':
        if len(self.bands) >= MAX_BANDS:
            return self
        return replace(self, bands=self.bands + (band.clamped(),))

    def removed(self, i: int) -> 'Curve':
        return replace(self, bands=self.bands[:i] + self.bands[i + 1:])

    def sorted(self) -> 'Curve':
        return replace(self, bands=tuple(sorted(self.bands,
                                                key=lambda b: b.freq)))

    def flat(self) -> 'Curve':
        """Nothing changes the sound any more; the bands stay to drag."""
        bands = tuple(replace(b, gain=0.0) if b.kind.has_gain
                      else replace(b, on=False) for b in self.bands)
        return Curve(0.0, bands)

    def auto_preamp(self, rate: int = RATE) -> 'Curve':
        """The preamp that keeps the loudest boost at or under 0 dB.

        Rounded down to 0.1 dB so the result never clips by a rounding
        error; a curve that only cuts gets 0.
        """
        peak = Curve(0.0, self.bands).peak_db(rate)
        pre = -max(peak, 0.0)
        pre = math.floor(pre * 10 + 1e-9) / 10
        return self.with_preamp(pre)

    # -- reading it --
    def response(self, freqs, rate: int = RATE,
                 with_preamp: bool = True) -> list[float]:
        total = [self.preamp if with_preamp else 0.0] * len(freqs)
        for b in self.bands:
            if b.on:
                total = [t + r for t, r in zip(total, b.response(freqs, rate))]
        return total

    def peak_db(self, rate: int = RATE) -> float:
        """The loudest point of the response, preamp included."""
        freqs = log_freqs(400)
        # a narrow bell can sit between grid points: look at its centre too
        freqs += [b.freq for b in self.bands
                  if b.on and FREQ_MIN <= b.freq <= FREQ_MAX]
        return max(self.response(freqs, rate)) if freqs else self.preamp

    def level_db(self, rate: int = RATE) -> float:
        """How much louder (+) or quieter (-) the curve makes music, in dB,
        preamp included.  For a fair A/B: louder always sounds better.

        An estimate, not a measurement: the change in loudness (LUFS) of pink
        noise played through the curve.  Pink noise has the same energy in
        every octave, so it is an average over a log axis; each frequency is
        weighted as ITU-R BS.1770 weights it (`_K_POWER`), so a cut below
        30 Hz or a boost at 18 kHz hardly counts, as you hardly hear it.
        """
        resp = self.response(_K_FREQS, rate)
        num = sum(w * 10 ** (r / 10) for w, r in zip(_K_POWER, resp))
        return 10 * math.log10(max(num / _K_TOTAL, 1e-12))

    def fits_live(self) -> bool:
        return (self.preamp <= PREAMP_MAX + 1e-9
                and all(b.fits_live() for b in self.bands))

    def file_ok(self) -> bool:
        """Every band is a type the AutoEQ-file engine reads."""
        return all(b.kind.file_ok for b in self.bands)

    @property
    def active(self) -> int:
        return sum(1 for b in self.bands if not b.is_identity)


def _k_power(freqs) -> list[float]:
    """|H(f)|^2 of BS.1770's K-weighting (the two-stage filter loudness
    meters use), from the standard's 48 kHz coefficients."""
    stages = (((1.53512485958697, -2.69169618940638, 1.19839281085285),
               (-1.69065929318241, 0.73248077421585)),
              ((1.0, -2.0, 1.0),
               (-1.99004745483398, 0.99007225036621)))
    out = []
    for f in freqs:
        z1 = cmath.exp(-2j * math.pi * f / 48000)
        z2 = z1 * z1
        p = 1.0
        for (b0, b1, b2), (a1, a2) in stages:
            p *= abs(b0 + b1 * z1 + b2 * z2) ** 2 / \
                abs(1 + a1 * z1 + a2 * z2) ** 2
        out.append(p)
    return out


_K_FREQS = log_freqs(160)
_K_POWER = _k_power(_K_FREQS)
_K_TOTAL = sum(_K_POWER)


def default_curve() -> Curve:
    """A neutral 5-band start: a low shelf, three bells, a high shelf."""
    return Curve(0.0, (Band('LSC', 80, 0.0, 0.7),
                       Band('PK', 250, 0.0, 1.0),
                       Band('PK', 1000, 0.0, 1.0),
                       Band('PK', 4000, 0.0, 1.0),
                       Band('HSC', 12000, 0.0, 0.7)))


def new_band_at(curve: Curve, freq: float = 1000.0,
                gain: float = 0.0) -> Band:
    """A band for "add one here": a bell, unless it sits at an edge."""
    if freq < 40 and gain < 0:
        return Band('HPQ', freq, 0.0, 0.71).clamped()
    if freq > 16000 and gain < 0:
        return Band('LPQ', freq, 0.0, 0.71).clamped()
    return Band('PK', freq, gain, 1.0).clamped()


# ----------------------------------------------------- APO / AutoEQ text --
# One filter per line, Equalizer APO's "Filter" command, which is also what
# AutoEQ's ParametricEQ.txt, Squiglink and REW export:
#   Preamp: -6.0 dB
#   Filter 1: ON PK Fc 105 Hz Gain -3.0 dB Q 0.70
# The writer keeps today's exact layout for PK/LSC/HSC: param_eq reads it.

_NUM = r'[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?'
_PREAMP = re.compile(rf'^\s*Preamp\s*:\s*({_NUM})\s*dB', re.I)
_FILTER = re.compile(r'^\s*Filter\s*\d*\s*:\s*(ON|OFF)\s+([A-Za-z]+)(.*)$',
                     re.I)
_CMD = re.compile(r'^\s*([A-Za-z]+)\s*:')
_FC = re.compile(rf'\bFc\s+({_NUM})\s*(k?)Hz', re.I)
_GAIN = re.compile(rf'\bGain\s+({_NUM})\s*dB', re.I)
_Q = re.compile(rf'\bQ\s+({_NUM})', re.I)
_BW = re.compile(rf'\bBW\s+Oct\s+({_NUM})', re.I)
_SLOPE = re.compile(rf'^\s*({_NUM})\s*dB\b', re.I)


def _f(text: str) -> float:
    return float(text.replace(',', '.'))


def to_text(curve: Curve) -> str:
    """The curve as APO / AutoEQ text."""
    lines = [f'Preamp: {curve.preamp:.1f} dB']
    for i, b in enumerate(curve.bands, 1):
        state = 'ON' if b.on else 'OFF'
        if b.kind.has_gain:
            lines.append(f'Filter {i}: {state} {b.type} Fc {b.freq:g} Hz '
                         f'Gain {b.gain:g} dB Q {b.q:g}')
        else:
            lines.append(f'Filter {i}: {state} {b.type} Fc {b.freq:g} Hz '
                         f'Q {b.q:g}')
    return '\n'.join(lines) + '\n'


@dataclass
class Parsed:
    curve: Curve
    notes: list            # what was changed or left out, in plain words
    filters_seen: int      # lines that looked like a filter at all


def parse_text(text: str) -> Parsed:
    """Read APO / AutoEQ text.  Never raises on content.

    Everything the editor cannot represent exactly is either converted (and
    said so) or skipped (and said so) -- a silently half-imported curve
    sounds wrong with no way to find out why.
    """
    notes: list[str] = []
    bands: list[Band] = []
    preamp = 0.0
    seen = 0
    channel_sel = None          # the first non-"all" channel selection used
    skipping = False
    skipped_types: dict[str, int] = {}
    clamped = 0
    ignored_cmds: set[str] = set()

    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        m = _PREAMP.match(line)
        if m:
            if not skipping:
                preamp += _f(m.group(1))
            continue
        cm = _CMD.match(line)
        cmd = cm.group(1).lower() if cm else ''
        if cmd == 'channel':
            sel = line.split(':', 1)[1].strip().lower()
            if sel in ('all', ''):
                skipping = False
            elif channel_sel is None:
                channel_sel = sel
                skipping = False
            else:
                skipping = sel != channel_sel
            continue
        fm = _FILTER.match(line)
        if not fm:
            if cmd and cmd not in ('filter', 'preamp'):
                ignored_cmds.add(cm.group(1))
            continue
        seen += 1
        if skipping:
            continue
        if len(bands) >= MAX_BANDS:
            notes.append(f'Only the first {MAX_BANDS} filters were kept.')
            break
        on = fm.group(1).upper() == 'ON'
        token = fm.group(2).upper()
        rest = fm.group(3)
        band = _parse_filter(token, rest, on, notes, skipped_types)
        if band is None:
            continue
        c = band.clamped()
        if c != band:
            clamped += 1
        bands.append(c)

    if channel_sel is not None:
        notes.append(f'This file sets channels separately; only the '
                     f'“{channel_sel.upper()}” settings were used, for '
                     'every channel.')
    for token, n in sorted(skipped_types.items()):
        notes.append(f'{n} filter{"s" if n > 1 else ""} of type {token} '
                     'left out (not supported).')
    if clamped:
        notes.append(f'{clamped} filter{"s were" if clamped > 1 else " was"}'
                     f' outside the range the equalizer can play '
                     f'({GAIN_MIN:+.0f} to {GAIN_MAX:+.0f} dB, Q '
                     f'{Q_MIN:g}–{Q_MAX:g}) and {"were" if clamped > 1 else "was"}'
                     ' brought inside it.')
    if 'GraphicEQ' in ignored_cmds or 'graphiceq' in {c.lower() for c in
                                                      ignored_cmds}:
        notes.append('Graphic EQ lines are not supported; use the '
                     'ParametricEQ.txt version of the file.')
        ignored_cmds = {c for c in ignored_cmds if c.lower() != 'graphiceq'}
    if ignored_cmds:
        notes.append('Ignored: ' + ', '.join(sorted(ignored_cmds)) + '.')
    pre = min(max(preamp, PREAMP_MIN), PREAMP_MAX)
    if pre != preamp:
        notes.append(f'The preamp was brought inside {PREAMP_MIN:+.0f} to '
                     f'{PREAMP_MAX:+.0f} dB.')
    return Parsed(Curve(pre, tuple(bands)), notes, seen)


def _parse_filter(token, rest, on, notes, skipped) -> Band | None:
    fc = _FC.search(rest)
    if not fc:
        skipped[token] = skipped.get(token, 0) + 1
        return None
    freq = _f(fc.group(1)) * (1000 if fc.group(2) else 1)
    gm = _GAIN.search(rest)
    gain = _f(gm.group(1)) if gm else 0.0
    qm = _Q.search(rest)
    bw = _BW.search(rest)
    q = _f(qm.group(1)) if qm else (octaves_to_q(_f(bw.group(1)))
                                    if bw else None)
    if token in ('PK', 'PEQ', 'MODAL'):
        if token == 'MODAL':
            notes.append('A Modal filter was read as a bell; its decay '
                         'target was left out.')
        return Band('PK', freq, gain, q if q is not None else 1.0, on)
    if token in ('LSC', 'HSC', 'LS', 'HS'):
        code = 'LSC' if token.startswith('L') else 'HSC'
        slope = _SLOPE.match(rest)
        if q is None and slope:
            q = slope_to_q(_f(slope.group(1)), gain)
        if q is None:
            q = 1 / math.sqrt(2)
        if token in ('LS', 'HS'):
            # APO's LS/HS are its own shelf designs, not RBJ's with a Q
            notes.append(f'A {token} shelf was read as a {code} shelf with '
                         f'Q {q:.2f}; it may sound slightly different.')
        return Band(code, freq, gain, q, on)
    if token in ('LP', 'LPQ', 'HP', 'HPQ'):
        code = 'LPQ' if token.startswith('L') else 'HPQ'
        return Band(code, freq, 0.0, q if q is not None else 1 / math.sqrt(2),
                    on)
    if token in ('NO', 'BP', 'AP'):
        if q is None:
            q = 1 / math.sqrt(2)
            notes.append(f'A {token} filter had no Q; 0.71 was used.')
        return Band(token, freq, 0.0, q, on)
    skipped[token] = skipped.get(token, 0) + 1
    return None
