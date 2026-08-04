"""Ready-made signal paths — a strip and its whole chain, in one click.

A template is a recipe, not a new kind of object: loading one builds an
ordinary `paths.Strip` with ordinary stages, which is then dragged, edited and
deleted like anything else on the board.  Nothing here is remembered
afterwards, and no strip ever knows it came from a template.

Two things make the catalog worth having as data rather than as code in the
page:

**Plugin stages have to survive a machine that doesn't have the plugin.**  A
template asks for "a compressor" as an ordered list of patterns, not for one
library path: the first installed plugin that matches wins, and if none does
the stage is *skipped* and reported rather than written half-configured.  A
stage with no plugin makes `build_graph` raise, so the strip would be created
and then refuse to start — much worse than arriving one stage short with a
line of text saying so.  This is also why only plugins with matching input and
output counts (1×1 or 2×2) are ever chosen: those are the two shapes
`paths._effect_lanes` instantiates cleanly across a strip's channels, and a
sidechain plugin's 2-in/1-out would silently leave its second input dangling.

**Equalizer stages are free.**  They compile to builtin biquads, so every
template built only out of them works on any system with PipeWire and nothing
else installed.  The catalog is ordered so those come first.

The order of the list *is* the difficulty curve, from "a mix that feeds my
speakers" to chains people actually run for broadcast; `advanced` only marks
where the second half starts for the empty-state board, and is not a category.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import paths

# --------------------------------------------------------------- matching --


def _leaf(plugin) -> str:
    """The last meaningful word of an id — `…/compressor_stereo`, `ZamCompX2`.

    LADSPA labels from LSP are URLs and LV2 ids are URIs, so the human name is
    usually the only readable part; this gives the patterns a second thing to
    match against for plugins whose name is unhelpful.
    """
    ident = (plugin.label or plugin.plugin or '')
    return ident.rsplit('/', 1)[-1].rsplit(':', 1)[-1]


def usable(plugin) -> bool:
    """Whether a stage can be built from this plugin across any channel count.

    `paths._effect_lanes` instantiates a plugin once per channel (1-in/1-out)
    or once per channel pair (2-in/2-out).  Anything else — a sidechain input,
    a crossover's eighteen outputs — would be wired up wrong, so it is never
    picked automatically even when its name matches perfectly.
    """
    ins, outs = list(plugin.audio_in or []), list(plugin.audio_out or [])
    return bool(ins) and len(ins) == len(outs) and len(ins) in (1, 2)


def find_plugin(installed, patterns):
    """The best installed plugin for an ordered list of patterns.

    Patterns run most-specific first, so a template that would rather have
    Calf's compressor than LSP's says so and still gets *a* compressor on a
    machine with only one of them.  Within one pattern, stereo beats mono
    (fewer instances, and it is what the plugin was voiced for) and the
    shortest name beats the longest, which reliably prefers "Compressor
    Stereo" over "Sidechain Multiband Compressor Stereo x8".
    """
    for pat in patterns:
        rx = re.compile(pat, re.I)
        hits = [p for p in installed if usable(p)
                and (rx.search(p.name or '') or rx.search(_leaf(p)))]
        if hits:
            hits.sort(key=lambda p: (len(p.audio_in) != 2, len(p.name or ''),
                                     p.name or ''))
            return hits[0]
    return None


# ----------------------------------------------------------------- recipe --

@dataclass
class Step:
    """One stage a template wants, before it is matched against this machine.

    An `eq` step is always buildable; an `effect` step is a wish, and
    `patterns` is how it is granted.
    """
    kind: str                      # 'eq' | 'effect'
    name: str
    preamp: float = 0.0
    bands: tuple = ()              # (type, freq, gain, q)
    patterns: tuple = ()           # effect only, most specific first


@dataclass
class Template:
    id: str
    title: str
    blurb: str
    icon: str
    role: str                      # the strip it builds: 'source' | 'mix'
    kind: str = 'app'              # source flavour, see paths.SOURCE_KINDS
    positions: tuple = ('FL', 'FR')
    advanced: bool = False
    steps: tuple = ()
    detail: str = ''               # the longer line, shown in the dialog

    @property
    def chain(self) -> list:
        """Stage names, for drawing the chain on the template's own card."""
        return [s.name for s in self.steps]


