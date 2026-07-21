"""HRIR / impulse-response library: scan, analyze, classify, import."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import XDG_CONFIG

try:
    import soundfile as sf
except ImportError:          # analyzed lazily; app still runs without it
    sf = None

APP_DIR = XDG_CONFIG / 'pipewire-controller'
LIBRARY_DIR = APP_DIR / 'hrir'

AUDIO_EXTS = {'.wav', '.flac', '.ogg', '.aiff', '.aif', '.w64'}
SOFA_EXTS = {'.sofa'}

# channel count -> (kind id, human label, matching templates)
CLASSES = {
    14: ('hesuvi', 'HeSuVi 7.1 HRIR (14ch)',
         ['virtual-surround-7.1', 'virtual-surround-5.1', 'virtual-surround-stereo']),
    4: ('true-stereo', 'True-stereo IR (4ch: LL LR RL RR)', ['true-stereo-ir']),
    2: ('stereo', 'Stereo IR (2ch)', ['stereo-ir']),
    1: ('mono', 'Mono IR (1ch)', ['stereo-ir']),
}


@dataclass
class IRInfo:
    path: Path
    ok: bool = False
    error: str = ''
    channels: int = 0
    samplerate: int = 0
    frames: int = 0
    subtype: str = ''
    fmt: str = ''
    is_sofa: bool = False

    @property
    def duration(self) -> float:
        return self.frames / self.samplerate if self.samplerate else 0.0

    @property
    def kind(self) -> str:
        if self.is_sofa:
            return 'sofa'
        return CLASSES.get(self.channels, (f'{self.channels}ch',))[0]

    @property
    def kind_label(self) -> str:
        if self.is_sofa:
            return 'SOFA HRTF (spatializer)'
        if self.channels in CLASSES:
            return CLASSES[self.channels][1]
        return f'{self.channels}-channel IR (no matching template)'

    @property
    def templates(self) -> list[str]:
        if self.is_sofa:
            return ['sofa-spatializer-7.1', 'sofa-spatializer-5.1']
        return list(CLASSES.get(self.channels, (None, None, []))[2])


def analyze(path) -> IRInfo:
    path = Path(path)
    info = IRInfo(path=path)
    if path.suffix.lower() in SOFA_EXTS:
        info.is_sofa = True
        info.ok = path.is_file()
        if not info.ok:
            info.error = 'file not found'
        return info
    if sf is None:
        info.error = 'python-soundfile not installed'
        return info
    try:
        meta = sf.info(str(path))
        info.ok = True
        info.channels = meta.channels
        info.samplerate = meta.samplerate
        info.frames = meta.frames
        info.subtype = meta.subtype or ''
        info.fmt = meta.format or ''
    except Exception as e:      # soundfile raises RuntimeError subclasses
        info.error = str(e)
    return info


def scan_dir(directory=None) -> list[IRInfo]:
    directory = Path(directory) if directory else LIBRARY_DIR
    if not directory.is_dir():
        return []
    out = []
    for f in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if f.is_file() and f.suffix.lower() in (AUDIO_EXTS | SOFA_EXTS):
            out.append(analyze(f))
    return out


def import_file(src) -> Path:
    """Copy a file into the library, avoiding name clashes."""
    src = Path(src)
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    dst = LIBRARY_DIR / src.name
    if dst.exists() and dst.samefile(src):
        return dst
    n = 1
    while dst.exists():
        dst = LIBRARY_DIR / f'{src.stem}-{n}{src.suffix}'
        n += 1
    shutil.copy2(src, dst)
    return dst


def remove_file(path) -> bool:
    path = Path(path)
    try:
        if path.is_file() and path.parent == LIBRARY_DIR:
            path.unlink()
            return True
    except OSError:
        pass
    return False


def generate_demo_hrir() -> Path | None:
    """Synthesize a basic 14-channel HeSuVi-layout HRIR for testing.

    Crude ITD/ILD model: each virtual speaker contributes a delayed,
    attenuated impulse per ear, with the far ear slightly low-passed.
    Not audiophile material — but lets users hear virtual surround work
    before downloading a real HRIR set.
    """
    try:
        import numpy as np
        import soundfile as _sf
    except ImportError:
        return None

    rate = 48000
    length = 2048
    head_ms = 0.65          # max interaural delay

    # HeSuVi channel order: pairs of (speaker, ear)
    layout = [('FL', 'L'), ('FL', 'R'), ('SL', 'L'), ('SL', 'R'),
              ('RL', 'L'), ('RL', 'R'), ('FC', 'L'), ('FR', 'R'),
              ('FR', 'L'), ('SR', 'R'), ('SR', 'L'), ('RR', 'R'),
              ('RR', 'L'), ('FC', 'R')]
    azimuth = {'FL': -30, 'FR': 30, 'FC': 0, 'SL': -90, 'SR': 90,
               'RL': -150, 'RR': 150}

    data = np.zeros((length, 14), dtype=np.float32)
    for ch, (spk, ear) in enumerate(layout):
        az = np.radians(azimuth[spk])
        # positive lateral = toward right ear
        lateral = np.sin(az)
        same_side = (lateral >= 0) == (ear == 'R')
        delay_ms = (0.05 if same_side else 0.05 + abs(lateral) * head_ms)
        gain = 0.9 if same_side else 0.9 - 0.45 * abs(lateral)
        # back speakers a bit quieter/duller
        if spk in ('RL', 'RR'):
            gain *= 0.85
        idx = int(delay_ms / 1000 * rate)
        data[idx, ch] = gain
        # cheap one-pole lowpass for the far ear: smear the impulse
        if not same_side:
            k = int(0.4 * abs(lateral) * 24)
            for i in range(1, k + 1):
                if idx + i < length:
                    data[idx + i, ch] = gain * (0.55 ** i)

    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    out = LIBRARY_DIR / 'demo-hrir-14ch.wav'
    _sf.write(str(out), data, rate, subtype='FLOAT')
    return out
