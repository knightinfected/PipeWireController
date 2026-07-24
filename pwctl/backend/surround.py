"""Surround setup: sound-card profiles, layout definitions, speaker test tones."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .pw import pw_dump
from .system import run

# Channel orders follow the standard WAV/WAVEX layout for each count, so a
# generated test file plays each position where PipeWire expects it.
LAYOUTS = [
    ('stereo', 'Stereo 2.0', ['FL', 'FR']),
    ('2.1', 'Stereo 2.1', ['FL', 'FR', 'LFE']),
    ('quad', 'Quadraphonic 4.0', ['FL', 'FR', 'RL', 'RR']),
    ('5.1', 'Surround 5.1', ['FL', 'FR', 'FC', 'LFE', 'RL', 'RR']),
    ('7.1', 'Surround 7.1', ['FL', 'FR', 'FC', 'LFE', 'RL', 'RR', 'SL', 'SR']),
]

# channel-mix keys managed by the Surround page and device presets
UPMIX_KEYS = [
    'channelmix.upmix', 'channelmix.upmix-method', 'channelmix.lfe-cutoff',
    'channelmix.mix-lfe', 'channelmix.fc-cutoff', 'channelmix.rear-delay',
    'channelmix.stereo-widen', 'channelmix.hilbert-taps',
]

SPEAKER_NAMES = {
    'FL': 'Front Left', 'FR': 'Front Right', 'FC': 'Center',
    'LFE': 'Subwoofer', 'RL': 'Rear Left', 'RR': 'Rear Right',
    'SL': 'Side Left', 'SR': 'Side Right',
}

# keywords used to rank a card profile for a chosen layout
PROFILE_HINTS = {
    'stereo': ['stereo'],
    '2.1': ['stereo'],
    'quad': ['surround 4.0', 'quad', 'surround'],
    '5.1': ['surround 5.1', '5.1', 'surround'],
    '7.1': ['surround 7.1', '7.1', 'surround'],
}


def layout(key):
    return next((l for l in LAYOUTS if l[0] == key), LAYOUTS[0])


@dataclass
class Card:
    id: int
    name: str
    description: str
    profiles: list = field(default_factory=list)   # [(idx, desc, available)]
    active_profile: int | None = None


def list_cards(dump=None, outputs_only=True) -> list[Card]:
    """Audio devices with their profiles.

    outputs_only=True (surround setup) keeps only output-capable profiles;
    False (dashboard configuration switcher) returns every profile the card
    offers, exactly like pavucontrol's Configuration tab.
    """
    dump = dump if dump is not None else pw_dump()
    cards = []
    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Device':
            continue
        info = obj.get('info') or {}
        props = info.get('props') or {}
        if props.get('media.class') != 'Audio/Device':
            continue
        params = info.get('params') or {}
        profiles = [(p.get('index'), p.get('description', p.get('name', '?')),
                     p.get('available', 'unknown'))
                    for p in (params.get('EnumProfile') or [])
                    if not (outputs_only
                            and (p.get('name') or '').startswith('input:'))]
        active = next((p.get('index') for p in (params.get('Profile') or [])),
                      None)
        cards.append(Card(id=obj['id'], name=props.get('device.name', ''),
                          description=props.get('device.description',
                                                props.get('device.name', '')),
                          profiles=profiles, active_profile=active))
    return cards


def suggest_profile(card: Card, layout_key: str) -> int | None:
    """Best-matching profile index for a layout (available ones first)."""
    hints = PROFILE_HINTS.get(layout_key, [])
    best, best_score = None, -1
    for idx, desc, available in card.profiles:
        d = desc.lower()
        score = 0
        for rank, hint in enumerate(hints):
            if hint in d:
                score = 100 - rank * 10
                break
        if score == 0:
            continue
        if available == 'yes':
            score += 5
        elif available == 'no':
            score -= 50
        if score > best_score:
            best, best_score = idx, score
    return best


def set_profile(device_id: int, profile_index: int) -> bool:
    rc, _, _ = run(['wpctl', 'set-profile', str(device_id),
                    str(profile_index)])
    return rc == 0


# ------------------------------------------------------------- test tones --

def play_test_tone(target_node_id: int | None, channel_index: int,
                   positions: list[str], rate=48000, duration=1.2):
    """Play a tone on one channel of a multichannel file via pw-play.

    Regular speakers get a 550 Hz burst, the LFE position a 60 Hz sine.
    Fire-and-forget; the file lives in the system tmp dir.
    """
    import numpy as np
    import soundfile as sf

    n = int(rate * duration)
    t = np.arange(n) / rate
    freq = 60.0 if positions[channel_index] == 'LFE' else 550.0
    tone = 0.5 * np.sin(2 * np.pi * freq * t)
    # fade in/out so it never clicks
    fade = int(rate * 0.03)
    env = np.ones(n)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    data = np.zeros((n, len(positions)), dtype=np.float32)
    data[:, channel_index] = tone * env

    path = Path(tempfile.gettempdir()) / f'pwctl-tone-{positions[channel_index]}.wav'
    sf.write(path, data, rate)
    cmd = ['pw-play']
    if target_node_id is not None:
        cmd += ['--target', str(target_node_id)]
    cmd.append(str(path))
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
