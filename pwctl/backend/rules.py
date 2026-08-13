"""Persistent node/device rules and per-application policies.

State lives in rules.json; from it we regenerate:
  * the WirePlumber drop-in (together with the toggle state from config.py):
    per-device rename / hide / audio format / rate / period-size / headroom /
    suspend timeout / session & driver priority, split into monitor.alsa.rules
    and monitor.bluez.rules by node-name prefix;
  * the stream.rules sections of the client.conf and pipewire-pulse.conf
    drop-ins: per-application target device, auto-connect behaviour, etc.

Everything follows the app's core rule: drop-ins only, never base files.
"""

from __future__ import annotations

import json
import re

from . import config, pw
from .system import atomic_write

RULES_PATH = config.XDG_CONFIG / 'pipewire-controller' / 'rules.json'

# node props the per-device UI exposes (key, title, subtitle, kind, extra)
DEVICE_PROP_SCHEMA = [
    ('audio.rate', 'Sample rate', 'Fixed rate for this device only. '
     '0 = follow the graph.', 'enum',
     [0, 44100, 48000, 88200, 96000, 176400, 192000]),
    ('audio.format', 'Bit depth / sample format',
     'Force the ALSA sample format. Auto negotiates the best available.',
     'enum', ['auto', 'S16LE', 'S24LE', 'S24_32LE', 'S32LE', 'F32LE']),
    ('api.alsa.period-size', 'Period size (device buffer)',
     'ALSA period in frames — the hardware chunk size underneath the '
     'quantum. 0 = driver default.', 'enum',
     [0, 64, 128, 256, 512, 1024, 2048]),
    ('api.alsa.headroom', 'Headroom (frames)',
     'Extra buffered frames — raise to fix crackling USB interfaces.',
     'enum', [0, 64, 128, 256, 512, 1024, 2048]),
    ('node.latency', 'Preferred quantum',
     'Ask the graph for this quantum while the device is in use '
     '(e.g. 256/48000). Empty = no preference.', 'latency', None),
    ('priority.session', 'Session priority',
     'Higher priority wins automatic default-device selection.', 'int',
     (0, 5000)),
    ('priority.driver', 'Clock master priority',
     'Higher priority makes this device drive the graph clock.', 'int',
     (0, 30000)),
    ('session.suspend-timeout-seconds', 'Suspend timeout (s)',
     'Seconds of silence before the device suspends. 0 = never suspend.',
     'int', (0, 60)),
]


def load() -> dict:
    data = {'nodes': {}, 'apps': [], 'devices': {}}
    try:
        stored = json.loads(RULES_PATH.read_text())
        if isinstance(stored.get('nodes'), dict):
            data['nodes'] = stored['nodes']
        if isinstance(stored.get('apps'), list):
            data['apps'] = stored['apps']
        if isinstance(stored.get('devices'), dict):
            data['devices'] = stored['devices']
    except (OSError, ValueError):
        pass
    return data


def save(data: dict):
    atomic_write(RULES_PATH, json.dumps(data, indent=2) + '\n')
    regen_all()


# --------------------------------------------------------------- node rules --

def node_rule(node_name: str) -> dict:
    return load()['nodes'].get(node_name, {})


def set_node_rule(node_name: str, rename=None, hide=None, props=None,
                  desc=None):
    """Update one node's rule; empty rules are removed entirely.

    desc is the node's description at the time of writing, remembered only so
    the un-hide list can show a readable name: a hidden node is refused by
    WirePlumber, so it never appears in the graph again to be looked up.
    """
    data = load()
    rule = data['nodes'].get(node_name, {})
    if desc:
        rule['desc'] = desc
    if rename is not None:
        if rename:
            rule['rename'] = rename
        else:
            rule.pop('rename', None)
    if hide is not None:
        if hide:
            rule['hide'] = True
        else:
            rule.pop('hide', None)
    if props is not None:
        cur = rule.get('props', {})
        for k, v in props.items():
            if v in (None, '', 'auto', 0) and k != 'session.suspend-timeout-seconds':
                cur.pop(k, None)
            elif v in (None, ''):
                cur.pop(k, None)
            else:
                cur[k] = v
        if cur:
            rule['props'] = cur
        else:
            rule.pop('props', None)
    # 'desc' alone is bookkeeping, not a customization — a rule holding
    # nothing else is dropped, so the row stops showing as customized.
    if any(k in rule for k in ('rename', 'hide', 'props')):
        data['nodes'][node_name] = rule
    else:
        data['nodes'].pop(node_name, None)
    save(data)


