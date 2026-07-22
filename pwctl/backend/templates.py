"""Filter-chain config templates.

Each template renders a complete standalone PipeWire config (module context +
filter-chain module) from a ChainMeta, so it can be run as its own
`pipewire -c <file>.conf` process and restarted independently.
"""

from __future__ import annotations

from .. import spa_json

# HeSuVi 14-channel WAV layout: channel index per (speaker, ear)
HESUVI = {
    ('FL', 'L'): 0, ('FL', 'R'): 1, ('SL', 'L'): 2, ('SL', 'R'): 3,
    ('RL', 'L'): 4, ('RL', 'R'): 5, ('FC', 'L'): 6, ('FR', 'R'): 7,
    ('FR', 'L'): 8, ('SR', 'R'): 9, ('SR', 'L'): 10, ('RR', 'R'): 11,
    ('RR', 'L'): 12, ('FC', 'R'): 13,
}

SPEAKER_AZ_EL = {  # for SOFA spatializer templates
    'FL': (30.0, 0.0), 'FR': (330.0, 0.0), 'FC': (0.0, 0.0),
    'LFE': (0.0, -30.0), 'RL': (150.0, 0.0), 'RR': (210.0, 0.0),
    'SL': (90.0, 0.0), 'SR': (270.0, 0.0),
}


def _base_conf(modules_args: dict) -> dict:
    """Standalone config skeleton mirroring /usr/share/pipewire/filter-chain.conf."""
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
            {'name': 'libpipewire-module-filter-chain',
             'args': modules_args},
        ],
    }


def _sink_props(meta, channels, positions):
    cap = {
        'node.name': f'effect_input.pwctl.{meta.id}',
        'media.class': 'Audio/Sink',
        'audio.channels': channels,
        'audio.position': positions,
    }
    play = {
        'node.name': f'effect_output.pwctl.{meta.id}',
        'node.passive': True,
        'audio.channels': 2,
        'audio.position': ['FL', 'FR'],
    }
    if meta.target:
        play['target.object'] = meta.target
        play['node.dont-reconnect'] = False
    return cap, play


def _source_props(meta):
    cap = {'node.name': f'capture.pwctl.{meta.id}', 'node.passive': True}
    if meta.target:
        cap['target.object'] = meta.target
    play = {
        'node.name': f'pwctl.{meta.id}',
        'media.class': 'Audio/Source',
        'audio.position': ['FL', 'FR'],
    }
    return cap, play


def _hesuvi_surround(meta, speakers):
    """Convolver virtual-surround graph from a 14ch HeSuVi WAV."""
    hrir = meta.hrir or 'MISSING-HRIR.wav'
    gain = meta.params.get('gain', 1.0)
    nodes, links, inputs = [], [], []
    mix_l, mix_r = [], []

    for spk in speakers:
        nodes.append({'type': 'builtin', 'label': 'copy', 'name': f'copy{spk}'})
        inputs.append(f'copy{spk}:In')
        src = 'FC' if spk == 'LFE' else spk       # LFE rendered as center
        for ear in ('L', 'R'):
            conv = f'conv{spk}_{ear}'
            nodes.append({
                'type': 'builtin', 'label': 'convolver', 'name': conv,
                'config': {'filename': hrir,
                           'channel': HESUVI[(src, ear)], 'gain': gain}})
            links.append({'output': f'copy{spk}:Out', 'input': f'{conv}:In'})
            (mix_l if ear == 'L' else mix_r).append(f'{conv}:Out')

    for name, srcs in (('mixL', mix_l), ('mixR', mix_r)):
        nodes.append({'type': 'builtin', 'label': 'mixer', 'name': name})
        for i, s in enumerate(srcs, 1):
            links.append({'output': s, 'input': f'{name}:In {i}'})

    graph = {'nodes': nodes, 'links': links, 'inputs': inputs,
             'outputs': ['mixL:Out', 'mixR:Out']}
    cap, play = _sink_props(meta, len(speakers), speakers)
    return graph, cap, play


