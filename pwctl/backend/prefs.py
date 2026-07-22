"""Small persistent UI preferences (ui.json in the app config dir)."""

from __future__ import annotations

import json
import threading

from .config import XDG_CONFIG
from .system import atomic_write

PREFS_PATH = XDG_CONFIG / 'pipewire-controller' / 'ui.json'
_LOCK = threading.Lock()

DEFAULTS = {
    'volume_style': 'classic',   # classic | stepped | precision | meter
    'advanced': False,           # show advanced settings across the app
    'surround_layout': '5.1',    # last chosen layout on the Surround page
    'autoload_presets': False,   # apply device preset when default changes
    'device_presets': {},        # node.name -> preset dict (backend/presets)
    'last_page': 'dashboard',    # restored on startup
    'dashboard_tab': 'overview',
    'win_width': 1080,
    'win_height': 760,
    'win_maximized': False,
    'graph_positions': {},       # node.name -> [x, y] on the patchbay
    'notify_links': False,       # desktop notification on broken links
    'notify_services': True,     # … on failed audio services
    'notify_xruns': False,       # … on new xruns (Monitor page polling)
}


def load() -> dict:
    try:
        data = json.loads(PREFS_PATH.read_text())
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **data}


def save(**updates):
    with _LOCK:
        prefs = load()
        prefs.update(updates)
        atomic_write(PREFS_PATH, json.dumps(prefs, indent=2) + '\n')
    return prefs


def get(key: str):
    return load().get(key, DEFAULTS.get(key))