def clear_node_rule(node_name: str):
    data = load()
    if node_name in data['nodes']:
        del data['nodes'][node_name]
        save(data)


# --------------------------------------------------------------- card rules --

def device_rule(device_name: str) -> dict:
    return load()['devices'].get(device_name, {})


def set_device_rule(device_name: str, hide=None, desc=None):
    """Hide a whole sound card.

    Node rules disable one endpoint (WirePlumber refuses to create that node),
    which is why a hidden output leaves its card behind in the desktop's sound
    settings, profile switcher and all.  Disabling the *device* removes the
    card itself, and with it every input and output it would have published.
    """
    data = load()
    rule = data['devices'].get(device_name, {})
    if desc:
        rule['desc'] = desc
    if hide is not None:
        if hide:
            rule['hide'] = True
        else:
            rule.pop('hide', None)
    if rule.get('hide'):
        data['devices'][device_name] = rule
    else:
        data['devices'].pop(device_name, None)
    save(data)


def clear_device_rule(device_name: str):
    data = load()
    if device_name in data['devices']:
        del data['devices'][device_name]
        save(data)


def hidden_entries() -> list[dict]:
    """Everything currently hidden, for the un-hide list on the Devices page.

    Hidden objects are absent from the graph by definition, so this is the
    only place they can be listed — and their label has to come from what we
    stored when hiding them.
    """
    data = load()
    out = [{'kind': 'device', 'key': name, 'label': rule.get('desc') or name}
           for name, rule in sorted(data['devices'].items())
           if rule.get('hide')]
    out += [{'kind': 'node', 'key': name, 'label': rule.get('desc') or name}
            for name, rule in sorted(data['nodes'].items())
            if rule.get('hide')]
    return out


# ---------------------------------------------------------------- app rules --

# A stream's direction, as the media.class every client sets on its node.
# Adding this to a match makes "speakers for Teams" and "this mic for Teams"
# two distinct rules instead of one that overwrites the other (issue #7).
DIR_PLAYBACK = 'Stream/Output/Audio'
DIR_RECORDING = 'Stream/Input/Audio'


def app_rules() -> list[dict]:
    return load()['apps']


def rule_direction(rule: dict) -> str:
    """'Stream/Output/Audio', 'Stream/Input/Audio' or '' for either."""
    return (rule.get('match') or {}).get('media.class', '')


def rule_enabled(rule: dict) -> bool:
    """Absent means on, so every rule written before this existed stays on."""
    return rule.get('enabled', True)


def set_app_rule_enabled(index: int, on: bool):
    data = load()
    if 0 <= index < len(data['apps']):
        data['apps'][index]['enabled'] = bool(on)
        save(data)


# ------------------------------------------------------- matching, locally --
#
# The same matching PipeWire does, so the app can answer "which streams does
# this rule cover *right now*" without waiting for them to be recreated.
# All three branches were verified against pipewire 1.6.8 with a probe
# drop-in rather than read off the documentation, because the doc line
# ("all keys must match the value. ! negates. ~ starts regex") does not say
# where the prefixes go or what happens when the property is missing.

def _value_matches(pattern: str, actual) -> bool:
    """One key of a match object.

    Grammar, in this order: a leading '!' negates, then a leading '~' makes
    the rest a regex; anything else compares literally. A property that is
    absent never matches on its own — but negation still flips that, so
    '!foo' matches a stream that has no such property at all. That last one
    is measured behaviour, not an assumption.
    """
    negate = pattern.startswith('!')
    if negate:
        pattern = pattern[1:]
    if actual is None:
        hit = False
    elif pattern.startswith('~'):
        try:
            hit = re.search(pattern[1:], str(actual)) is not None
        except re.error:          # a half-typed regex must not raise here
            hit = False
    else:
        hit = str(actual) == pattern
    return hit != negate


def split_pattern(value: str) -> tuple[bool, bool, str]:
    """A stored match value taken apart: (negate, is_regex, text)."""
    negate = value.startswith('!')
    if negate:
        value = value[1:]
    is_regex = value.startswith('~')
    if is_regex:
        value = value[1:]
    return negate, is_regex, value


def join_pattern(negate: bool, is_regex: bool, text: str) -> str:
    """The inverse of split_pattern — prefixes in the order PipeWire reads."""
    return ('!' if negate else '') + ('~' if is_regex else '') + text