def _per_channel_convolver(meta, channel_map):
    """Simple L/R convolver sink; channel_map: {'FL': ch, 'FR': ch}."""
    hrir = meta.hrir or 'MISSING-IR.wav'
    gain = meta.params.get('gain', 1.0)
    nodes, links = [], []
    for spk, ch in channel_map.items():
        nodes.append({'type': 'builtin', 'label': 'convolver',
                      'name': f'conv{spk}',
                      'config': {'filename': hrir, 'channel': ch,
                                 'gain': gain}})
    graph = {'nodes': nodes,
             'inputs': [f'conv{s}:In' for s in channel_map],
             'outputs': [f'conv{s}:Out' for s in channel_map]}
    cap, play = _sink_props(meta, 2, ['FL', 'FR'])
    return graph, cap, play


def _passthrough_sink(meta, speakers):
    """Plain multichannel virtual sink: apps see all channels, PipeWire's
    channelmix folds them down to whatever the target device offers."""
    nodes = [{'type': 'builtin', 'label': 'copy', 'name': f'copy{s}'}
             for s in speakers]
    graph = {'nodes': nodes,
             'inputs': [f'copy{s}:In' for s in speakers],
             'outputs': [f'copy{s}:Out' for s in speakers]}
    cap, play = _sink_props(meta, len(speakers), speakers)
    play['audio.channels'] = len(speakers)
    play['audio.position'] = speakers
    return graph, cap, play


def _true_stereo(meta):
    """4ch true-stereo IR: channels LL, LR, RL, RR."""
    hrir = meta.hrir or 'MISSING-IR.wav'
    gain = meta.params.get('gain', 1.0)
    nodes = [
        {'type': 'builtin', 'label': 'copy', 'name': 'copyL'},
        {'type': 'builtin', 'label': 'copy', 'name': 'copyR'},
    ]
    for name, ch in (('convLL', 0), ('convLR', 1), ('convRL', 2), ('convRR', 3)):
        nodes.append({'type': 'builtin', 'label': 'convolver', 'name': name,
                      'config': {'filename': hrir, 'channel': ch, 'gain': gain}})
    nodes += [{'type': 'builtin', 'label': 'mixer', 'name': 'mixL'},
              {'type': 'builtin', 'label': 'mixer', 'name': 'mixR'}]
    links = [
        {'output': 'copyL:Out', 'input': 'convLL:In'},
        {'output': 'copyL:Out', 'input': 'convLR:In'},
        {'output': 'copyR:Out', 'input': 'convRL:In'},
        {'output': 'copyR:Out', 'input': 'convRR:In'},
        {'output': 'convLL:Out', 'input': 'mixL:In 1'},
        {'output': 'convRL:Out', 'input': 'mixL:In 2'},
        {'output': 'convLR:Out', 'input': 'mixR:In 1'},
        {'output': 'convRR:Out', 'input': 'mixR:In 2'},
    ]
    graph = {'nodes': nodes, 'links': links,
             'inputs': ['copyL:In', 'copyR:In'],
             'outputs': ['mixL:Out', 'mixR:Out']}
    cap, play = _sink_props(meta, 2, ['FL', 'FR'])
    return graph, cap, play


def _sofa_spatializer(meta, speakers):
    hrir = meta.hrir or 'MISSING.sofa'
    gain = meta.params.get('gain', 1.0)
    nodes, links, inputs = [], [], []
    mix_l, mix_r = [], []
    for spk in speakers:
        az, el = SPEAKER_AZ_EL[spk]
        sp = f'sp{spk}'
        nodes.append({
            'type': 'sofa', 'label': 'spatializer', 'name': sp,
            'config': {'filename': hrir, 'gain': gain},
            'control': {'Azimuth': az, 'Elevation': el, 'Radius': 1.0}})
        inputs.append(f'{sp}:In')
        mix_l.append(f'{sp}:Out L')
        mix_r.append(f'{sp}:Out R')
    for name, srcs in (('mixL', mix_l), ('mixR', mix_r)):
        nodes.append({'type': 'builtin', 'label': 'mixer', 'name': name})
        for i, s in enumerate(srcs, 1):
            links.append({'output': s, 'input': f'{name}:In {i}'})
    graph = {'nodes': nodes, 'links': links, 'inputs': inputs,
             'outputs': ['mixL:Out', 'mixR:Out']}
    cap, play = _sink_props(meta, len(speakers), speakers)
    return graph, cap, play


