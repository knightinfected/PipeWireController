"""What the Devices page says about each endpoint, worked out from one dump.

Pure: no GTK and no subprocess.  Every function takes the lists a page
already has (`pw.list_audio_nodes`, `pw.list_streams`, `surround.list_cards`,
all from one `pw-dump`), so a delivery costs no extra read, and every answer
can be tested without a window (`tests/test_endpoints.py`).

Each answer is read off the node's own props at call time, never cached and
never inferred from a name or version where a property says it outright --
the house rule (`CLAUDE.md`, "Probe, never infer").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import chains, surround

# `pw.AudioNode.software_kind` -> what a person would call it.
KIND_LABEL = {
    'equalizer': 'Equalizer',
    'mic cleanup': 'Mic cleanup',
    'signal path': 'Signal path',
    'filter chain': 'Filter chain',
    'loopback': 'Loopback',
}

STATE_LABEL = {
    'running': 'Running',
    'idle': 'Idle',
    'suspended': 'Suspended',
    'creating': 'Starting',
    'error': 'Error',
}

# The dot beside the state: semantic colour only (UI.md §2).
STATE_DOT = {
    'running': 'ok',
    'creating': 'warn',
    'error': 'err',
}


@dataclass
class Activity:
    """What is attached to one endpoint right now."""

    # people's apps: playing into a sink, or recording from a source
    apps: list = field(default_factory=list)            # pw.Stream
    # apps recording what a sink plays (its monitor ports)
    monitors: list = field(default_factory=list)        # pw.Stream
    # software devices whose output lands here
    feeds: list = field(default_factory=list)           # pw.AudioNode
    # a software device's destinations, one per output leg (None = unlinked)
    sends: list = field(default_factory=list)           # pw.AudioNode | None
    # the one leg a picker can move: set only when there is exactly one
    out_stream: int | None = None
    out_dest: int | None = None


def _group(node_or_stream) -> str:
    return (node_or_stream.props or {}).get('node.link-group') or ''


def activity(nodes, streams) -> dict:
    """{node id: Activity} for every node in `nodes`.

    `streams` must be the UNFILTERED `pw.list_streams()`: the output legs of
    a virtual device are exactly the streams `apps_only` drops.

    A virtual device is a set of objects sharing one `node.link-group` (the
    same discriminator `pw.Stream.is_app` and `pw.virtual_outputs` use):
    a sink plus one playback stream per output for a chain, a loopback or a
    combined device, or a sink plus a source for a virtual microphone.  So
    its destinations are every playback leg in its group, plus any source
    in it, and each destination lists the device among its feeds.
    """
    by_id = {n.id: n for n in nodes}
    acts = {n.id: Activity() for n in nodes}

    legs: dict[str, list] = {}
    for s in streams:
        if s.is_playback and not s.is_app and _group(s):
            legs.setdefault(_group(s), []).append(s)
    sources_by_group: dict[str, list] = {}
    for n in nodes:
        if not n.is_sink and n.is_virtual and _group(n):
            sources_by_group.setdefault(_group(n), []).append(n)

    for s in streams:
        if not s.is_app or s.target_id not in acts:
            continue
        target = by_id[s.target_id]
        if s.is_playback:
            acts[target.id].apps.append(s)
        elif target.is_sink:
            acts[target.id].monitors.append(s)
        else:
            acts[target.id].apps.append(s)

    for n in nodes:
        if not (n.is_virtual and n.is_sink):
            continue
        group = _group(n)
        if not group:
            continue
        mine = acts[n.id]
        own_legs = legs.get(group, [])
        for leg in own_legs:
            dest = by_id.get(leg.target_id)
            mine.sends.append(dest)
            if dest is not None and dest.id != n.id \
                    and n not in acts[dest.id].feeds:
                acts[dest.id].feeds.append(n)
        for src in sources_by_group.get(group, []):
            mine.sends.append(src)
            if n not in acts[src.id].feeds:
                acts[src.id].feeds.append(n)
        if len(own_legs) == 1 and not sources_by_group.get(group):
            mine.out_stream = own_legs[0].id
            mine.out_dest = own_legs[0].target_id
    return acts


def output_choices(node, nodes, acts) -> list:
    """Sinks this software device may be re-pointed at, ring-checked.

    The same rule as the Mixer's output picker: never itself, never anything
    that would feed back into it.  The ring check walks the *live* graph,
    so it sees hand-written chains as well as ours.  Empty when the device
    has no single movable output (a combined device, a virtual mic).
    """
    mine = acts.get(node.id)
    if mine is None or mine.out_stream is None:
        return []
    edges = {}
    for n in nodes:
        a = acts.get(n.id)
        if a is not None and a.sends:
            first = a.sends[0]
            edges[n.name] = first.name if first is not None else ''
    return [n for n in nodes
            if n.is_sink and n.name != node.name
            and not chains.would_loop(node.name, n.name, edges)]


def connection(node) -> str:
    """How a hardware endpoint is attached: 'Built-in', 'USB', 'HDMI'..."""
    p = node.props or {}
    api = str(p.get('device.api') or '')
    bus = str(p.get('device.bus') or '')
    form = str(p.get('device.form-factor') or '')
    if api.startswith('bluez') or bus == 'bluetooth':
        return 'Bluetooth'
    if bus == 'usb':
        return 'USB'
    if 'hdmi' in node.name.lower() or form == 'hdmi':
        return 'HDMI'
    if bus in ('pci', 'isa') or form == 'internal':
        return 'Built-in'
    return ''


def codec(node) -> str:
    """A Bluetooth endpoint's codec as the node reports it, or ''."""
    value = (node.props or {}).get('api.bluez5.codec')
    return str(value).upper() if value else ''


