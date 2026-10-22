"""Live PipeWire state: pw-dump, pw-metadata settings, wpctl devices."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field

from . import balance, levels
from .system import run

# ------------------------------------------------------------- pw-metadata --

_META_RE = re.compile(r"key:'([^']+)'\s+value:'([^']*)'")

SETTINGS_KEYS = (
    'log.level', 'clock.rate', 'clock.allowed-rates', 'clock.quantum',
    'clock.min-quantum', 'clock.max-quantum', 'clock.force-quantum',
    'clock.force-rate',
)


def _dump_metadata(dump, name):
    """{key: value} for the `name` metadata's subject-0 entries in a pw-dump.

    None when the dump has no such object (pw-dump failed, or the list is not
    a whole dump): that is "cannot tell", and the readers below then ask
    pw-metadata as they always did.  pw-dump has already parsed the values,
    so a JSON object arrives as a dict and a plain number as a number.
    """
    for obj in dump or ():
        if (obj.get('type') == 'PipeWire:Interface:Metadata'
                and (obj.get('props') or {}).get('metadata.name') == name):
            return {e.get('key'): e.get('value')
                    for e in obj.get('metadata') or ()
                    if e.get('subject') == 0}
    return None


def _meta_text(value) -> str:
    """A value from the dump, as text, the way pw-metadata gives it.

    pw-dump has turned numbers into numbers, and their exact text is gone:
    '0256' comes back '256' and '1.50' as '1.5' (measured on 1.0.5, 1.4.2
    and 1.6.2).  Whatever int() makes of the one, it makes of the other.
    """
    return value if isinstance(value, str) else json.dumps(value)


def read_settings(dump=None) -> dict:
    """Runtime settings from the `settings` metadata, as text.

    Read from `dump` when it carries them, which saves the Dashboard a
    pw-metadata process on every refresh.  Numbers are normalised then (see
    _meta_text), so a page that shows the text itself should not pass one.
    """
    meta = _dump_metadata(dump, 'settings')
    if meta is not None:
        return {k: _meta_text(v) for k, v in meta.items()}
    rc, out, _ = run(['pw-metadata', '-n', 'settings'])
    vals = {}
    if rc == 0:
        for m in _META_RE.finditer(out):
            vals[m.group(1)] = m.group(2)
    return vals


def set_setting(key: str, value) -> bool:
    rc, _, _ = run(['pw-metadata', '-n', 'settings', '0', key, str(value)])
    return rc == 0


_DEFAULT_KEY = re.compile(r'default\.[\w.]+')


def read_default_names(dump=None) -> dict:
    """Default sink/source node names from the `default` metadata.

    Read from `dump` when it carries that metadata, which saves a pw-metadata
    process on every list_audio_nodes() call, and takes the defaults from the
    same moment as the nodes they point at.
    """
    meta = _dump_metadata(dump, 'default')
    if meta is not None:
        res = {}
        for key, value in meta.items():
            if not _DEFAULT_KEY.fullmatch(key or ''):
                continue
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
            if isinstance(value, dict):
                res[key] = value.get('name')
        return res
    rc, out, _ = run(['pw-metadata', '-n', 'default'])
    res = {}
    if rc == 0:
        for m in re.finditer(r"key:'(default\.[\w.]+)'\s+value:'({[^']*})'", out):
            try:
                res[m.group(1)] = json.loads(m.group(2)).get('name')
            except (ValueError, AttributeError):
                pass
    return res


# ----------------------------------------------------------------- pw-dump --

def pw_dump():
    rc, out, _ = run(['pw-dump'], timeout=15)
    if rc != 0 or not out.strip():
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


@dataclass
class AudioNode:
    id: int
    name: str
    description: str
    media_class: str
    props: dict = field(default_factory=dict)
    is_default: bool = False
    volume: float | None = None
    muted: bool = False
    channels: list = field(default_factory=list)      # per-channel, cubic (UI) scale
    channel_map: list = field(default_factory=list)   # 'FL', 'FR', 'AUX3', …
    serial: int = -1
    ports: list = field(default_factory=list)   # [(route_index, description, available)]
    active_port: int | None = None
    # `info.state` from the dump: 'running', 'idle', 'suspended', 'creating'
    # or 'error'; '' when the dump does not say.
    state: str = ''
    # The negotiated format while the node runs, from its `Format` param:
    # {'rate': 48000, 'channels': 2, 'format': 'S32LE'}.  Empty while it is
    # suspended, because PipeWire clears the param then (measured
    # 2026-10-22: every suspended node in the dump has `Format: []`).
    fmt: dict = field(default_factory=dict)

    @property
    def is_sink(self):
        return self.media_class == 'Audio/Sink'

    @property
    def is_virtual(self):
        # Filter-chain / loopback nodes have no device.api
        return 'device.api' not in self.props

    @property
    def software_kind(self) -> str:
        """What *kind* of software device this is, for a badge beside 'virtual'.

        Answered from the node's own props, never from our meta files: this is
        read on every 3 s dashboard poll, and a meta lookup would be a stat and
        a JSON parse per row per tick.  Two sources, and between them they
        cover objects this app did not create:

        * **`node.name`** carries our own prefixes.  `pwctl.eq.enh-…` is an
          Equalizer, `pwctl.source.psou-…`/`pwctl.mix.pmix-…` are Signal Paths
          strips, `effect_input.pwctl.…` is a chain from the Filter Chains page
          (an effect rack is one too -- telling those apart needs the meta, and
          "filter chain" is true of both).
        * **`node.link-group`** is stamped by the module that built the node --
          `filter-chain-<pid>-<n>` or `loopback-<pid>-<n>` -- so a hand-written
          chain in `filter-chain.conf.d` is identified as readily as ours.
          This is the same property `pw.Stream.is_app` and `virtual_outputs`
          pair legs on.

        Returns '' when there is nothing to add beyond "virtual" -- including
        for our own virtual devices, where a second badge reading "virtual
        device" would only repeat the first.
        """
        if not self.is_virtual:
            return ''
        name = self.name
        if name.startswith('pwctl.eq.'):
            return 'equalizer'
        if name.startswith('pwctl.mic.'):
            return 'mic cleanup'
        if name.startswith(('pwctl.source.', 'pwctl.mix.', 'pwctl.xover.')):
            return 'signal path'
        if name.startswith('pwctl.vd-'):
            return ''                    # "virtual" already says it
        if name.startswith('effect_input.pwctl.'):
            return 'filter chain'
        group = self.props.get('node.link-group', '')
        if group.startswith('filter-chain-'):
            return 'filter chain'
        if group.startswith('loopback-'):
            return 'loopback'
        return ''


def _device_routes(dump) -> dict:
    """device object id -> (EnumRoute list, active Route list)."""
    routes = {}
    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Device':
            continue
        info = obj.get('info') or {}
        props = info.get('props') or {}
        if props.get('media.class') != 'Audio/Device':
            continue
        params = info.get('params') or {}
        routes[obj['id']] = (params.get('EnumRoute') or [],
                            params.get('Route') or [])
    return routes


def _card_slot(node: AudioNode):
    """(device id, card.profile.device) for a node on a card, else None."""
    try:
        return (int(node.props['device.id']),
                int(node.props['card.profile.device']))
    except (KeyError, TypeError, ValueError):
        return None


def _active_route(node: AudioNode, routes: dict) -> dict | None:
    """The card's active Route for this node, or None.

    None for anything not on a card (virtual devices, chains) and for nodes
    whose card has no route for them (every Pro Audio node).
    """
    slot = _card_slot(node)
    if slot is None:
        return None
    _enum, active = routes.get(slot[0], ([], []))
    want = 'Output' if node.is_sink else 'Input'
    return next((r for r in active
                 if r.get('device') == slot[1]
                 and r.get('direction') == want), None)


def _attach_ports(node: AudioNode, routes: dict):
    """Fill node.ports / node.active_port from its device's Route params."""
    slot = _card_slot(node)
    if slot is None:
        return
    enum, _active = routes.get(slot[0], ([], []))
    want = 'Output' if node.is_sink else 'Input'
    node.ports = [(r['index'], r.get('description', f'Route {r["index"]}'),
                   r.get('available', 'unknown'))
                  for r in enum
                  if r.get('direction') == want
                  and slot[1] in (r.get('devices') or [])]
    route = _active_route(node, routes)
    node.active_port = route['index'] if route else None


