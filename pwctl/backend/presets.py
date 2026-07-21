"""Per-device presets: snapshot and restore audio settings per output device.

A preset is keyed by the sink's node.name and stores the channel-mix
overrides, sink volume and the card profile that were active when it was
saved. With autoload enabled, the app applies the matching preset whenever
the default output changes to that device.
"""

from __future__ import annotations

import time

from . import config, prefs, pw, surround

STREAM_CONFS = ('client.conf', 'pipewire-pulse.conf')


def all_presets() -> dict:
    return prefs.get('device_presets') or {}


def delete(device_name: str):
    presets = all_presets()
    presets.pop(device_name, None)
    prefs.save(device_presets=presets)


def snapshot() -> dict | None:
    """Capture current settings for the default output device."""
    dump = pw.pw_dump()
    nodes = pw.list_audio_nodes(dump)
    sink = next((n for n in nodes if n.is_sink and n.is_default), None)
    if not sink:
        return None
    upmix = {}
    for key in surround.UPMIX_KEYS:
        val = config.get_override('client.conf', 'stream.properties', key)
        if val is not None:
            upmix[key] = val
    card = next((c for c in surround.list_cards(dump)
                 if str(c.id) == str(sink.props.get('device.id'))), None)
    preset = {
        'device': sink.name,
        'description': sink.description,
        'volume': sink.volume,
        'upmix': upmix,
        'card_name': card.name if card else None,
        'profile': card.active_profile if card else None,
        'saved': time.strftime('%Y-%m-%d %H:%M'),
    }
    presets = all_presets()
    presets[sink.name] = preset
    prefs.save(device_presets=presets)
    return preset


def apply(preset: dict) -> list[str]:
    """Apply a preset; returns human-readable list of what was done."""
    done = []
    upmix = preset.get('upmix') or {}
    if upmix:
        for key, value in upmix.items():
            for conf in STREAM_CONFS:
                config.set_override(conf, 'stream.properties', key, value)
        done.append(f'{len(upmix)} channel-mix settings (restart to apply)')

    dump = pw.pw_dump()
    if preset.get('card_name') and preset.get('profile') is not None:
        card = next((c for c in surround.list_cards(dump)
                     if c.name == preset['card_name']), None)
        if card and card.active_profile != preset['profile']:
            if surround.set_profile(card.id, preset['profile']):
                done.append('card profile')

    if preset.get('volume') is not None:
        for obj in dump:
            if obj.get('type') != 'PipeWire:Interface:Node':
                continue
            props = (obj.get('info') or {}).get('props') or {}
            if props.get('node.name') == preset['device']:
                pw.set_volume(obj['id'], preset['volume'])
                done.append(f'volume {preset["volume"] * 100:.0f}%')
                break
    return done


def preset_for(device_name: str) -> dict | None:
    return all_presets().get(device_name)