def port_name(node) -> str:
    """The active port's name ('Speakers', 'Headphones'), or ''."""
    return next((desc for idx, desc, _a in node.ports
                 if idx == node.active_port), '')


def kind(node) -> str:
    """What a software endpoint is, in words; '' for hardware."""
    if not node.is_virtual:
        return ''
    if node.name.startswith('pwctl.vd-') and not node.is_sink:
        return 'Virtual microphone'
    return KIND_LABEL.get(node.software_kind, 'Virtual device')


def subtitle(node) -> str:
    """The line under a card's name: where the sound physically is."""
    if node.is_virtual:
        return kind(node)
    parts = [port_name(node), connection(node), codec(node)]
    return ' · '.join(p for p in parts if p)


def _sample_bits(fmt: str) -> str:
    f = (fmt or '').upper()
    for prefix, text in (('F64', '64-bit float'), ('F32', '32-bit float'),
                         ('S32', '32-bit'), ('S24', '24-bit'),
                         ('S16', '16-bit'), ('U8', '8-bit'), ('S8', '8-bit')):
        if f.startswith(prefix):
            return text
    return ''


def _channels(count) -> str:
    if not count:
        return ''
    return {1: 'mono', 2: 'stereo'}.get(count, f'{count} channels')


def format_text(node) -> str:
    """'48 kHz · 24-bit · stereo' while running, else the state in words."""
    fmt = node.fmt or {}
    if node.state == 'running' and fmt:
        rate = fmt.get('rate')
        parts = [f'{rate / 1000:g} kHz' if rate else '',
                 _sample_bits(fmt.get('format', '')),
                 _channels(fmt.get('channels'))]
        text = ' · '.join(p for p in parts if p)
        if text:
            return text
    return STATE_LABEL.get(node.state, '')


# Channel layouts by the set of positions, so the order a driver lists them
# in does not matter.  5.1 comes as either rear or side pairs.
_LAYOUTS = {
    frozenset({'MONO'}): 'Mono',
    frozenset({'FC'}): 'Mono',
    frozenset({'FL', 'FR'}): 'Stereo',
    frozenset({'FL', 'FR', 'LFE'}): '2.1',
    frozenset({'FL', 'FR', 'RL', 'RR'}): 'Quad',
    frozenset({'FL', 'FR', 'FC', 'LFE', 'RL', 'RR'}): '5.1',
    frozenset({'FL', 'FR', 'FC', 'LFE', 'SL', 'SR'}): '5.1',
    frozenset({'FL', 'FR', 'FC', 'LFE', 'RL', 'RR', 'SL', 'SR'}): '7.1',
}


def layout_name(positions, count=None) -> str:
    """'Stereo', '5.1', '7.1'… from a channel map; '4 channels' otherwise
    (Pro Audio's AUX0…AUXn names no layout)."""
    pos = [str(p).upper() for p in (positions or [])]
    name = _LAYOUTS.get(frozenset(pos)) if len(set(pos)) == len(pos) else None
    if name:
        return name
    count = count or len(pos)
    if not count:
        return ''
    return {1: 'Mono', 2: 'Stereo'}.get(count, f'{count} channels')