_POSITION_RE = re.compile(r'[A-Z][A-Z0-9_]*')


def _applied_channels(obj, route: dict | None = None
                      ) -> tuple[list[float], list[str], bool] | None:
    """(raw channel volumes as PipeWire applies them, positions, muted).

    A node on a card is read from its active `route` when one is passed and
    it carries volumes, not from the node's own Props: see `dump_volume()`.

    Usually that is just `channelVolumes`.  The exception, measured
    2026-10-21: WirePlumber restores a virtual node's saved volume by
    `media.name`, even when the saved entry has fewer channels than the node
    has now (a device rebuilt from stereo to 5.1 under the same name, or two
    combined devices sharing "Combine stream").  Props then carry a 2-entry
    array on a 6-channel node, pactl shows the other four at 0%, and what
    actually plays is the **mean of the short array on every channel**.
    `softVolumes` always has the node's real channel count, so the array is
    rebuilt to that length with the mean, which is the level you hear, and
    a full-length write from the app repairs the node.
    """
    route_props = (route or {}).get('props') or {}
    if route_props.get('channelVolumes'):
        entry = route_props
    else:
        params = (obj.get('info') or {}).get('params') or {}
        props = params.get('Props') or []
        if not props:
            return None
        entry = props[0] or {}
    raw = list(entry.get('channelVolumes') or [])
    if not raw:
        return None
    real = len(entry.get('softVolumes') or ())
    if real > len(raw):
        raw = [sum(raw) / len(raw)] * real
    positions = list(entry.get('channelMap') or [])
    if len(positions) != len(raw):
        node_props = (obj.get('info') or {}).get('props') or {}
        positions = _POSITION_RE.findall(str(node_props.get('audio.position')
                                             or ''))
        if len(positions) != len(raw):
            positions = [''] * len(raw)
    return raw, positions, bool(entry.get('mute'))