def _crossfeed(meta):
    """Chu-Moy-style headphone crossfeed with builtin filters only."""
    cross_gain = meta.params.get('cross_gain', 0.35)
    cutoff = meta.params.get('cutoff', 700)
    delay_s = meta.params.get('cross_delay', 0.0003)
    nodes = [
        {'type': 'builtin', 'label': 'copy', 'name': 'copyL'},
        {'type': 'builtin', 'label': 'copy', 'name': 'copyR'},
        {'type': 'builtin', 'label': 'bq_lowpass', 'name': 'lpL',
         'control': {'Freq': cutoff}},
        {'type': 'builtin', 'label': 'bq_lowpass', 'name': 'lpR',
         'control': {'Freq': cutoff}},
        {'type': 'builtin', 'label': 'delay', 'name': 'dlL',
         'config': {'max-delay': 0.01}, 'control': {'Delay (s)': delay_s}},
        {'type': 'builtin', 'label': 'delay', 'name': 'dlR',
         'config': {'max-delay': 0.01}, 'control': {'Delay (s)': delay_s}},
        {'type': 'builtin', 'label': 'mixer', 'name': 'mixL',
         'control': {'Gain 1': 0.9, 'Gain 2': cross_gain}},
        {'type': 'builtin', 'label': 'mixer', 'name': 'mixR',
         'control': {'Gain 1': 0.9, 'Gain 2': cross_gain}},
    ]
    links = [
        {'output': 'copyL:Out', 'input': 'mixL:In 1'},
        {'output': 'copyR:Out', 'input': 'mixR:In 1'},
        {'output': 'copyL:Out', 'input': 'lpL:In'},
        {'output': 'copyR:Out', 'input': 'lpR:In'},
        {'output': 'lpL:Out', 'input': 'dlL:In'},
        {'output': 'lpR:Out', 'input': 'dlR:In'},
        {'output': 'dlL:Out', 'input': 'mixR:In 2'},
        {'output': 'dlR:Out', 'input': 'mixL:In 2'},
    ]
    graph = {'nodes': nodes, 'links': links,
             'inputs': ['copyL:In', 'copyR:In'],
             'outputs': ['mixL:Out', 'mixR:Out']}
    cap, play = _sink_props(meta, 2, ['FL', 'FR'])
    return graph, cap, play


def _parametric_eq(meta):
    """Stereo parametric EQ, optionally loading an AutoEq/SquigLink file."""
    cfg = {}
    if meta.params.get('eq_file'):
        cfg['filename'] = meta.params['eq_file']
    else:
        cfg['filters'] = [
            {'type': 'bq_lowshelf', 'freq': 105, 'gain': 0.0, 'q': 0.7},
            {'type': 'bq_peaking', 'freq': 1000, 'gain': 0.0, 'q': 1.0},
            {'type': 'bq_highshelf', 'freq': 10000, 'gain': 0.0, 'q': 0.7},
        ]
    nodes = [{'type': 'builtin', 'label': 'param_eq', 'name': 'eq',
              'config': cfg}]
    graph = {'nodes': nodes,
             'inputs': ['eq:In 1', 'eq:In 2'],
             'outputs': ['eq:Out 1', 'eq:Out 2']}
    cap, play = _sink_props(meta, 2, ['FL', 'FR'])
    return graph, cap, play


def _bass_boost(meta):
    gain = meta.params.get('bass_gain', 6.0)
    freq = meta.params.get('bass_freq', 100)
    nodes, links = [], []
    for ch in ('L', 'R'):
        nodes.append({'type': 'builtin', 'label': 'bq_lowshelf',
                      'name': f'bass{ch}',
                      'control': {'Freq': freq, 'Gain': gain, 'Q': 0.707}})
    graph = {'nodes': nodes,
             'inputs': ['bassL:In', 'bassR:In'],
             'outputs': ['bassL:Out', 'bassR:Out']}
    cap, play = _sink_props(meta, 2, ['FL', 'FR'])
    return graph, cap, play