def rule_matches(rule: dict, props: dict) -> bool:
    """True when every key of the rule's match object matches (they are ANDed)."""
    match = rule.get('match') or {}
    if not match:
        return False
    return all(_value_matches(str(v), props.get(k)) for k, v in match.items())


def matching_streams(rule: dict, streams: list) -> list:
    """The live streams a rule covers, in the order they were reported."""
    return [s for s in streams if rule_matches(rule, s.props)]


def apply_rule_to_streams(rule: dict, streams: list, nodes: list) -> int:
    """Move every already-running stream the rule matches onto its target.

    stream.rules are read by a client when it *creates* a stream, so a new
    or edited rule otherwise does nothing until the app is restarted — the
    single most common "it didn't work". Applying it to what is already
    playing is the same move the Dashboard's device dropdown performs.
    Returns how many streams were actually moved.
    """
    target = (rule.get('props') or {}).get('target.object')
    if not target or not rule_enabled(rule):
        return 0
    node = next((n for n in nodes if n.name == target), None)
    if node is None:                      # target unplugged — nothing to do
        return 0
    moved = 0
    for s in matching_streams(rule, streams):
        if s.target_id == node.id:        # already there
            continue
        if pw.move_stream(s.id, node.serial):
            moved += 1
    return moved


def rule_problem(index: int, rules_list: list, nodes: list) -> str:
    """Why a rule will not do anything, in a few words, or '' if it is fine.

    Deliberately reports rather than repairs: silently dropping a rule whose
    device is unplugged would throw the user's routing away the first time
    they undocked a laptop.
    """
    rule = rules_list[index]
    if not rule_enabled(rule):
        return ''                          # the switch already says so
    match = rule.get('match') or {}
    for earlier in rules_list[:index]:
        if rule_enabled(earlier) and (earlier.get('match') or {}) == match:
            return 'overridden by an identical rule above'
    target = (rule.get('props') or {}).get('target.object')
    if target and not any(n.name == target for n in nodes):
        return 'target device not connected'
    return ''


def stream_match(stream) -> dict:
    """The match object identifying one running stream's application.

    Same preference order the rule dialog uses when you pick a running app:
    the friendly application name, else the binary, else the node name.
    """
    props = getattr(stream, 'props', {}) or {}
    for key in ('application.name', 'application.process.binary', 'node.name'):
        value = props.get(key)
        if value:
            return {key: value,
                    'media.class': getattr(stream, 'media_class', '')}
    return {}


def bind_stream_to_node(stream, node_name: str) -> dict:
    """Persist "this app plays into that node" as an application rule.

    Sending an app somewhere is otherwise a live `target.object` write that
    is gone at the next restart — this is what makes it stick. Any other
    actions already set for the same app (auto-connect, pin) are preserved.
    """
    match = stream_match(stream)
    if not match or not match.get('media.class'):
        return {}
    existing = next((r for r in app_rules() if r.get('match') == match), None)
    props = dict((existing or {}).get('props') or {})
    props['target.object'] = node_name
    upsert_app_rule(match, props, old_match=match,
                    enabled=rule_enabled(existing or {}))
    return match


def stream_is_bound(stream, node_name: str) -> bool:
    """True when a live rule already sends this app to that node."""
    props = getattr(stream, 'props', {}) or {}
    return any(rule_enabled(r) and rule_matches(r, props) and
               (r.get('props') or {}).get('target.object') == node_name
               for r in app_rules())


def unbind_stream(stream) -> bool:
    """Drop the persistent destination for this app, keeping its other actions.

    The rule goes away entirely if the destination was all it had, so
    un-keeping an app leaves nothing behind to puzzle over later.
    """
    match = stream_match(stream)
    rule = next((r for r in app_rules() if r.get('match') == match), None)
    if rule is None:
        return False
    props = dict(rule.get('props') or {})
    if props.pop('target.object', None) is None:
        return False
    upsert_app_rule(match, props, old_match=match,
                    enabled=rule_enabled(rule))
    return True


def upsert_app_rule(match: dict, props: dict, old_match: dict | None = None,
                    enabled: bool = True):
    """Add or replace one application rule.

    `match` is a whole match object, not a single key: PipeWire requires
    *all* of its keys to match, so an app key plus media.class addresses one
    direction of one app. Two such rules coexist, which is the point.

    `old_match` is the match the rule had before an edit. Pass it whenever
    the dialog could have changed the match itself — otherwise editing a
    rule's app name or direction appends a second rule and leaves the
    original behind.
    """
    match = {k: v for k, v in match.items() if v}
    data = load()
    stale = [m for m in (match, old_match) if m]
    data['apps'] = [r for r in data['apps'] if r.get('match') not in stale]
    if props and match:
        entry = {'match': match, 'props': props}
        # Only written when off, so an untouched rules.json keeps its shape
        # and a rule that predates the switch is still read as enabled.
        if not enabled:
            entry['enabled'] = False
        data['apps'].append(entry)
    save(data)