def dump_volume(obj, route: dict | None = None) -> tuple[float, bool] | None:
    """(volume, muted) straight out of a pw-dump object, or None.

    The values are already in the dump we just fetched — every node's
    `info.params.Props` carries them — so reading them here replaces one
    `wpctl get-volume` subprocess *per node and per stream, per poll*.  On a
    16-endpoint system that measured 463 ms -> 63 ms for a dashboard refresh.

    **A node on a card is read from its active `route`** (its device's Route
    param), when the caller passes one and it carries volumes.  That is what
    wpctl, pactl and WirePlumber's saved state all use, and the node's own
    Props can disagree with it: after **any profile switch on an ALSA card**
    the recreated nodes report `channelVolumes` 1.0 and `mute` false, while
    the route and the ALSA mixer keep the saved level and mute (only the
    software share of a level, `softVolumes`, is lost from the node).  The
    node catches up only when the level or mute really changes; a write of
    the same value leaves it behind (measured 2026-10-22, PipeWire
    1.6.9 / WirePlumber 0.5.18, `docs.local/probes/2026-10-22-route-volume/`).
    Bluetooth and a fresh session were in step.  Nodes with no route
    (virtual, chains, streams, Pro Audio) are read from their Props.

    Two things about the payload, both established by measurement against
    `wpctl get-volume` on PipeWire 1.6.8 rather than read anywhere:

    * Use **`channelVolumes`, not `volume`** — `volume` reads 1.0 on every node
      regardless of the actual level (it is a separate master prop), so it would
      silently report 100% for everything.
    * The volume is the **cube root of the loudest channel**.  wpctl reports
      the first channel instead (`channelVolumes = [0.027, 0.216]` reads back
      as 0.30, measured), and this used to copy it — but a master write scales
      from the loudest channel (`balance.scaled`), so a slider showing channel
      0 moved *down* when raised on a right-heavy balance (found on review,
      2026-10-09).  The loudest channel is also what pavucontrol and GNOME
      show.  On a level node, every node nobody has balanced, the two agree.
    """
    applied = _applied_channels(obj, route)
    if applied is None:
        return None
    raw, _positions, muted = applied
    return max(raw) ** (1 / 3), muted


def dump_channels(obj, route: dict | None = None
                  ) -> tuple[list[float], list[str]]:
    """(channels, channelMap) out of a pw-dump object, in the cubic UI scale.

    Separate from `dump_volume()`, whose 2-tuple has callers of its own.  The
    values are cube-rooted per channel, the same transform `dump_volume`
    applies, so `max(channels) == volume` for every node.  `route` as for
    `dump_volume()`.
    """
    applied = _applied_channels(obj, route)
    if applied is None:
        return [], []
    raw, positions, _muted = applied
    return [c ** (1 / 3) for c in raw], positions


