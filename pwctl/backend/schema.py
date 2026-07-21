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
            'risk. 1024 ≈ 21 ms at 48 kHz.',
            'enum', 1024, choices=QUANTA),
    Setting('default.clock.min-quantum', 'Minimum quantum',
            'Smallest buffer a client may request. Pro-audio apps go to 32 or '
            'below; raise it if you hear crackles.',
            'enum', 32, choices=QUANTA[:7]),
    Setting('default.clock.max-quantum', 'Maximum quantum',
            'Largest buffer allowed. Higher saves power for music playback.',
            'enum', 2048, choices=QUANTA[4:]),
    Setting('clock.power-of-two-quantum', 'Power-of-two quantum',
            'Round quantums down to a power of two — keeps DSP fast.',
            'bool', True),
]

PIPEWIRE_ADVANCED = [
    Setting('mem.allow-mlock', 'Lock realtime memory',
            'Pin audio buffers in RAM so they can never be swapped out. '
            'Disable only on very low-memory systems.',
            'bool', True),
    Setting('cpu.zero.denormals', 'Zero denormal floats',
            'Flush denormals to zero in the audio thread — prevents rare CPU '
            'spikes with some plugins (recommended when using filter chains).',
            'bool', False),
    Setting('loop.rt-prio', 'Realtime priority',
            'Priority of audio data threads. -1 = use module default (88). '
            '0 disables realtime scheduling.',
            'int', -1, min=-1, max=99),
    Setting('link.max-buffers', 'Max buffers per link',
            '16 works for pure PipeWire graphs; 64 is needed for old JACK apps.',
            'enum', 64, choices=[16, 32, 64]),
    Setting('settings.check-quantum', 'Strict quantum check',
            'Refuse metadata quantum changes outside min/max bounds.',
            'bool', False),
    Setting('settings.check-rate', 'Strict rate check',
            'Refuse metadata rate changes not in the allowed-rates list.',
            'bool', False),
    Setting('log.level', 'Daemon log level',
            '0 errors only … 5 trace. Level 3+ is useful when debugging '
            'devices; keep at 2 normally.',
            'int', 2, min=0, max=5),
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
            restart='pulse'),
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
            restart='pulse'),
    Setting('monitor.channel-volumes', 'Monitor follows volume',
            'Monitor ports reflect the node volume instead of raw signal.',
            'bool', False, conf='client.conf', section='stream.properties',
            restart='pulse'),
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
]

RESTART_LABEL = {
    'pipewire': 'PipeWire',
    'pulse': 'PipeWire-Pulse',
    'wireplumber': 'WirePlumber',
}
