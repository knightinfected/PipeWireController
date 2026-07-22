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

from . import config
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
    data = {'nodes': {}, 'apps': []}
    try:
        stored = json.loads(RULES_PATH.read_text())
        if isinstance(stored.get('nodes'), dict):
            data['nodes'] = stored['nodes']
        if isinstance(stored.get('apps'), list):
            data['apps'] = stored['apps']
    except (OSError, ValueError):
        pass
    return data


def save(data: dict):
    atomic_write(RULES_PATH, json.dumps(data, indent=2) + '\n')
    regen_all()


# --------------------------------------------------------------- node rules --

def node_rule(node_name: str) -> dict:
    return load()['nodes'].get(node_name, {})


def set_node_rule(node_name: str, rename=None, hide=None, props=None):
    """Update one node's rule; empty rules are removed entirely."""
    data = load()
    rule = data['nodes'].get(node_name, {})
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
    if rule:
        data['nodes'][node_name] = rule
    else:
        data['nodes'].pop(node_name, None)
    save(data)


def clear_node_rule(node_name: str):
    data = load()
    if node_name in data['nodes']:
        del data['nodes'][node_name]
        save(data)


# ---------------------------------------------------------------- app rules --

def app_rules() -> list[dict]:
    return load()['apps']


def upsert_app_rule(match_key: str, match_value: str, props: dict):
    """Add or replace the rule matching one application."""
    data = load()
    data['apps'] = [r for r in data['apps']
                    if r.get('match') != {match_key: match_value}]
    if props:
        data['apps'].append({'match': {match_key: match_value},
                             'props': props})
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

    for name, rule in sorted(load()['nodes'].items()):
        props = _update_props_for(rule)
        if not props:
            continue
        entry = {'matches': [{'node.name': name}],
                 'actions': {'update-props': props}}
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
        if not match or not props:
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
