"""Equalizer presets: a few built-in curves, and the user's own.

A user preset is a plain APO / AutoEQ text file in
`~/.config/pipewire-controller/enhance/presets/`, so it can be copied to and
from Equalizer APO, Peace or EasyEffects as it is, and the folder can be
filled by hand.  The name is the file's first comment line when it has one
(`# Preset: Late night`), else the file name.

Built-in curves carry the preamp `Curve.auto_preamp()` gives them, so loading
one never clips.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import eqcurve as E
from . import system
from .config import XDG_CONFIG

PRESET_DIR = XDG_CONFIG / 'pipewire-controller' / 'enhance' / 'presets'
_HEADER = re.compile(r'^\s*#\s*Preset:\s*(.+?)\s*$', re.I)


@dataclass(frozen=True)
class Preset:
    name: str
    curve: E.Curve
    blurb: str = ''
    builtin: bool = True
    path: Path | None = None

    @property
    def key(self) -> str:
        return ('builtin:' if self.builtin else 'user:') + self.name


def _b(t, f, g=0.0, q=1.0):
    return E.Band(t, f, g, q)


def _safe(name, blurb, *bands) -> Preset:
    return Preset(name, E.Curve(0.0, tuple(bands)).auto_preamp(), blurb)


BUILTIN: list[Preset] = [
    Preset('Flat', E.default_curve(), 'Five bands, nothing changed'),
    _safe('Bass boost', 'More low end, without boom',
          _b('LSC', 105, 6.0, 0.7), _b('PK', 250, -1.0, 1.0)),
    _safe('Treble boost', 'More air and detail',
          _b('HSC', 8000, 4.0, 0.7), _b('PK', 3000, 1.0, 1.0)),
    _safe('Vocal clarity', 'Voices forward, less mud',
          _b('LSC', 100, -1.5, 0.7), _b('PK', 300, -2.0, 1.2),
          _b('PK', 2500, 3.0, 1.2), _b('PK', 5000, 2.0, 1.6)),
    _safe('Late night', 'Full sound at low volume',
          _b('LSC', 90, 6.0, 0.7), _b('PK', 3000, -1.0, 1.0),
          _b('HSC', 10000, 4.0, 0.7)),
    _safe('Less harsh', 'Tames sharp, fatiguing treble',
          _b('PK', 3000, -3.0, 1.5), _b('PK', 6500, -2.5, 2.0),
          _b('HSC', 12000, -1.5, 0.7)),
    _safe('Speech', 'Podcasts and calls: rumble cut, words clear',
          _b('HPQ', 80, 0.0, 0.71), _b('PK', 200, -2.0, 1.0),
          _b('PK', 3000, 3.0, 1.0), _b('HSC', 9000, -2.0, 0.7)),
    _safe('Footsteps', 'Games: quieter rumble, clearer steps',
          _b('LSC', 120, -4.0, 0.7), _b('PK', 600, 1.5, 1.3),
          _b('PK', 2200, 4.0, 1.6), _b('PK', 5000, 2.5, 1.8)),
    _safe('Rumble cut', 'Removes sub-bass under 40 Hz',
          _b('HPQ', 40, 0.0, 0.71)),
]


def builtin(name: str) -> Preset | None:
    return next((p for p in BUILTIN if p.name == name), None)


# ------------------------------------------------------------ user presets --

def _slug(name: str) -> str:
    s = re.sub(r'[^A-Za-z0-9._ -]+', '', name).strip().replace(' ', '-')
    return (s or 'preset')[:60]


def _read(path: Path) -> Preset | None:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    name = path.stem
    for line in text.splitlines()[:3]:
        m = _HEADER.match(line)
        if m:
            name = m.group(1)
            break
    parsed = E.parse_text(text)
    if not parsed.curve.bands:
        return None
    return Preset(name, parsed.curve, '', False, path)


def list_user() -> list[Preset]:
    if not PRESET_DIR.is_dir():
        return []
    out = [p for p in (_read(f) for f in sorted(PRESET_DIR.glob('*.txt')))
           if p is not None]
    return sorted(out, key=lambda p: p.name.lower())


def save_user(name: str, curve: E.Curve) -> Preset:
    """Save (or overwrite) a user preset by name."""
    name = ' '.join(name.split()) or 'Preset'
    PRESET_DIR.mkdir(parents=True, exist_ok=True)
    existing = next((p for p in list_user() if p.name == name), None)
    path = existing.path if existing else PRESET_DIR / f'{_slug(name)}.txt'
    n = 2
    while existing is None and path.exists():
        path = PRESET_DIR / f'{_slug(name)}-{n}.txt'
        n += 1
    # The comment keeps the name exactly as typed; APO ignores it.
    text = f'# Preset: {name}\n' + E.to_text(curve)
    system.atomic_write(path, text)
    return Preset(name, curve, '', False, path)


def delete_user(preset: Preset) -> bool:
    if preset.builtin or preset.path is None:
        return False
    try:
        preset.path.unlink()
        return True
    except OSError:
        return False