def list_audio_nodes(dump=None) -> list[AudioNode]:
    dump = dump if dump is not None else pw_dump()
    defaults = read_default_names(dump)
    def_sink = defaults.get('default.audio.sink')
    def_source = defaults.get('default.audio.source')
    routes = _device_routes(dump)
    nodes = []
    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = (obj.get('info') or {}).get('props') or {}
        mclass = props.get('media.class', '')
        if mclass not in ('Audio/Sink', 'Audio/Source'):
            continue
        name = props.get('node.name', '')
        node = AudioNode(
            id=obj['id'], name=name,
            description=props.get('node.description') or props.get('node.nick') or name,
            media_class=mclass, props=props,
            serial=props.get('object.serial', -1),
            is_default=(name == def_sink if mclass == 'Audio/Sink'
                        else name == def_source))
        _attach_ports(node, routes)
        route = _active_route(node, routes)
        vol = dump_volume(obj, route)
        if vol:
            node.volume, node.muted = vol
        node.channels, node.channel_map = dump_channels(obj, route)
        node.state, node.fmt = _node_state(obj)
        nodes.append(node)
    return nodes


def _node_state(obj) -> tuple[str, dict]:
    """(info.state, the running format) out of one dump object."""
    info = obj.get('info') or {}
    state = info.get('state') or ''
    if not isinstance(state, str):
        state = ''
    fmt = {}
    formats = (info.get('params') or {}).get('Format') or []
    first = formats[0] if formats and isinstance(formats[0], dict) else {}
    for key in ('rate', 'channels', 'format'):
        if first.get(key) is not None:
            fmt[key] = first[key]
    return state, fmt


def card_map(dump=None) -> dict[int, tuple[str, str]]:
    """device.id -> (device.name, description) for every audio card.

    Lets a node row name the card it belongs to (node props carry device.id,
    not the card's name).
    """
    dump = dump if dump is not None else pw_dump()
    cards = {}
    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Device':
            continue
        props = (obj.get('info') or {}).get('props') or {}
        if props.get('media.class') != 'Audio/Device':
            continue
        name = props.get('device.name')
        if name:
            cards[obj['id']] = (name, props.get('device.description')
                                or props.get('device.nick') or name)
    return cards


# ----------------------------------------------------------------- streams --

STREAM_CLASSES = ('Stream/Output/Audio', 'Stream/Input/Audio')


@dataclass
class Stream:
    id: int
    serial: int
    name: str            # application.name / media.name / node.name
    media: str           # media.name ("what is playing")
    media_class: str
    icon: str | None = None
    binary: str | None = None
    props: dict = field(default_factory=dict)
    volume: float | None = None
    muted: bool = False
    channels: list = field(default_factory=list)      # per-channel, cubic (UI) scale
    channel_map: list = field(default_factory=list)   # 'FL', 'FR', 'AUX3', …
    target_id: int | None = None   # node id of the device it is linked to

    @property
    def is_playback(self):
        return self.media_class == 'Stream/Output/Audio'

    @property
    def is_app(self):
        """Is this something a person started, or is it our own plumbing?

        A filter chain, a loopback and a virtual device are all built from a
        capture node and a *playback stream*, so the graph reports each of
        them as `Stream/Output/Audio` — indistinguishable, by media class,
        from Firefox.  On this machine that put eight rows of infrastructure
        ("Everything output output", "Stream Mix (discarded) output") into a
        mixer holding three real apps.

        `node.link-group` is the discriminator, and it is the same one
        WirePlumber's own linking scripts use: PipeWire's filter-chain and
        loopback modules stamp it on both legs so the pair reads as one
        object, and nothing an application creates carries it.  Testing it
        rather than our `pwctl.` prefix also covers hand-written chains in
        `filter-chain.conf.d`, which are just as much not-an-app.
        """
        p = self.props
        return not (p.get('node.link-group')
                    or (p.get('node.name') or '').startswith('pwctl.'))