def delete_app_rule(index: int):
    data = load()
    if 0 <= index < len(data['apps']):
        del data['apps'][index]
        save(data)


# ------------------------------------------------------------- regeneration --

def _update_props_for(rule: dict) -> dict:
    props = dict(rule.get('props', {}))
    if rule.get('rename'):
        props['node.description'] = rule['rename']
        props['node.nick'] = rule['rename']
    if rule.get('hide'):
        props['node.disabled'] = True
    if props.get('audio.format') == 'auto':
        del props['audio.format']
    return props


def wireplumber_data() -> dict:
    """The complete WirePlumber drop-in content: toggles + node rules."""
    state = config.read_wp_toggles()
    alsa_rules, bluez_rules = [], []

    if state.get('disable_suspend'):
        alsa_rules.append({
            'matches': [{'node.name': '~alsa_input.*'},
                        {'node.name': '~alsa_output.*'}],
            'actions': {'update-props': {
                'session.suspend-timeout-seconds': 0}},
        })
    if state.get('alsa_headroom'):
        alsa_rules.append({
            'matches': [{'node.name': '~alsa_output.*'}],
            'actions': {'update-props': {'api.alsa.headroom': 1024}},
        })

    stored = load()
    for name, rule in sorted(stored['nodes'].items()):
        props = _update_props_for(rule)
        if not props:
            continue
        entry = {'matches': [{'node.name': name}],
                 'actions': {'update-props': props}}
        if name.startswith('bluez_'):
            bluez_rules.append(entry)
        else:
            alsa_rules.append(entry)

    # Whole-card hides match the device, not its nodes — see set_device_rule.
    for name, rule in sorted(stored['devices'].items()):
        if not rule.get('hide'):
            continue
        entry = {'matches': [{'device.name': name}],
                 'actions': {'update-props': {'device.disabled': True}}}
        if name.startswith('bluez_'):
            bluez_rules.append(entry)
        else:
            alsa_rules.append(entry)

    data = {}
    if alsa_rules:
        data['monitor.alsa.rules'] = alsa_rules
    if bluez_rules:
        data['monitor.bluez.rules'] = bluez_rules

    bt = {}
    defaults = config.WP_DEFAULTS
    if state.get('sbc_xq') != defaults['sbc_xq']:
        bt['bluez5.enable-sbc-xq'] = state['sbc_xq']
    if state.get('msbc') != defaults['msbc']:
        bt['bluez5.enable-msbc'] = state['msbc']
    if state.get('bt_hw_volume') != defaults['bt_hw_volume']:
        bt['bluez5.enable-hw-volume'] = state['bt_hw_volume']
    if bt:
        data['monitor.bluez.properties'] = bt
    if state.get('bt_autoswitch') != defaults['bt_autoswitch']:
        data['wireplumber.settings'] = {
            'bluetooth.autoswitch-to-headset-profile': state['bt_autoswitch']}
    return data


def stream_rules() -> list[dict]:
    """client.conf / pipewire-pulse.conf stream.rules from the app policies."""
    out = []
    for rule in app_rules():
        match = rule.get('match') or {}
        props = dict(rule.get('props') or {})
        if not match or not props or not rule_enabled(rule):
            continue
        out.append({'matches': [match],
                    'actions': {'update-props': props}})
    return out


def regen_all():
    """Rewrite every generated drop-in section owned by this module."""
    config.write_our_dropin_section('wireplumber.conf', wireplumber_data(),
                                    config.WP_DIRS,
                                    owned=('monitor.alsa.rules',
                                           'monitor.bluez.rules',
                                           'monitor.bluez.properties',
                                           'wireplumber.settings'))
    rules = stream_rules()
    for conf in ('client.conf', 'pipewire-pulse.conf'):
        data = config.read_our_dropin(conf, config.PW_DIRS)
        if rules:
            data['stream.rules'] = rules
        else:
            data.pop('stream.rules', None)
        config.write_our_dropin(conf, data, config.PW_DIRS)