def _eq_stage(step: Step) -> dict:
    stage = paths.new_stage('eq', step.name)
    stage['params'] = {
        'preamp': float(step.preamp),
        'bands': [{'on': True, 'type': t, 'freq': float(f),
                   'gain': float(g), 'q': float(q)}
                  for t, f, g, q in step.bands],
    }
    return stage


def build_stages(template: Template, installed) -> tuple[list, list]:
    """(stages, names of the steps this machine cannot provide)."""
    stages, missing = [], []
    for step in template.steps:
        if step.kind == 'eq':
            stages.append(_eq_stage(step))
            continue
        hit = find_plugin(installed, step.patterns)
        if hit is None:
            missing.append(step.name)
            continue
        stages.append(paths.new_stage(
            'effect', step.name, type=hit.type, plugin=hit.plugin,
            label=hit.label, audio_in=list(hit.audio_in),
            audio_out=list(hit.audio_out)))
    return stages, missing


def missing_steps(template: Template, installed) -> list:
    """What `build_stages` would skip — asked before anything is created."""
    return [s.name for s in template.steps
            if s.kind == 'effect' and find_plugin(installed, s.patterns) is None]


# ------------------------------------------------------- plugin shorthand --
# Named once so several templates can ask for "a compressor" and get the same
# answer, and so a distribution that ships only one plugin pack still works.

P_GATE = (r'^Calf Gate$', r'^Gate Stereo$', r'^ZamGateX2$', r'^Gate ',
          r'\bgate\b')
P_COMP = (r'^Calf Compressor$', r'^Compressor Stereo$', r'^ZamCompX2$',
          r'^Compressor ', r'\bcompressor\b')
P_MBCOMP = (r'^Calf Multiband Compressor$', r'^Multiband Compressor Stereo',
            r'^ZaMultiCompX2$', r'multiband compressor')
P_LIMIT = (r'^Calf Limiter$', r'^Limiter Stereo$', r'^ZaMaximX2$',
           r'^Limiter ', r'\blimiter\b')
P_DEESS = (r'^Calf Deesser$', r'de.?esser', r'^Dynamics Processor Stereo$')
P_LOUD = (r'^Loudness Compensator Stereo$', r'loudness compensator',
          r'^Calf Bass Enhancer$')
P_CROSSFEED = (r'^ZamHeadX2$', r'crossfeed', r'\bbs2b\b',
               r'^Calf Haas Stereo Enhancer$')
P_WIDE = (r'^Calf Stereo Tools$', r'^Calf Haas Stereo Enhancer$',
          r'^Calf Multi Spread$', r'stereo (tools|enhancer|widen)')
P_SAT = (r'^Calf Tape Simulator$', r'^Calf Saturator$', r'^ZamTube$',
         r'^ZamAutoSat$', r'saturat')
P_EXCITE = (r'^Calf Exciter$', r'^Calf Bass Enhancer$',
            r'^Calf Multiband Enhancer$', r'exciter|enhancer')
P_TRANSIENT = (r'^Calf Transient Designer$', r'transient',
               r'^Beat Breather Stereo$')
P_PHONO = (r'^ZamPhono$', r'phono|riaa', r'^Calf Vinyl$',
           r'^Calf Tape Simulator$')
P_AUTOGAIN = (r'^Autogain Stereo$', r'autogain', r'^Calf Compressor$')
P_REVERB = (r'^Calf Reverb$', r'^ZamVerb$', r'\breverb\b')


# ---------------------------------------------------------------- catalog --
# Ordered, deliberately: a plain mix at the top, a full broadcast chain at the
# bottom, and every entry in between adding exactly one idea to the one before
# it.  Nothing is grouped — the position in the list is the whole hierarchy.