def format_tags(node) -> list:
    """The device's format as short tags: ['48 kHz', '24-bit', 'Stereo'].

    Rate and sample format exist only while the device runs: a suspended or
    idle node has `Format: []` in the dump (DR-012, measured), so it gets
    its channel layout alone, which the channel map always gives.
    """
    fmt = node.fmt or {}
    tags = []
    if node.state == 'running' and fmt:
        rate = fmt.get('rate')
        if rate:
            tags.append(f'{rate / 1000:g} kHz')
        bits = _sample_bits(fmt.get('format', ''))
        if bits:
            tags.append(bits)
    positions = list(getattr(node, 'channel_map', None) or [])
    running = fmt.get('channels') if node.state == 'running' else None
    if running and positions and running != len(positions):
        positions = []          # the running format wins over the map
    count = running or len(positions) \
        or len(getattr(node, 'channels', None) or [])
    layout = layout_name(positions, count)
    if layout:
        tags.append(layout)
    return tags


def state_dot(node) -> str:
    """CSS class for the state dot: ok / warn / err / dim."""
    return STATE_DOT.get(node.state, 'dim')


def owner(node, chain_templates=None):
    """(page name, page title) of the page that manages this endpoint.

    None for hardware and for software nobody here created (a hand-written
    chain in `filter-chain.conf.d`).  `chain_templates` maps a chain id to
    its template, because an effect rack is a chain too and only its
    template says which page edits it.
    """
    name = node.name
    if name.startswith('pwctl.vd-'):
        return 'virtual', 'Virtual Devices'
    if name.startswith(('pwctl.eq.', 'pwctl.mic.')):
        return 'enhance', 'Equalizer'
    if name.startswith(('pwctl.source.', 'pwctl.mix.', 'pwctl.xover.')):
        return 'paths', 'Signal Paths'
    if name.startswith('effect_input.pwctl.'):
        chain_id = name[len('effect_input.pwctl.'):]
        if (chain_templates or {}).get(chain_id) == 'effect-rack':
            return 'effects', 'Effects'
        return 'chains', 'Filter Chains'
    return None


def card_of(node, cards):
    """The surround.Card a hardware node belongs to, or None."""
    try:
        dev = int((node.props or {}).get('device.id'))
    except (TypeError, ValueError):
        return None
    return next((c for c in cards if c.id == dev), None)


def cards_without_endpoint(cards, nodes, sink: bool) -> list:
    """Sound cards that could give this direction but give nothing now.

    A card on Off, or on an output-only profile seen from the Inputs side.
    Without these the card would simply be missing from the page, which is
    the WirePlumber saved-profile trap at its worst: no endpoint, so nothing
    to click to get it back.

    Only when there is something to offer: the card is fully off (nothing in
    either direction), or a working profile for this direction exists.  A
    USB mic whose card also lists an unavailable duplex profile is not an
    "output that is off", and listing it as one would be noise.
    """
    used = {True: set(), False: set()}
    for n in nodes:
        card = card_of(n, cards)
        if card is not None:
            used[n.is_sink].add(card.id)
    out = []
    for c in cards:
        if not (c.has_sink if sink else c.has_source) or c.id in used[sink]:
            continue
        fully_off = c.id not in used[not sink]
        if fully_off or surround.working_profile(c, sink=sink) is not None:
            out.append(c)
    return out


@dataclass
class ProfileState:
    unavailable: bool       # the active profile is marked unavailable
    gives_nothing: bool     # it provides nothing in this direction
    fix: int | None         # a working profile to offer, or None

    @property
    def stuck(self) -> bool:
        return (self.unavailable or self.gives_nothing) \
            and self.fix is not None


def profile_state(card, sink: bool) -> ProfileState:
    """The Mixer's ⚠ + Reset rule, for one card and one direction.

    The ⚠ means the active profile is *explicitly* unavailable (HDMI with
    nothing plugged in); 'unknown' (Pro Audio, which ALSA cannot probe) is
    playable and gets no alarm.  A fix is offered whenever the card gives
    nothing usable in this direction and a working profile exists.
    """
    avail = next((a for idx, _d, a in card.profiles
                  if idx == card.active_profile), 'unknown')
    has_sink, has_src = card.dirs.get(card.active_profile, (False, False))
    return ProfileState(
        unavailable=avail == 'no',
        gives_nothing=not (has_sink if sink else has_src),
        fix=surround.working_profile(card, sink=sink))


def profile_name(card, index) -> str:
    return next((d for idx, d, _a in card.profiles if idx == index), '')
