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
    # Sidebar visibility, remembered across restarts.  Only consulted while
    # the window is wide enough to hold the sidebar beside the content: the
    # breakpoint owns the narrow case, and a docked window should come back
    # docked rather than re-opening the sidebar over its own content.
    'sidebar_shown': True,
    'dashboard_tab': 'overview',
    # Dashboard → Overview board layout.  Card *ids*, never indices: an index
    # moves under the user the moment a card is added or removed upstream.
    # Both lists are advisory — an id that no longer exists is ignored, and a
    # card missing from `dashboard_order` keeps its built-in position — so a
    # card added in a later release turns up where its author put it instead
    # of silently vanishing from an arranged board.
    'dashboard_order': [],       # card ids, in the order the user chose
    'dashboard_hidden': [],      # card ids the user has put away
    # Dashboard → Overview → Favourites, as a list of `node.name`.  Names, not
    # ids or serials: an id is recycled and a serial changes every time a node
    # is recreated, so only the name survives a reboot or a chain restart —
    # the same reasoning routing snapshots are built on.
    'favorite_devices': [],
    # node.name -> the description it had when last seen, so a device that is
    # unplugged still reads as "Scarlett Solo" rather than as its ALSA node
    # name.  Learned, never authoritative: the live description always wins.
    'favorite_labels': {},
    'win_width': 1080,
    'win_height': 760,
    'win_maximized': False,
    'graph_positions': {},       # node.name -> [x, y] on the patchbay
    'notify_links': False,       # desktop notification on broken links
    'notify_services': True,     # … on failed audio services
    'notify_xruns': False,       # … on new xruns (Monitor page polling)
    'monitor_poll': 1,           # seconds between Monitor page samples
    'eq_ab_compare': False,      # experimental live A/B on the Equalizer page
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