def list_streams(dump=None, apps_only=False) -> list[Stream]:
    """Application playback/recording streams with their current device.

    The connected device is derived from the live Link objects (ground
    truth), not from target.* metadata which may be stale or absent.

    `apps_only` drops the filter/loopback legs — see `Stream.is_app`.  It
    defaults to off because two callers want the plumbing: `enhance` finds an
    equalizer's own `.out` stream this way, and it is how the Patchbay-side
    code reasons about the whole graph.
    """
    dump = dump if dump is not None else pw_dump()
    classes = {}
    links = []
    for obj in dump:
        t = obj.get('type')
        if t == 'PipeWire:Interface:Node':
            p = (obj.get('info') or {}).get('props') or {}
            classes[obj['id']] = p.get('media.class', '')
        elif t == 'PipeWire:Interface:Link':
            i = obj.get('info') or {}
            links.append((i.get('output-node-id'), i.get('input-node-id')))
    streams = []
    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = (obj.get('info') or {}).get('props') or {}
        mclass = props.get('media.class', '')
        if mclass not in STREAM_CLASSES:
            continue
        # Level-meter taps, ours and other mixers', are not app streams.
        if props.get(levels.MARKER) or props.get('media.name') == 'Peak detect':
            continue
        name = (props.get('application.name') or props.get('media.name')
                or props.get('node.name', ''))
        s = Stream(id=obj['id'], serial=props.get('object.serial', -1),
                   name=name, media=props.get('media.name', ''),
                   media_class=mclass,
                   icon=props.get('application.icon-name'),
                   binary=props.get('application.process.binary'),
                   props=props)
        if apps_only and not s.is_app:
            continue
        if s.is_playback:
            s.target_id = next((dst for src, dst in links
                                if src == s.id
                                and classes.get(dst) == 'Audio/Sink'), None)
        else:
            # capture from a source, or from a sink's monitor ports
            s.target_id = next((src for src, dst in links
                                if dst == s.id
                                and classes.get(src) in ('Audio/Sink',
                                                         'Audio/Source')), None)
        vol = dump_volume(obj)
        if vol:
            s.volume, s.muted = vol
        s.channels, s.channel_map = dump_channels(obj)
        streams.append(s)
    streams.sort(key=lambda s: (s.name.lower(), s.id))
    return streams


def virtual_outputs(dump=None, nodes=None, streams=None) -> dict:
    """{virtual sink node.name -> (output stream id, destination node id)}.

    A filter chain, a loopback and a virtual device are each a *pair* of nodes
    carrying one `node.link-group`: the sink you play into, and the playback
    stream that carries the result back out.  Pairing on that rather than on a
    name convention like `<name>.out` is the whole point — it finds the output
    leg of a hand-written chain in `filter-chain.conf.d` (`effect_input.x` /
    `effect_output.x`) just as well as one we generated, and those are exactly
    the ones no page of ours can otherwise re-point.

    Two kinds of virtual sink are deliberately absent, because neither has an
    output anyone could choose: a virtual **microphone**'s sink leg, which
    feeds a source rather than a sink, and a leg whose output is discarded.
    The destination is None when the pair exists but is not linked anywhere.

    Pass `nodes`/`streams` when the caller already has them — the dashboard
    poll does, and re-parsing one pw-dump three times is the cost otherwise.
    `streams` must be the **unfiltered** list; `apps_only=True` drops exactly
    the legs this needs.
    """
    dump = dump if dump is not None else pw_dump()
    nodes = nodes if nodes is not None else list_audio_nodes(dump)
    streams = streams if streams is not None else list_streams(dump)

    by_group = {}
    for s in streams:
        group = s.props.get('node.link-group')
        if group:
            by_group.setdefault(group, s)

    outputs = {}
    for node in nodes:
        if not (node.is_virtual and node.is_sink):
            continue
        group = node.props.get('node.link-group')
        out = by_group.get(group) if group else None
        if out is not None:
            outputs[node.name] = (out.id, out.target_id)
    return outputs


def move_stream(stream_id: int, target_serial: int) -> bool:
    """Route a stream to another device; WirePlumber moves it live."""
    rc, _, _ = run(['pw-metadata', str(stream_id), 'target.object',
                    str(target_serial), 'Spa:Id'])
    return rc == 0


