"""The app's own preferences and UI state (ui.json in the app config dir).

`PREFS` is the registry: every key the file may hold, its default, and what
kind of thing it is (`store`):

* ``pref``  — a choice the user makes, shown in Preferences.  *Reset
  preferences* clears these and nothing else.
* ``state`` — where the user was (window size, last page, folds).
* ``data``  — things the user built (favourites, Device Presets, the Patchbay
  layout).  Never cleared by a reset.

A key is never renamed or retyped: 0.6.1 reads the same file, and `save()`
keeps every key it doesn't know, so a downgrade keeps working.  A changed
meaning gets a new key and the old one is left alone (`advanced` →
`show_advanced`).

`watch(key, cb)` calls `cb(value)` after a save changes that key.  The app
installs a dispatcher (`GLib.idle_add`) so a save from a worker thread still
calls watchers on the main loop; this module stays free of GTK.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import XDG_CONFIG
from .system import atomic_write

PREFS_PATH = XDG_CONFIG / 'pipewire-controller' / 'ui.json'
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Pref:
    key: str
    default: Any
    store: str = 'pref'          # 'pref' | 'state' | 'data'
    # For a row the Preferences window builds by itself.  A key with no
    # title has a hand-built row, or none (state and data).
    title: str = ''
    subtitle: str = ''
    choices: tuple = field(default_factory=tuple)    # ((value, label), ...)


PREFS: list[Pref] = [
    # -- choices (Preferences) --------------------------------------------
    Pref('volume_style', 'classic'),     # classic | stepped | precision | meter
    # Replaces `advanced` (2026-10-28): advanced settings are shown unless
    # the user hides them.  `advanced` stays below, untouched, for 0.6.1.
    Pref('show_advanced', True,
         title='Show advanced settings',
         subtitle='Buffering, timing and expert options on the settings '
                  'pages. Turn it off for a simpler view.'),
    Pref('color_scheme', 'system',
         title='Style',
         choices=(('system', 'Follow system'), ('light', 'Light'),
                  ('dark', 'Dark'))),
    Pref('start_page', 'last',
         title='Open at start',
         choices=(('last', 'The page you were on'),
                  ('dashboard', 'Dashboard'))),
    Pref('level_meters', True,
         title='Live level meters',
         subtitle='The moving level bars. Each one is a small capture '
                  'process, so turning them off saves some CPU.'),
    Pref('start_minimized', False),
    Pref('keep_running', False),
    Pref('autoload_presets', False),     # apply a device preset on a switch
    Pref('notify_links', False),         # desktop notification on broken links
    Pref('notify_services', True),       # … on failed audio services
    Pref('notify_xruns', False),         # … on new xruns (Monitor page polling)
    Pref('monitor_poll', 1),             # seconds between Monitor samples
    Pref('eq_match_level', True),        # Equalizer: Compare at one loudness

    # -- where the user was ------------------------------------------------
    Pref('advanced', False, 'state'),    # 0.6.1's switch; read by 0.6.1 only
    Pref('surround_layout', '5.1', 'state'),
    Pref('last_page', 'dashboard', 'state'),
    # Whether the sidebar is shown, while the window is wide enough to have
    # it beside the page.  A narrow window always opens on its page.
    Pref('sidebar_shown', True, 'state'),
    Pref('dashboard_tab', 'overview', 'state'),
    Pref('win_width', 1080, 'state'),
    Pref('win_height', 760, 'state'),
    Pref('win_maximized', False, 'state'),
    # The Patchbay's Show menu: {'midi': bool, 'monitors': bool,
    # 'video': bool}.  Empty means MIDI on, unused monitors and video off.
    Pref('graph_view', {}, 'state'),
    # Devices page, what is folded: {'sections': ['outputs'|'inputs'],
    # 'cards': ['out:<node.name>'|'in:<node.name>']}.  Names, as Favourites
    # are, so a fold survives a reboot and the device coming back.
    Pref('devices_folded', {}, 'state'),
    # Search (ui/search.py): the keys of the results last opened, newest
    # first, shown before anything is typed.  Keys, not titles, so one a
    # later run no longer has is just left out.
    Pref('search_recent', [], 'state'),

    # -- what the user built -------------------------------------------------
    Pref('device_presets', {}, 'data'),  # node.name -> preset (backend/presets)
    # Dashboard → Overview → Favourites, as a list of `node.name`.  Names, not
    # ids or serials: an id is recycled and a serial changes every time a node
    # is recreated, so only the name survives a reboot or a chain restart —
    # the same reasoning routing snapshots are built on.
    Pref('favorite_devices', [], 'data'),
    # node.name -> the description it had when last seen, so a device that is
    # unplugged still reads as "Scarlett Solo" rather than as its ALSA node
    # name.  Learned, never authoritative: the live description always wins.
    Pref('favorite_labels', {}, 'data'),
    Pref('graph_positions', {}, 'data'),     # node.name -> [x, y]
]

BY_KEY = {p.key: p for p in PREFS}
DEFAULTS = {p.key: p.default for p in PREFS}

# -- cache --------------------------------------------------------------------
#
# Every get() used to re-read and parse the file.  The cache is keyed by the
# path and the file's (mtime, size), so an edit made outside the app, or a
# harness pointing PREFS_PATH somewhere else, is still seen.

_cache: dict = {'key': None, 'data': None}


def _stamp():
    try:
        st = os.stat(PREFS_PATH)
        return (str(PREFS_PATH), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(PREFS_PATH), None, None)


def _read() -> dict:
    stamp = _stamp()
    if _cache['key'] == stamp and _cache['data'] is not None:
        return _cache['data']
    try:
        data = json.loads(PREFS_PATH.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    _cache['key'], _cache['data'] = stamp, data
    return data


def load() -> dict:
    # A copy: callers may change what they get back.
    return json.loads(json.dumps({**DEFAULTS, **_read()}))


def get(key: str):
    data = _read()
    value = data.get(key, DEFAULTS.get(key))
    if isinstance(value, (dict, list)):
        value = json.loads(json.dumps(value))
    return value


# -- change notifications ---------------------------------------------------

_watchers: dict[int, tuple[str, Callable]] = {}
_next_token = [1]
_dispatch: list[Callable | None] = [None]


def set_dispatcher(fn: Callable | None):
    """How to reach the main loop from a worker thread (GLib.idle_add)."""
    _dispatch[0] = fn


def watch(key: str, callback: Callable) -> int:
    """Call `callback(value)` whenever a save changes `key`."""
    with _LOCK:
        token = _next_token[0]
        _next_token[0] += 1
        _watchers[token] = (key, callback)
    return token


def unwatch(token: int | None):
    if token is not None:
        with _LOCK:
            _watchers.pop(token, None)


def _notify(changed: dict):
    if not changed:
        return
    with _LOCK:
        calls = [(cb, changed[key]) for key, cb in _watchers.values()
                 if key in changed]
    if not calls:
        return

    def run():
        for cb, value in calls:
            try:
                cb(value)
            except Exception:          # noqa: BLE001 — one bad page must
                import traceback       # not stop the others hearing it
                traceback.print_exc()
        return False
    if (_dispatch[0] is not None
            and threading.current_thread() is not threading.main_thread()):
        _dispatch[0](run)
    else:
        run()


def save(**updates):
    with _LOCK:
        prefs = {**DEFAULTS, **_read()}
        before = {k: prefs.get(k) for k in updates}
        prefs.update(updates)
        atomic_write(PREFS_PATH, json.dumps(prefs, indent=2) + '\n')
        _cache['key'] = None
    _notify({k: v for k, v in updates.items() if before.get(k) != v})
    return prefs


def reset_preferences() -> list[str]:
    """Put every `pref` key back to its default; state and data stay.

    Returns the keys that changed.
    """
    defaults = {p.key: p.default for p in PREFS if p.store == 'pref'}
    with _LOCK:
        prefs = {**DEFAULTS, **_read()}
        changed = {k: v for k, v in defaults.items() if prefs.get(k) != v}
        prefs.update(defaults)
        atomic_write(PREFS_PATH, json.dumps(prefs, indent=2) + '\n')
        _cache['key'] = None
    _notify(changed)
    return sorted(changed)
