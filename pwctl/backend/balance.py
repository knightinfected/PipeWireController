"""Per-channel volume arithmetic: master, balance, one channel.

Pure logic, no GTK and no subprocess, so every page computes a write the same
way and a harness can check it without PipeWire.  Values are in the app's
cubic (UI) scale throughout, the same scale as every slider, 0..MAX_VOL.

The one rule all of it serves: **only a channel control changes the balance.**
A master write scales every channel by the same ratio, so a balance the user
set survives every slider, preset and snapshot that moves the level.

DR-002 / DR-005 in the vault hold the design and the measurements.
"""

from __future__ import annotations

MAX_VOL = 1.5           # the sliders' top end (150%), shared with ui.volume
LEVEL_EPS = 1e-4        # channels closer than this count as level

CHANNEL_NAMES = {
    'FL': 'Front left', 'FR': 'Front right', 'FC': 'Centre',
    'LFE': 'Subwoofer', 'RL': 'Rear left', 'RR': 'Rear right',
    'SL': 'Side left', 'SR': 'Side right', 'MONO': 'Mono',
    'RC': 'Rear centre', 'FLC': 'Front left centre',
    'FRC': 'Front right centre', 'TC': 'Top centre',
}


def channel_name(position: str, index: int) -> str:
    """'FL' -> 'Front left'; AUX3 and anything unknown is shown as-is."""
    if not position or position == 'UNK':
        return f'Channel {index + 1}'
    return CHANNEL_NAMES.get(position, position)


def clamp(value: float) -> float:
    return max(0.0, min(MAX_VOL, float(value)))


def is_level(chans) -> bool:
    """Are all channels at the same level?  An empty or mono list is."""
    return not chans or max(chans) - min(chans) < LEVEL_EPS


def master_of(chans) -> float | None:
    """The master level is the loudest channel.

    The same convention pavucontrol and GNOME use, and the only one that keeps
    a master drag honest: scaling from the loudest channel means no channel is
    ever pushed past the value the slider shows.
    """
    return max(chans) if chans else None


def scaled(chans, value: float, shape=None) -> list[float]:
    """Every channel scaled so the loudest lands on `value`.

    `shape` is a remembered non-silent array to scale from when `chans` is all
    zero; without it a master dragged to 0 would forget the balance.
    """
    value = clamp(value)
    base = list(chans)
    if base and max(base) <= 0 and shape:
        base = list(shape)
    if not base:
        return [value]
    top = max(base)
    if top <= 0 or is_level(base):
        # exactly `value`, not c * value / top: a level node must reach wpctl
        # with the argv it always had, and 0.125 * x / x can round to 0.13
        return [value] * len(base)
    return [clamp(c * value / top) for c in base]


def balance_of(chans) -> float:
    """Stereo balance in [-1, +1]: 0 is centred, +1 is hard right."""
    if len(chans) != 2:
        return 0.0
    left, right = chans
    if abs(left - right) < LEVEL_EPS or max(left, right) <= 0:
        return 0.0
    if right > left:
        return 1 - left / right
    return -(1 - right / left)


def with_balance(chans, bal: float) -> list[float]:
    """A stereo pair rebuilt from its loudest channel and a balance."""
    top = max(chans) if chans else 1.0
    bal = max(-1.0, min(1.0, bal))
    if bal >= 0:
        return [top * (1 - bal), top]
    return [top, top * (1 + bal)]


def with_channel(chans, index: int, value: float) -> list[float]:
    out = list(chans)
    if 0 <= index < len(out):
        out[index] = clamp(value)
    return out


class Levels:
    """One row's view of a node's channels, kept between graph reads.

    A row holds one of these so the master control, the balance and the
    channel controls all read and write the same array: a master drag updates
    it, so the channel controls never act on levels from before the drag.
    """

    def __init__(self, chans=(), positions=()):
        self.chans: list[float] = []
        self.positions: list[str] = []
        self._shape: list[float] | None = None
        self.update(chans, positions)

    def update(self, chans, positions=None):
        """Take the graph's values (a refresh, never user input)."""
        self.chans = [float(c) for c in (chans or [])]
        if positions is not None:
            self.positions = list(positions)
        self._remember()

    def _remember(self):
        if self.chans and max(self.chans) > 0:
            self._shape = list(self.chans)

    @property
    def count(self) -> int:
        return len(self.chans)

    @property
    def master(self) -> float | None:
        return master_of(self.chans)

    @property
    def balance(self) -> float:
        return balance_of(self.chans)

    @property
    def level(self) -> bool:
        return is_level(self.chans)

    def name(self, index: int) -> str:
        pos = self.positions[index] if index < len(self.positions) else ''
        return channel_name(pos, index)

    def set_master(self, value: float) -> list[float]:
        self.chans = scaled(self.chans, value, self._shape)
        self._remember()
        return list(self.chans)

    def set_balance(self, bal: float) -> list[float]:
        if self.count == 2:
            self.chans = with_balance(self.chans, bal)
            self._remember()
        return list(self.chans)

    def set_channel(self, index: int, value: float) -> list[float]:
        self.chans = with_channel(self.chans, index, value)
        self._remember()
        return list(self.chans)

    def even_out(self) -> list[float]:
        """Every channel to the master level: the balance reset."""
        top = self.master or 0.0
        self.chans = [top] * self.count
        self._remember()
        return list(self.chans)
