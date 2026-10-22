"""A short test sound on one output, for the Devices page's Test button.

Three things measured on 2026-10-22 shape this
(`docs.local/probes/2026-10-22-devices-ui/`):

* **Target by `object.serial`.**  `pw-play --target` resolves a number as a
  serial, not an object id, and the two drift apart once ids are recycled
  (a profile switch recycles them).
* **WirePlumber remembers a stream by its app name.**  This machine had a
  saved `application.name:pw-play` target pointing at a device that no longer
  exists, which would have sent an untargeted test anywhere.  The stream here
  sets `state.restore-props/target = false`, which per WirePlumber's
  `state-stream.lua` gates the save as well as the restore, so a test neither
  inherits nor leaves anything behind (measured: no new entry).
* **It must not look like an app.**  The node is named `pwctl.test-sound`,
  so `pw.Stream.is_app` (which skips `pwctl.` names) keeps it off the very
  card it is playing on.

`pw-play` ships in every PipeWire install we support, but in a different
package per distro (`pipewire-utils`, `pipewire-bin`), so it is looked up at
call time, never assumed (the house rule).
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import tempfile
import wave

RATE = 48000
NODE_NAME = 'pwctl.test-sound'
PROPS = ('{ node.name = "%s" media.name = "Test sound" media.role = "Test" '
         'state.restore-props = false state.restore-target = false }'
         % NODE_NAME)
MISSING = ('Test sounds need pw-play, which is part of PipeWire\'s tools '
           '(pipewire-utils on Fedora, pipewire-bin on Debian and Ubuntu).')


def available() -> bool:
    return shutil.which('pw-play') is not None


def _chime() -> bytes:
    """Two soft notes, left then right, 16-bit stereo: about 0.9 s.

    Left-then-right says which side is which without a second button, and a
    soft attack with a long decay never clicks.  Quiet on purpose (peak
    -12 dBFS): a test should not startle anyone whose volume is up.
    """
    frames = []
    notes = ((0.0, 0.42, 659.25, 0), (0.30, 0.60, 987.77, 1))   # E5, B5
    total = int(RATE * 0.95)
    peak = 10 ** (-12 / 20) * 32767
    for i in range(total):
        t = i / RATE
        out = [0.0, 0.0]
        for start, length, freq, side in notes:
            dt = t - start
            if 0 <= dt < length:
                attack = min(1.0, dt / 0.012)
                decay = math.exp(-dt * 6.0)
                tail = min(1.0, (length - dt) / 0.03)
                v = math.sin(2 * math.pi * freq * dt) * attack * decay * tail
                out[side] += v
                out[1 - side] += v * 0.18          # a little of it centred
        frames.append(struct.pack('<hh', int(out[0] * peak),
                                  int(out[1] * peak)))
    return b''.join(frames)


def _wav_path() -> str:
    """The chime as a file in the runtime dir, written once per session."""
    base = os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir()
    path = os.path.join(base, 'pwctl-test-sound.wav')
    if not os.path.exists(path):
        tmp = path + '.tmp'
        with wave.open(tmp, 'wb') as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(_chime())
        os.replace(tmp, path)
    return path


def play(serial: int, timeout: float = 6.0) -> tuple[bool, str]:
    """Play the chime on the node with this serial and wait for it.

    Returns (ok, message); the message is for a toast when it did not work.
    Run it off the UI thread: it waits for pw-play to finish (~1 s).
    """
    exe = shutil.which('pw-play')
    if exe is None:
        return False, MISSING
    try:
        path = _wav_path()
    except OSError as e:
        return False, f'Could not write the test sound: {e}'
    try:
        r = subprocess.run([exe, '--target', str(int(serial)), '-P', PROPS,
                            path], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, 'The test sound did not finish playing'
    if r.returncode != 0:
        err = (r.stderr or '').strip().splitlines()
        return False, 'pw-play failed' + (f': {err[-1]}' if err else '')
    return True, ''