def driver_clock(dump=None) -> dict:
    """Current rate/quantum from the running driver node, if any."""
    dump = dump if dump is not None else pw_dump()
    for obj in dump:
        if obj.get('type') != 'PipeWire:Interface:Node':
            continue
        info = obj.get('info') or {}
        state = info.get('state')
        props = info.get('props') or {}
        if state == 'running' and props.get('media.class') in ('Audio/Sink', 'Audio/Source'):
            rate = props.get('clock.rate') or 0
            quantum = props.get('clock.quantum') or 0
            if rate and quantum:
                return {'rate': int(rate), 'quantum': int(quantum)}
    return {}


# ------------------------------------------------------------------- wpctl --

_VOL_RE = re.compile(r'Volume:\s*([\d.]+)\s*(\[MUTED\])?')


def get_volume(node_id):
    """One node's (volume, muted), by subprocess.

    No longer used by the list builders — they read the same values out of the
    dump via `dump_volume()`, which is why a poll no longer spawns a process per
    node.  Kept for the one-off case where there is no dump in hand.
    """
    rc, out, _ = run(['wpctl', 'get-volume', str(node_id)])
    if rc != 0:
        return None
    m = _VOL_RE.search(out)
    if not m:
        return None
    return float(m.group(1)), bool(m.group(2))


def set_volume(node_id, value: float) -> bool:
    """Every channel to one level, through `wpctl`.

    This flattens a balance.  Anything holding a node goes through
    `set_master()` instead, which keeps it; this stays for a write with no
    node in hand.
    """
    rc, _, _ = run(['wpctl', 'set-volume', str(node_id), f'{value:.2f}'])
    return rc == 0


def set_master(node, value: float) -> bool:
    """Move `node`'s level to `value` and keep its balance.

    `node` is an AudioNode or a Stream from a recent read.  On a level node
    (one nobody has balanced) this is the same `wpctl set-volume` argv as
    `set_volume()`, byte for byte.
    """
    chans = list(getattr(node, 'channels', None) or [])
    values = balance.scaled(chans, value) if chans else [balance.clamp(value)]
    return set_channel_volumes(node.id, values, serial=node.serial,
                               media_class=node.media_class)


def pulse_available() -> bool:
    """Does a PulseAudio server (pipewire-pulse) answer?

    Asked only after a `pactl` write failed, to tell "no server" from "no such
    node": the first means fall back to `pw-cli`, the second means the node is
    gone and nothing should be written.  Never cached.
    """
    rc, _, _ = run(['pactl', 'info'])
    return rc == 0


def pactl_verb(media_class: str) -> str | None:
    """The pactl volume command for a node of this class, or None.

    The class decides it, never trial and error: a sink and its monitor
    source share one index, so `set-source-volume <a sink's serial>` succeeds
    and sets the monitor (measured 2026-10-21).
    """
    mc = media_class or ''
    if mc.startswith('Stream/Output/Audio'):
        return 'set-sink-input-volume'
    if mc.startswith('Stream/Input/Audio'):
        return 'set-source-output-volume'
    if mc.startswith('Audio/Sink'):
        return 'set-sink-volume'
    if mc.startswith('Audio/Source'):
        return 'set-source-volume'
    return None


def set_channel_volumes(node_id, values: list[float], serial: int | None = None,
                        media_class: str = '') -> bool:
    """Write one value per channel.  `values` are in the cubic (UI) scale.

    **`values` must carry one entry per channel of the node.**  `pw-cli` takes
    a short array as the *mean* of what was sent, written to every channel,
    silently and with rc 0 (measured 2026-09-19).  The callers build the array
    from `balance.Levels`, which always holds the full set.

    Writers, measured 2026-09-19 and 2026-10-21:

    * **Level values go through `wpctl set-volume`**, the argv the app has
      always used, so nothing changes for a node nobody has balanced.
    * **Anything else through `pactl`, addressed by `object.serial`**, which
      is pactl's index for sinks, sources and both kinds of stream.
      WirePlumber saves the result and restores it when the node comes back
      (hardware in `default-routes`, everything else in `stream-properties`).
      pactl refuses a wrong-length array, unlike `pw-cli`.  The integer form
      is `(n / 65536) ** 3` raw, so a rescale doesn't walk in 1% steps.
    * **`pw-cli` only when no pipewire-pulse answers** (no `pactl` binary,
      or no server), and then to the right object (measured 2026-10-22):
      a node on a card with an active Route gets the **card's Route** written
      (`save: true`), as pipewire-pulse does.  Writing such a node's own
      Props changes only the numbers it reports: no mixer, no software
      volume, nothing saved, on ALSA and Bluetooth alike.  A node with no
      route (virtual, chains, Pro Audio) gets its Props, which there is
      exactly what pactl does and is saved too.  See `_fallback_write`.
    """
    if not values:
        return False
    seq = _next_write(node_id)
    vals = [balance.clamp(v) for v in values]
    if balance.is_level(vals):
        rc, _, _ = run(['wpctl', 'set-volume', str(node_id), f'{vals[0]:.2f}'])
        return rc == 0

    verb = pactl_verb(media_class)
    if verb and serial is not None and serial >= 0:
        ints = [str(round(v * 65536)) for v in vals]
        rc, _, _ = run(['pactl', verb, str(serial)] + ints)
        if rc == 0:
            return True
        if pulse_available():
            # The server answered and still refused: the node is gone or has
            # a different channel count.  pw-cli would average the array.
            return False

    return _fallback_write(node_id, vals, seq)