def _rnnoise(meta):
    """Noise-cancelling microphone source (needs noise-suppression-for-voice)."""
    vad = meta.params.get('vad_threshold', 50.0)
    nodes = [{
        'type': 'ladspa', 'name': 'rnnoise',
        'plugin': meta.params.get(
            'rnnoise_plugin', '/usr/lib/ladspa/librnnoise_ladspa.so'),
        'label': 'noise_suppressor_stereo',
        'control': {'VAD Threshold (%)': vad, 'VAD Grace Period (ms)': 200,
                    'Retroactive VAD Grace (ms)': 0},
    }]
    graph = {'nodes': nodes,
             'inputs': ['rnnoise:Input 1', 'rnnoise:Input 2'],
             'outputs': ['rnnoise:Output 1', 'rnnoise:Output 2']}
    cap, play = _source_props(meta)
    return graph, cap, play


def _effect_rack(meta):
    """Series chain of LADSPA/LV2 plugins as a stereo insert sink.

    params['plugins']: list of dicts with type/plugin/label/name/audio_in/
    audio_out (see backend.plugins.Plugin).  Stereo plugins run as one
    instance, mono plugins as an L/R pair; a single plugin with unknown
    ports is emitted as a bare one-node graph (filter-chain infers ports).
    """
    specs = meta.params.get('plugins') or []
    if not specs:
        raise ValueError('effect rack has no plugins')

    def node_def(spec, name):
        d = {'type': spec['type'], 'name': name, 'plugin': spec['plugin']}
        if spec['type'] == 'ladspa':
            d['label'] = spec['label']
        controls = (spec.get('controls') or {})
        if controls:
            d['control'] = dict(controls)
        return d

    if len(specs) == 1 and not (specs[0].get('audio_in')
                                and specs[0].get('audio_out')):
        graph = {'nodes': [node_def(specs[0], 'fx0')]}
        cap, play = _sink_props(meta, 2, ['FL', 'FR'])
        return graph, cap, play

    nodes, links = [], []
    stages = []                    # [(in_ports, out_ports)] with node prefix
    for i, spec in enumerate(specs):
        ins, outs = spec.get('audio_in') or [], spec.get('audio_out') or []
        if not ins or not outs:
            raise ValueError(
                f"{spec.get('name', spec['plugin'])}: audio ports unknown — "
                'it can only be used alone in a rack')
        if len(ins) >= 2 and len(outs) >= 2:
            name = f'fx{i}'
            nodes.append(node_def(spec, name))
            stages.append(([f'{name}:{ins[0]}', f'{name}:{ins[1]}'],
                           [f'{name}:{outs[0]}', f'{name}:{outs[1]}']))
        else:                      # mono: run an L/R pair
            nl, nr = f'fx{i}L', f'fx{i}R'
            nodes.append(node_def(spec, nl))
            nodes.append(node_def(spec, nr))
            stages.append(([f'{nl}:{ins[0]}', f'{nr}:{ins[0]}'],
                           [f'{nl}:{outs[0]}', f'{nr}:{outs[0]}']))
    for (_, prev_out), (next_in, _) in zip(stages, stages[1:]):
        links.append({'output': prev_out[0], 'input': next_in[0]})
        links.append({'output': prev_out[1], 'input': next_in[1]})
    graph = {'nodes': nodes, 'links': links,
             'inputs': stages[0][0], 'outputs': stages[-1][1]}
    cap, play = _sink_props(meta, 2, ['FL', 'FR'])
    return graph, cap, play