CATALOG: list = [
    # ---------------------------------------------------------- starters --
    Template(
        id='speakers', title='Speakers', role='mix',
        icon='audio-speakers-symbolic',
        blurb='A mix that feeds whatever your speakers are.',
        detail='The plainest thing on the board: audio goes in, audio comes '
               'out of your current output. Add stages to it whenever you '
               'want — this is where most setups start.'),
    Template(
        id='headphones', title='Headphones', role='mix',
        icon='audio-headphones-symbolic',
        blurb='A mix with a gentle headphone tilt already dialled in.',
        detail='A little more low end, a touch off the upper-mid glare and a '
               'lift on top — the shape most headphones want before you tune '
               'anything for your own pair.',
        steps=(Step('eq', 'Headphone tilt', preamp=-1.0, bands=(
            ('LSC', 105, 1.5, 0.7), ('PK', 3200, -1.5, 1.1),
            ('HSC', 10000, 1.0, 0.7))),)),
    Template(
        id='bass-boost', title='Bass boost', role='mix',
        icon='audio-volume-high-symbolic',
        blurb='More low end, with the headroom to survive it.',
        detail='A shelf under 80 Hz and a little lift at the very bottom. The '
               'preamp comes down by the same amount the shelf goes up, which '
               'is what keeps a boosted mix from clipping.',
        steps=(Step('eq', 'Low shelf', preamp=-4.0, bands=(
            ('LSC', 80, 6.0, 0.7), ('PK', 45, 2.0, 1.0),
            ('PK', 300, -1.5, 1.2))),)),
    Template(
        id='dialogue', title='Dialogue boost', role='mix',
        icon='camera-video-symbolic',
        blurb='For films where you can hear everything except the talking.',
        detail='Takes weight out of the rumble, lifts the band voices live in '
               'and adds a little air. Works on anything with a soundtrack '
               'fighting the dialogue.',
        steps=(Step('eq', 'Speech lift', preamp=-1.5, bands=(
            ('LSC', 130, -3.0, 0.7), ('PK', 2600, 3.0, 1.2),
            ('PK', 6000, 1.5, 1.6))),)),
    Template(
        id='late-night', title='Late-night listening', role='mix',
        icon='weather-clear-night-symbolic',
        blurb='The loudness curve, for listening quietly.',
        detail='Ears lose the bottom and the top first as things get quieter, '
               'so both come back up and the middle steps aside. Turn it off '
               'when you turn the volume up.',
        steps=(Step('eq', 'Loudness curve', preamp=-4.0, bands=(
            ('LSC', 70, 5.0, 0.7), ('PK', 350, -2.0, 1.0),
            ('HSC', 9000, 3.0, 0.7))),)),
    Template(
        id='eq-everything', title='Equalizer on everything', role='source',
        kind='everything', icon='format-justify-fill-symbolic',
        blurb='One equalizer between every app and your output.',
        detail='A flat ten-band curve, ready to be pulled about. Send your '
               'apps into it and everything you play goes through the same '
               'correction.',
        steps=(Step('eq', 'Equalizer', bands=tuple(
            ('PK', f, 0.0, 1.0) for f in
            (32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000))),)),
    Template(
        id='eq-app', title='Equalizer on one app', role='source', kind='app',
        icon='applications-multimedia-symbolic',
        blurb='Shape one application without touching the rest.',
        detail='A five-band curve on a source of its own. Drag an app onto '
               'the card and only that app goes through it.',
        steps=(Step('eq', 'Equalizer', bands=tuple(
            ('PK', f, 0.0, 1.0) for f in (60, 250, 1000, 4000, 12000))),)),
    Template(
        id='music', title='Music', role='source', kind='app',
        icon='emblem-music-symbolic',
        blurb='A warm, unfussy curve for listening.',
        detail='A small lift at either end and a dip where most mixes get '
               'crowded. Meant to be lived with rather than measured.',
        steps=(Step('eq', 'Warmth', preamp=-2.0, bands=(
            ('LSC', 110, 2.5, 0.7), ('PK', 450, -1.5, 1.1),
            ('PK', 2800, -1.0, 1.4), ('HSC', 11000, 2.0, 0.7))),)),
    Template(
        id='game', title='Game audio', role='source', kind='app',
        icon='applications-games-symbolic',
        blurb='Footsteps and cues in front of the soundtrack.',
        detail='Lifts the band footsteps, reloads and voice lines sit in, and '
               'takes some weight out of explosions so they stop masking it.',
        steps=(Step('eq', 'Cue lift', preamp=-2.0, bands=(
            ('LSC', 120, -2.5, 0.7), ('PK', 900, -1.5, 1.0),
            ('PK', 3600, 3.5, 1.4), ('HSC', 11000, 1.5, 0.7))),)),
    Template(
        id='mic-basic', title='Microphone', role='source', kind='mic',
        positions=('FL',), icon='audio-input-microphone-symbolic',
        blurb='Rumble out, presence in — the first thing any mic wants.',
        detail='A shelf that takes out desk thumps and handling noise, a dip '
               'where a close mic gets boxy, and a lift where speech lives.',
        steps=(Step('eq', 'Voice shape', bands=(
            ('LSC', 100, -8.0, 0.7), ('PK', 220, -2.5, 1.2),
            ('PK', 3000, 3.0, 1.2))),)),

    # ---------------------------------------------------------- advanced --
    Template(
        id='limiter', title='Safety limiter', role='mix', advanced=True,
        icon='security-high-symbolic',
        blurb='Catches peaks before they reach your speakers.',
        detail='A limiter on its own, meant to sit at the end of everything '
               'else. Cheap insurance once you start boosting things, and the '
               'one stage that belongs last in any chain.',
        steps=(Step('effect', 'Limiter', patterns=P_LIMIT),)),
    Template(
        id='night-tv', title='Even out loud and quiet', role='mix',
        advanced=True, icon='view-continuous-symbolic',
        blurb='Compression for films that whisper and then explode.',
        detail='A compressor pulls the loud parts down so you can turn the '
               'quiet parts up, and a limiter catches whatever is left. The '
               'thing a television calls "night mode".',
        steps=(Step('effect', 'Compressor', patterns=P_COMP),
               Step('effect', 'Limiter', patterns=P_LIMIT))),
    Template(
        id='loudness', title='Loudness compensation', role='mix',
        advanced=True, icon='audio-volume-medium-symbolic',
        blurb='Keeps the tone right as the volume comes down.',
        detail='The equal-loudness contour applied properly, by a plugin that '
               'tracks the level, instead of one fixed curve that is only '
               'correct at one volume.',
        steps=(Step('effect', 'Loudness', patterns=P_LOUD),
               Step('effect', 'Limiter', patterns=P_LIMIT))),
    Template(
        id='crossfeed', title='Crossfeed for headphones', role='mix',
        advanced=True, icon='media-playlist-shuffle-symbolic',
        blurb='Takes the hard-panned edge off headphone listening.',
        detail='Bleeds a little of each channel into the other, the way your '
               'head does with speakers. Old recordings with an instrument '
               'entirely in one ear stop being tiring.',
        steps=(Step('effect', 'Crossfeed', patterns=P_CROSSFEED),
               Step('eq', 'Tilt back', preamp=-1.0, bands=(
                   ('HSC', 8000, 1.0, 0.7),)))),
    Template(
        id='small-speakers', title='Small speakers', role='mix',
        advanced=True, icon='computer-symbolic',
        blurb='Fakes the bottom octave a small driver cannot make.',
        detail='A harmonic enhancer builds overtones of the bass your speaker '
               'is too small to reproduce, so your ear fills in the '
               'fundamental. Far kinder to a laptop speaker than a bass '
               'shelf, which only makes it distort.',
        steps=(Step('effect', 'Bass enhancer', patterns=P_EXCITE),
               Step('eq', 'Clear the mud', preamp=-1.0, bands=(
                   ('LSC', 90, -6.0, 0.7), ('PK', 400, -2.0, 1.1))),
               Step('effect', 'Limiter', patterns=P_LIMIT))),
    Template(
        id='widener', title='Wider stereo', role='mix', advanced=True,
        icon='object-flip-horizontal-symbolic',
        blurb='Opens the image out past the speakers.',
        detail='Widens the sides while leaving the middle where it is, so '
               'voices and bass stay centred. A little goes a long way; too '
               'much and the mix collapses on anything mono.',
        steps=(Step('effect', 'Stereo width', patterns=P_WIDE),)),
    Template(
        id='warmth', title='Tape warmth', role='mix', advanced=True,
        icon='media-playlist-repeat-symbolic',
        blurb='Gentle saturation, for digital that sounds too clean.',
        detail='Adds the harmonics and the soft ceiling tape had. Best kept '
               'subtle — the point is that you notice when you switch it off, '
               'not when you switch it on.',
        steps=(Step('effect', 'Saturation', patterns=P_SAT),
               Step('effect', 'Limiter', patterns=P_LIMIT))),
    Template(
        id='punch', title='More punch', role='mix', advanced=True,
        icon='media-skip-forward-symbolic',
        blurb='Sharpens attacks without touching the level.',
        detail='A transient designer works on the shape of each hit rather '
               'than on loudness, so drums get their edge back without the '
               'whole mix getting louder.',
        steps=(Step('effect', 'Transients', patterns=P_TRANSIENT),
               Step('effect', 'Limiter', patterns=P_LIMIT))),
    Template(
        id='turntable', title='Turntable', role='source', kind='mic',
        advanced=True, icon='media-optical-symbolic',
        blurb='Rumble filter, RIAA-ish trim and a little tape.',
        detail='For a deck coming in on a line input. The high-pass takes out '
               'warp and rumble below the record, the curve trims what a '
               'phono stage tends to leave behind, and the saturation covers '
               'the rest.',
        steps=(Step('eq', 'Rumble filter', bands=(
                   ('LSC', 35, -14.0, 0.7), ('PK', 60, -3.0, 1.4))),
               Step('effect', 'Phono character', patterns=P_PHONO),
               Step('eq', 'Air', preamp=-1.0, bands=(
                   ('PK', 2200, -1.0, 1.2), ('HSC', 12000, 1.5, 0.7))))),
    Template(
        id='voice-chat', title='Voice chat cleanup', role='source',
        kind='mic', positions=('FL',), advanced=True,
        icon='user-available-symbolic',
        blurb='Gate the room out, then even the level.',
        detail='A gate closes between sentences so your keyboard and your fan '
               'never reach the call, the curve does what the plain microphone '
               'template does, and the compressor keeps you at one level as '
               'you move about.',
        steps=(Step('effect', 'Noise gate', patterns=P_GATE),
               Step('eq', 'Voice shape', bands=(
                   ('LSC', 100, -8.0, 0.7), ('PK', 220, -2.5, 1.2),
                   ('PK', 3000, 3.0, 1.2))),
               Step('effect', 'Compressor', patterns=P_COMP))),
    Template(
        id='podcast', title='Podcast and streaming voice', role='source',
        kind='mic', positions=('FL',), advanced=True,
        icon='audio-input-microphone-symbolic',
        blurb='Gate, shape, compress, limit — in that order.',
        detail='The chain every streaming guide converges on. The order is '
               'the point: gate first so the compressor never lifts the room, '
               'tone next, level after that, and a limiter last so nothing '
               'you do can clip the stream.',
        steps=(Step('effect', 'Noise gate', patterns=P_GATE),
               Step('eq', 'Voice shape', preamp=-1.0, bands=(
                   ('LSC', 90, -10.0, 0.7), ('PK', 200, -3.0, 1.2),
                   ('PK', 2800, 3.0, 1.2), ('HSC', 9000, 2.0, 0.7))),
               Step('effect', 'Compressor', patterns=P_COMP),
               Step('effect', 'Limiter', patterns=P_LIMIT))),
    Template(
        id='broadcast', title='Broadcast voice', role='source', kind='mic',
        positions=('FL',), advanced=True,
        icon='network-wireless-symbolic',
        blurb='The full desk: gate, de-ess, tone, multiband, limit.',
        detail='What a radio chain actually does. The de-esser sits before '
               'the compressor so sibilance is not what triggers it, and the '
               'multiband stage evens the bands out separately, which is why '
               'broadcast voices sound the same however the person moves.',
        steps=(Step('effect', 'Noise gate', patterns=P_GATE),
               Step('effect', 'De-esser', patterns=P_DEESS),
               Step('eq', 'Voice shape', preamp=-1.0, bands=(
                   ('LSC', 90, -10.0, 0.7), ('PK', 200, -3.0, 1.2),
                   ('PK', 2800, 3.0, 1.2), ('HSC', 9000, 2.0, 0.7))),
               Step('effect', 'Multiband', patterns=P_MBCOMP),
               Step('effect', 'Limiter', patterns=P_LIMIT))),
    Template(
        id='stream-bus', title='Stream loudness bus', role='mix',
        advanced=True, icon='media-record-symbolic',
        blurb='Holds one level, so nobody rides your volume slider.',
        detail='Autogain finds the level, the multiband keeps the bands even '
               'and the limiter guards the ceiling. Put your game, your music '
               'and your microphone into this and they arrive at the same '
               'loudness.',
        steps=(Step('effect', 'Autogain', patterns=P_AUTOGAIN),
               Step('effect', 'Multiband', patterns=P_MBCOMP),
               Step('effect', 'Limiter', patterns=P_LIMIT))),
    Template(
        id='monitor-bus', title='Studio monitor bus', role='mix',
        advanced=True, icon='applications-engineering-symbolic',
        blurb='A flat eight-band curve and a limiter to work behind.',
        detail='Starts flat on purpose: this is the strip you put your room '
               'measurement into, band by band. The limiter is there so a '
               'mistake in the curve cannot reach the speakers.',
        steps=(Step('eq', 'Room correction', bands=tuple(
                   ('PK', f, 0.0, 2.0) for f in
                   (40, 63, 100, 160, 250, 400, 630, 1000))),
               Step('effect', 'Limiter', patterns=P_LIMIT))),
    Template(
        id='mastering', title='Mastering-style bus', role='mix',
        advanced=True, icon='preferences-other-symbolic',
        blurb='Tilt, glue, and a ceiling — the last three things.',
        detail='A broad tilt rather than surgical bands, one gentle multiband '
               'to glue it together, and a limiter. Deliberately mild: a bus '
               'chain that you can hear working is doing too much.',
        steps=(Step('eq', 'Broad tilt', preamp=-2.0, bands=(
                   ('LSC', 120, 1.0, 0.7), ('PK', 500, -1.0, 1.0),
                   ('HSC', 8000, 1.5, 0.7))),
               Step('effect', 'Glue', patterns=P_MBCOMP),
               Step('effect', 'Limiter', patterns=P_LIMIT))),
    Template(
        id='reverb-send', title='Reverb send', role='mix', advanced=True,
        icon='weather-showers-symbolic',
        blurb='A wet mix of its own, for sending things into.',
        detail='A mix that exists to be sent to. Point one source at your '
               'speakers and at this one as well, and you have a parallel '
               'effect with a level of its own — which is what a send is.',
        steps=(Step('effect', 'Reverb', patterns=P_REVERB),
               Step('eq', 'Thin it out', preamp=-3.0, bands=(
                   ('LSC', 200, -6.0, 0.7), ('HSC', 7000, -3.0, 0.7))))),
]


def by_id(tid: str):
    return next((t for t in CATALOG if t.id == tid), None)


def starters() -> list:
    return [t for t in CATALOG if not t.advanced]


def advanced() -> list:
    return [t for t in CATALOG if t.advanced]


__all__ = ['Template', 'Step', 'CATALOG', 'build_stages', 'missing_steps',
           'find_plugin', 'usable', 'by_id', 'starters', 'advanced']
