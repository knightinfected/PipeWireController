"""Read merged PipeWire/WirePlumber config values and write drop-in overrides.

All writes go to our own drop-in files (99-pipewire-controller.conf) in the
user's config dirs — base files are never touched, and removing an override
simply drops the key from our drop-in.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import spa_json

XDG_CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))

PW_DIRS = [Path('/usr/share/pipewire'), Path('/etc/pipewire'), XDG_CONFIG / 'pipewire']
WP_DIRS = [Path('/usr/share/wireplumber'), Path('/etc/wireplumber'), XDG_CONFIG / 'wireplumber']

DROPIN_NAME = '99-pipewire-controller.conf'
HEADER = ('Managed by PipeWire Controller — do not edit by hand.\n'
          'Remove this file to drop all overrides made by the app.')


def _conf_files(conf_name: str, dirs) -> list[Path]:
    """All files PipeWire would read for conf_name, in application order."""
    base = None
    for d in reversed(dirs):          # user > /etc > /usr/share for the base
        p = d / conf_name
        if p.is_file():
            base = p
            break
    dropins: dict[str, Path] = {}
    for d in dirs:                     # same filename: later dir wins
        dd = d / (conf_name + '.d')
        if dd.is_dir():
            for f in dd.iterdir():
                if f.is_file() and f.name.endswith('.conf'):
                    dropins[f.name] = f
    files = [base] if base else []
    files += [dropins[k] for k in sorted(dropins)]
    return files


def read_merged_section(conf_name: str, section: str, dirs=PW_DIRS) -> dict:
    """Merged key/value dict for a properties section (later files win)."""
    merged: dict = {}
    for f in _conf_files(conf_name, dirs):
        try:
            data = spa_json.load_file(f)
        except (OSError, spa_json.SpaJsonError):
            continue
        sec = data.get(section)
        if isinstance(sec, dict):
            merged.update(sec)
    return merged


def value_source(conf_name: str, section: str, key: str, dirs=PW_DIRS):
    """Which file last set this key (None = built-in default)."""
    src = None
    for f in _conf_files(conf_name, dirs):
        try:
            data = spa_json.load_file(f)
        except (OSError, spa_json.SpaJsonError):
            continue
        sec = data.get(section)
        if isinstance(sec, dict) and key in sec:
            src = f
    return src


# ------------------------------------------------------------------ writes --

def _dropin_path(conf_name: str, dirs) -> Path:
    return dirs[-1] / (conf_name + '.d') / DROPIN_NAME


def read_our_dropin(conf_name: str, dirs=PW_DIRS) -> dict:
    p = _dropin_path(conf_name, dirs)
    if not p.is_file():
        return {}
    try:
        return spa_json.load_file(p)
    except spa_json.SpaJsonError:
        return {}


def write_our_dropin(conf_name: str, data: dict, dirs=PW_DIRS):
    p = _dropin_path(conf_name, dirs)
    # prune empty sections
    data = {k: v for k, v in data.items() if v not in ({}, [], None)}
    if not data:
        if p.is_file():
            p.unlink()
        return
    from .system import atomic_write
    atomic_write(p, spa_json.dumps(data, header=HEADER))


def write_our_dropin_section(conf_name: str, data: dict, dirs=PW_DIRS,
                             owned: tuple = ()):
    """Replace the `owned` sections of our drop-in with `data`, leaving any
    other sections (written by other parts of the app) untouched."""
    cur = read_our_dropin(conf_name, dirs)
    for key in owned:
        cur.pop(key, None)
    cur.update(data)
    write_our_dropin(conf_name, cur, dirs)


def set_override(conf_name: str, section: str, key: str, value, dirs=PW_DIRS):
    """Set (or with value=None remove) one key in our drop-in."""
    data = read_our_dropin(conf_name, dirs)
    sec = data.setdefault(section, {})
    if value is None:
        sec.pop(key, None)
    else:
        sec[key] = value
    write_our_dropin(conf_name, data, dirs)


def get_override(conf_name: str, section: str, key: str, dirs=PW_DIRS):
    return read_our_dropin(conf_name, dirs).get(section, {}).get(key)


def clear_all_overrides():
    """Delete every drop-in the app has written (plus the state files they
    are regenerated from, so they don't come back). Returns removed paths."""
    removed = []
    for conf, dirs in (('pipewire.conf', PW_DIRS), ('client.conf', PW_DIRS),
                       ('pipewire-pulse.conf', PW_DIRS),
                       ('wireplumber.conf', WP_DIRS)):
        p = _dropin_path(conf, dirs)
        if p.is_file():
            p.unlink()
            removed.append(str(p))
    from .rules import RULES_PATH
    for state in (WP_STATE, RULES_PATH):
        if state.is_file():
            state.unlink()
            removed.append(str(state))
    return removed


# ----------------------------------------------------- wireplumber toggles --
# WirePlumber settings are structured (rules/monitor sections), so the drop-in
# is regenerated from a small app-level toggle state file.

import json as _json

WP_STATE = XDG_CONFIG / 'pipewire-controller' / 'wireplumber-toggles.json'

WP_DEFAULTS = {
    'disable_suspend': False,      # keep ALSA nodes always active (no pops)
    'sbc_xq': False,               # bluez5.enable-sbc-xq
    'msbc': True,                  # bluez5.enable-msbc (headset mic quality)
    'bt_hw_volume': True,          # bluez5.enable-hw-volume
    'bt_autoswitch': True,         # bluez5.autoswitch-profile
    'alsa_headroom': False,        # api.alsa.headroom 1024 (USB crackle fix)
}


def read_wp_toggles() -> dict:
    state = dict(WP_DEFAULTS)
    if WP_STATE.is_file():
        try:
            state.update(_json.loads(WP_STATE.read_text()))
        except ValueError:
            pass
    return state


def write_wp_toggles(state: dict):
    from .system import atomic_write
    atomic_write(WP_STATE, _json.dumps(state, indent=2))
    # the WirePlumber drop-in combines these toggles with per-device rules,
    # so regeneration lives in rules.py
    from . import rules
    rules.regen_all()