TEMPLATES = {
    'plain-71-sink': {
        'title': 'Virtual 7.1 Sink (plain downmix)',
        'desc': 'Gives apps a full 8-channel device; the signal is folded '
                'down to the real output by PipeWire using your channel-mix '
                'settings. No HRIR — for surround content on plain '
                'headphones/stereo, or as an input for other chains.',
        'needs': None,
        'build': lambda m: _passthrough_sink(
            m, ['FL', 'FR', 'FC', 'LFE', 'RL', 'RR', 'SL', 'SR']),
    },
    'virtual-surround-7.1': {
        'title': 'Virtual Surround 7.1 → Headphones',
        'desc': 'Binaural 7.1 downmix using a 14-channel HeSuVi HRIR. '
                'Games/movies see an 8-channel sink.',
        'needs': 'hesuvi',
        'build': lambda m: _hesuvi_surround(
            m, ['FL', 'FR', 'FC', 'LFE', 'RL', 'RR', 'SL', 'SR']),
    },
    'virtual-surround-5.1': {
        'title': 'Virtual Surround 5.1 → Headphones',
        'desc': 'Binaural 5.1 downmix using a 14-channel HeSuVi HRIR.',
        'needs': 'hesuvi',
        'build': lambda m: _hesuvi_surround(
            m, ['FL', 'FR', 'FC', 'LFE', 'RL', 'RR']),
    },
    'virtual-surround-stereo': {
        'title': 'Stereo Widener (HRIR front stage)',
        'desc': 'Renders plain stereo through the front-speaker HRIR pair — '
                'speaker-like soundstage on headphones.',
        'needs': 'hesuvi',
        'build': lambda m: _hesuvi_surround(m, ['FL', 'FR']),
    },
    'true-stereo-ir': {
        'title': 'True-Stereo Convolver (4ch IR)',
        'desc': 'Full true-stereo convolution (LL/LR/RL/RR) — reverbs and '
                'room impulse responses.',
        'needs': 'true-stereo',
        'build': _true_stereo,
    },
    'stereo-ir': {
        'title': 'Stereo Convolver (1–2ch IR)',
        'desc': 'Per-channel convolution — cab sims, simple reverbs, EQ IRs. '
                'Mono IRs are applied to both channels.',
        'needs': 'stereo',
        'build': lambda m: _per_channel_convolver(
            m, {'FL': 0, 'FR': 1 if m.hrir_channels != 1 else 0}),
    },
    'sofa-spatializer-7.1': {
        'title': 'SOFA Spatializer 7.1',
        'desc': 'Scientific HRTF rendering from a .sofa file (libmysofa).',
        'needs': 'sofa',
        'build': lambda m: _sofa_spatializer(
            m, ['FL', 'FR', 'FC', 'LFE', 'RL', 'RR', 'SL', 'SR']),
    },
    'sofa-spatializer-5.1': {
        'title': 'SOFA Spatializer 5.1',
        'desc': 'Scientific HRTF rendering from a .sofa file (libmysofa).',
        'needs': 'sofa',
        'build': lambda m: _sofa_spatializer(
            m, ['FL', 'FR', 'FC', 'LFE', 'RL', 'RR']),
    },
    'crossfeed': {
        'title': 'Headphone Crossfeed',
        'desc': 'Chu-Moy-style crossfeed built from delay + lowpass — eases '
                'fatigue from hard-panned stereo. No IR file needed.',
        'needs': None,
        'build': _crossfeed,
    },
    'parametric-eq': {
        'title': 'Parametric EQ (AutoEq)',
        'desc': 'System-wide parametric EQ. Loads AutoEq / Squiglink '
                'ParametricEQ.txt files for your headphones.',
        'needs': None,
        'build': _parametric_eq,
    },
    'bass-boost': {
        'title': 'Bass Boost',
        'desc': 'Low-shelf boost with adjustable frequency and gain.',
        'needs': None,
        'build': _bass_boost,
    },
    'effect-rack': {
        'title': 'Effect Rack (LADSPA / LV2 inserts)',
        'desc': 'A stereo sink that routes audio through a series of '
                'LADSPA/LV2 plugins before it reaches the output device. '
                'Built from the Effects page.',
        'needs': 'plugins',        # created via the Effects page, not the
        #                            generic chain dialog
        'build': _effect_rack,
    },
    'rnnoise-source': {
        'title': 'Noise-Cancelling Microphone',
        'desc': 'RNNoise mic filter (requires the noise-suppression-for-voice '
                'LADSPA plugin).',
        'needs': None,
        'build': _rnnoise,
    },
}


def render(meta) -> str:
    """Render full standalone conf text for a chain."""
    tpl = TEMPLATES[meta.template]
    graph, cap, play = tpl['build'](meta)
    args = {
        'node.description': meta.name,
        'media.name': meta.name,
        'filter.graph': graph,
        'capture.props': cap,
        'playback.props': play,
    }
    conf = _base_conf(args)
    header = (f'{meta.name}\nGenerated by PipeWire Controller '
              f'(template: {meta.template}). Do not edit by hand.')
    return spa_json.dumps(conf, header=header)
