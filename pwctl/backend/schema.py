"""Declarative schema for every config option the app exposes.

Each Setting maps one PipeWire config key to a UI row.  `kind`:
  bool | enum | int | float | rates (multi-select of sample rates)
`conf` + `section` say where the persistent override lives; the UI writes it
via config.set_override and flags the matching service for restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

RATES = [44100, 48000, 88200, 96000, 176400, 192000]
QUANTA = [16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192]
# Hard-limit ceiling goes higher than the everyday buffer sizes: PipeWire
# clamps quantum-limit to 65536, and 8192 is only ~43 ms at 192 kHz.
QUANTUM_LIMITS = [1024, 2048, 4096, 8192, 16384, 32768]


@dataclass
class Setting:
    key: str
    title: str
    subtitle: str
    kind: str                      # bool | enum | int | float | rates
    default: object
    conf: str = 'pipewire.conf'
    section: str = 'context.properties'
    choices: list = field(default_factory=list)
    min: float = 0
    max: float = 0
    step: float = 1
    restart: str = 'pipewire'      # pipewire | pulse | wireplumber
    advanced: bool = False         # only shown when the Advanced toggle is on
    custom: bool = False           # enum also accepts a typed value (Custom…)


PIPEWIRE_CLOCK = [
    Setting('default.clock.rate', 'Default sample rate',
            'Graph rate when no stream forces one. 48 kHz suits most systems; '
            'match your DAC for bit-perfect playback.',
            'enum', 48000, choices=RATES),
    Setting('default.clock.allowed-rates', 'Allowed sample rates',
            'Rates the graph may switch to on demand (e.g. for hi-res files). '
            'Empty = locked to the default rate.',
            'rates', []),
    Setting('default.clock.quantum', 'Default quantum (buffer size)',
            'Frames per processing cycle. Lower = less latency, more CPU/xrun '
            'risk. 1024 ≈ 21 ms at 48 kHz. Pick Custom… for a value off the '
            'list.',
            'enum', 1024, choices=QUANTA, custom=True),
    Setting('default.clock.min-quantum', 'Minimum quantum',
            'Smallest buffer a client may request. Pro-audio apps go to 32 or '
            'below; raise it if you hear crackles.',
            'enum', 32, choices=QUANTA[:7], custom=True),
    Setting('default.clock.max-quantum', 'Maximum quantum',
            'Largest buffer allowed. Higher saves power for music playback.',
            'enum', 2048, choices=QUANTA[4:], custom=True),
    Setting('clock.power-of-two-quantum', 'Power-of-two quantum',
            'Round quantums down to a power of two — keeps DSP fast.',
            'bool', True),
]

PIPEWIRE_ADVANCED = [
    Setting('default.clock.quantum-limit', 'Quantum hard limit',
            'Absolute ceiling for any buffer size in the graph (PipeWire caps '
            'this at 65536). Measured in frames, so its latency shrinks at high '
            'rates — 8192 is ~171 ms at 48 kHz but only ~43 ms at 192 kHz. Raise '
            'it for high-samplerate or offline-processing setups. Note: 8192 was '
            "JACK's own limit, so some JACK clients dislike larger buffers.",
            'enum', 8192, choices=QUANTUM_LIMITS, advanced=True),
    Setting('mem.allow-mlock', 'Lock realtime memory',
            'Pin audio buffers in RAM so they can never be swapped out. '
            'Disable only on very low-memory systems.',
            'bool', True, advanced=True),
    Setting('cpu.zero.denormals', 'Zero denormal floats',
            'Flush denormals to zero in the audio thread — prevents rare CPU '
            'spikes with some plugins (recommended when using filter chains).',
            'bool', False, advanced=True),
    Setting('loop.rt-prio', 'Realtime priority',
            'Priority of audio data threads. -1 = use module default (88). '
            '0 disables realtime scheduling.',
            'int', -1, min=-1, max=99, advanced=True),
    Setting('link.max-buffers', 'Max buffers per link',
            'Buffers a link may queue — this mostly matters for video streams. '
            'Audio needs very few (JACK runs fine on 1); higher values just use '
            'more memory. 16 is plenty.',
            'enum', 16, choices=[1, 8, 16, 32, 64], advanced=True),
    Setting('settings.check-quantum', 'Strict quantum check',
            'Refuse metadata quantum changes outside min/max bounds.',
            'bool', False, advanced=True),
    Setting('settings.check-rate', 'Strict rate check',
            'Refuse metadata rate changes not in the allowed-rates list.',
            'bool', False, advanced=True),
    Setting('log.level', 'Daemon log level',
            '0 errors only … 5 trace. Level 3+ is useful when debugging '
            'devices; keep at 2 normally.',
            'int', 2, min=0, max=5, advanced=True),
]

# client.conf → native apps; pipewire-pulse.conf → PulseAudio apps.
# The UI writes each of these to BOTH files so behaviour stays consistent.
STREAM = [
    Setting('resample.quality', 'Resampler quality',
            'Speex-style resampler quality (0–14). 4 is the balanced default; '
            '10+ for audiophile listening at ~2× CPU; 14 is near-transparent.',
            'int', 4, conf='client.conf', section='stream.properties',
            min=0, max=14, restart='pulse'),
    Setting('resample.disable', 'Disable resampling',
            'Never resample — streams must already match the graph rate. '
            'Only sensible with a locked rate and matching sources.',
            'bool', False, conf='client.conf', section='stream.properties',
            restart='pulse', advanced=True),
    Setting('channelmix.upmix', 'Upmix stereo → surround',
            'Expand stereo content to fill surround outputs.',
            'bool', True, conf='client.conf', section='stream.properties',
            restart='pulse'),
    Setting('channelmix.upmix-method', 'Upmix method',
            'psd derives ambience psychoacoustically (best); simple copies '
            'channels; none only fills silence.',
            'enum', 'psd', conf='client.conf', section='stream.properties',
            choices=['none', 'simple', 'psd'], restart='pulse'),
    Setting('channelmix.lfe-cutoff', 'LFE crossover (Hz)',
            'Send frequencies below this to the subwoofer when upmixing. '
            '0 disables; 120 Hz is typical for THX-style bass management.',
            'float', 0, conf='client.conf', section='stream.properties',
            min=0, max=300, step=10, restart='pulse'),
    Setting('channelmix.mix-lfe', 'Fold LFE into mains',
            'Mix the LFE channel into front speakers when the output has no '
            'subwoofer.',
            'bool', True, conf='client.conf', section='stream.properties',
            restart='pulse'),
    Setting('channelmix.normalize', 'Normalize downmix',
            'Scale channel volumes to avoid clipping when downmixing surround '
            'to stereo.',
            'bool', False, conf='client.conf', section='stream.properties',
            restart='pulse', advanced=True),
    Setting('monitor.channel-volumes', 'Monitor follows volume',
            'Monitor ports reflect the node volume instead of raw signal.',
            'bool', False, conf='client.conf', section='stream.properties',
            restart='pulse', advanced=True),
    Setting('channelmix.fc-cutoff', 'Center extraction cutoff (Hz)',
            'When upmixing, frequencies below this go to the front-center '
            'speaker. 0 disables; 12000 is a good start for dialogue clarity.',
            'float', 0, conf='client.conf', section='stream.properties',
            min=0, max=20000, step=500, restart='pulse', advanced=True),
    Setting('channelmix.rear-delay', 'Rear ambience delay (ms)',
            'Delay applied to upmixed rear channels — adds depth. 12 ms is '
            'the psychoacoustic sweet spot; 0 disables.',
            'float', 0, conf='client.conf', section='stream.properties',
            min=0, max=50, step=1, restart='pulse', advanced=True),
    Setting('channelmix.stereo-widen', 'Stereo widen',
            'Subtracts a little cross-talk to widen the front stage when '
            'upmixing. 0 = off, keep below 0.5.',
            'float', 0, conf='client.conf', section='stream.properties',
            min=0, max=1, step=0.1, restart='pulse', advanced=True),
    Setting('channelmix.hilbert-taps', 'Rear phase-shift taps',
            'Apply a Hilbert transform to upmixed rear channels (0 = off, '
            'try 63). Creates a more diffuse, "wrap-around" ambience.',
            'int', 0, conf='client.conf', section='stream.properties',
            min=0, max=128, restart='pulse', advanced=True),
    Setting('channelmix.disable', 'Disable channel mixing',
            'Never remix channels — streams only play if their channel '
            'layout exactly matches the device. Breaks upmix/downmix.',
            'bool', False, conf='client.conf', section='stream.properties',
            restart='pulse', advanced=True),
]

WIREPLUMBER = [
    # handled through config.read_wp_toggles / write_wp_toggles
    ('disable_suspend', 'Never suspend devices',
     'Keep ALSA devices always active. Fixes pops/clicks and cut-off first '
     'seconds on amps and HDMI that power down between sounds.'),
    ('sbc_xq', 'Bluetooth SBC-XQ',
     'Higher-bitrate SBC for A2DP — noticeably better than plain SBC when '
     'the headset lacks AAC/aptX/LDAC.'),
    ('msbc', 'Bluetooth mSBC headset mic',
     'Wideband (16 kHz) speech instead of old 8 kHz HFP when using the mic.'),
    ('bt_hw_volume', 'Bluetooth hardware volume',
     'Use AVRCP absolute volume on the device instead of software attenuation.'),
    ('bt_autoswitch', 'Auto-switch to headset profile',
     'Jump to the (low-fidelity) headset profile automatically when an app '
     'opens the Bluetooth mic.'),
    ('alsa_headroom', 'Extra ALSA headroom',
     'Reserve 1024 extra frames in the device buffer — the classic fix for '
     'crackling USB audio interfaces.'),
]

RESTART_LABEL = {
    'pipewire': 'PipeWire',
    'pulse': 'PipeWire-Pulse',
    'wireplumber': 'WirePlumber',
}