# The pw-cli fallback is slower than the other writers (a pw-dump to find the
# route, then pw-cli: ~30 ms against a drag's 8-16 ms steps), so a drag's
# threads can overlap there.  Each write takes a number on entry; the fallback
# writes one at a time and drops a write a newer one for the same node has
# already replaced, so the device ends on the drag's last value.
_WRITE_LOCK = threading.Lock()
_FALLBACK_LOCK = threading.Lock()
_LATEST: dict = {}


def _next_write(node_id) -> int:
    with _WRITE_LOCK:
        _LATEST[node_id] = _LATEST.get(node_id, 0) + 1
        return _LATEST[node_id]


def _superseded(node_id, seq) -> bool:
    with _WRITE_LOCK:
        return _LATEST.get(node_id, 0) != seq


def _card_route(node_id, dump=None):
    """(card object id, active Route) for a node on a card, else None.

    Read from a fresh dump at write time: card and route ids change with
    every profile switch, so nothing here is remembered.
    """
    dump = dump if dump is not None else pw_dump()
    obj = next((o for o in dump if o.get('id') == node_id
                and o.get('type') == 'PipeWire:Interface:Node'), None)
    if obj is None:
        return None
    props = (obj.get('info') or {}).get('props') or {}
    node = AudioNode(id=node_id, name=props.get('node.name', ''),
                     description='', media_class=props.get('media.class', ''),
                     props=props)
    route = _active_route(node, _device_routes(dump))
    if route is None:
        return None
    return int(props['device.id']), route


def _fallback_write(node_id, vals, seq) -> bool:
    """The pw-cli write, to the card's Route when the node has one."""
    raw = ', '.join(f'{v ** 3:.6f}' for v in vals)
    target = _card_route(node_id)
    with _FALLBACK_LOCK:
        if _superseded(node_id, seq):
            return True                 # a newer write for this node follows
        if target is None:
            rc, _, _ = run(['pw-cli', 'set-param', str(node_id), 'Props',
                            '{ channelVolumes: [ %s ] }' % raw])
            return rc == 0
        card_id, route = target
        have = ((route.get('props') or {}).get('channelVolumes') or [])
        if have and len(have) != len(vals):
            # pactl refuses a wrong-length array; a short one written here
            # would be averaged (as pw-cli does on Props), so refuse too.
            return False
        rc, _, _ = run(['pw-cli', 'set-param', str(card_id), 'Route',
                        '{ index: %d, device: %d, props: { channelVolumes: '
                        '[ %s ] }, save: true }'
                        % (route['index'], route['device'], raw)])
        return rc == 0


def set_mute(node_id, mute: bool) -> bool:
    rc, _, _ = run(['wpctl', 'set-mute', str(node_id), '1' if mute else '0'])
    return rc == 0


def set_default(node_id) -> bool:
    rc, _, _ = run(['wpctl', 'set-default', str(node_id)])
    return rc == 0


def set_route(node_id, route_index: int) -> bool:
    """Switch a device node's port (route), e.g. speakers -> headphones."""
    rc, _, _ = run(['wpctl', 'set-route', str(node_id), str(route_index)])
    return rc == 0


def pipewire_version() -> str:
    rc, out, _ = run(['pipewire', '--version'])
    m = re.search(r'libpipewire\s+([\d.]+)', out)
    return m.group(1) if m else '?'
