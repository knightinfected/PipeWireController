"""Start at login: an XDG autostart entry, and whether this desktop runs one.

The entry (`~/.config/autostart/<app-id>.desktop`) *is* the setting: there is
no key for it in ui.json, so the switch can never disagree with what the
desktop will do.  It is written only from Preferences, never by a package, so
an upgrade never starts launching anything at login.  `TryExec=` makes a
leftover entry harmless after an uninstall.

Not every session runs these entries.  GNOME, KDE and the other full desktops
do; Hyprland, Sway and most other compositors don't, unless they were started
through uwsm (systemd's autostart generator) or something like `dex -a` runs
them.  `session_support()` asks the session first and only then falls back on
naming the desktop, and says which of the two it did.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import XDG_CONFIG
from .system import atomic_write

APP_ID = 'io.github.knightinfected.PipeWireControlCenter'
ENTRY = XDG_CONFIG / 'autostart' / f'{APP_ID}.desktop'
BACKGROUND_FLAG = '--background'

# Desktops whose session starts XDG autostart entries itself (the
# freedesktop Desktop Application Autostart spec).  Read from their docs and
# the spec, not measured here.
SPEC_DESKTOPS = ('GNOME', 'KDE', 'XFCE', 'MATE', 'X-Cinnamon', 'Cinnamon',
                 'LXQt', 'LXDE', 'Budgie', 'Pantheon', 'Deepin', 'Unity')

# Compositors that don't, with the line that starts a program from their
# own config.  `{cmd}` is the command, already quoted for that config.
COMPOSITOR_LINES = {
    'Hyprland': ('Hyprland', '~/.config/hypr/hyprland.conf',
                 'exec-once = {cmd}'),
    'sway': ('Sway', '~/.config/sway/config', 'exec {cmd}'),
    'niri': ('niri', '~/.config/niri/config.kdl',
             'spawn-at-startup {args}'),
    'river': ('river', '~/.config/river/init', 'riverctl spawn "{cmd}"'),
    'labwc': ('labwc', '~/.config/labwc/autostart', '{cmd} &'),
}

YES, NO, MAYBE, UNKNOWN = 'yes', 'no', 'maybe', 'unknown'


# -- the command ---------------------------------------------------------------

def launcher() -> str:
    """The absolute path that starts this app, the way it was installed.

    A package puts `pipewire-control-center` on PATH; a git checkout has the
    launcher next to `pwctl/`.  An absolute path either way, because a
    session starting login items may not see the terminal's PATH (the same
    reason README writes the full path into the menu entry, issue #22).
    """
    found = shutil.which('pipewire-control-center')
    if found:
        return os.path.abspath(found)
    here = Path(__file__).resolve().parent.parent.parent
    local = here / 'pipewire-control-center'
    if local.is_file():
        return str(local)
    return 'pipewire-control-center'


def _quote_exec(arg: str) -> str:
    """One argument for a desktop entry's Exec= (Desktop Entry spec)."""
    reserved = set(' \t\n"\'\\><~|&;$*?#()`')
    if not arg or any(c in reserved for c in arg):
        # The spec unescapes a value twice (as a string, then as a quoted
        # argument), so a character escaped inside quotes takes two
        # backslashes in the file.
        inner = ''.join('\\\\' + c if c in '"`$\\' else c for c in arg)
        return f'"{inner}"'
    return arg


def command(minimized: bool) -> list[str]:
    argv = [launcher()]
    if minimized:
        argv.append(BACKGROUND_FLAG)
    return argv


# -- the entry -------------------------------------------------------------

def entry_text(minimized: bool) -> str:
    argv = command(minimized)
    exec_line = ' '.join(_quote_exec(a) for a in argv)
    return '\n'.join([
        '[Desktop Entry]',
        'Type=Application',
        'Name=PipeWire Controller',
        'Comment=Starts PipeWire Controller when you log in '
        '(turn it off in its Preferences)',
        f'Exec={exec_line}',
        f'TryExec={argv[0]}',
        'Icon=audio-card',
        'Terminal=false',
        'NoDisplay=true',
        'X-GNOME-Autostart-enabled=true',
        '',
    ])


def is_enabled() -> bool:
    return ENTRY.is_file()


def is_minimized() -> bool:
    """Whether the entry on disk starts the app without a window."""
    try:
        text = ENTRY.read_text()
    except OSError:
        return False
    for line in text.splitlines():
        if line.startswith('Exec='):
            return BACKGROUND_FLAG in line.split()
    return False


def enable(minimized: bool):
    atomic_write(ENTRY, entry_text(minimized))


def disable():
    try:
        ENTRY.unlink()
    except FileNotFoundError:
        pass


# -- does this session run it? ---------------------------------------------

def _desktops() -> list[str]:
    raw = os.environ.get('XDG_CURRENT_DESKTOP', '') or \
        os.environ.get('XDG_SESSION_DESKTOP', '')
    return [d for d in raw.split(':') if d]


def _target_active() -> bool | None:
    """systemd's autostart target: started by uwsm, KDE's and other
    systemd-managed sessions.  None when systemd can't be asked."""
    from . import units
    state = units.active_state('xdg-desktop-autostart.target')
    if state is None:
        from .system import sysctl_user, have
        if not have('systemctl'):
            return None
        _rc, out, _ = sysctl_user('is-active', 'xdg-desktop-autostart.target')
        state = out.strip() or None
    if state is None:
        return None
    return state in ('active', 'activating')


def compositor_line(minimized: bool = True) -> tuple[str, str, str] | None:
    """(compositor name, its config file, the line to add), or None."""
    for d in _desktops():
        for key, (name, path, form) in COMPOSITOR_LINES.items():
            if d.lower() == key.lower():
                argv = command(minimized)
                cmd = ' '.join(_shell_quote(a) for a in argv)
                args = ' '.join(f'"{a}"' for a in argv)
                return name, path, form.format(cmd=cmd, args=args)
    return None


def _shell_quote(arg: str) -> str:
    import shlex
    return shlex.quote(arg)


def session_support() -> tuple[str, str]:
    """(answer, why) — whether login items will start in this session.

    `yes` was asked of the session; `maybe` is a desktop known to implement
    the autostart spec, not asked; `no` is a compositor known not to; and
    `unknown` is anything else.
    """
    desktops = _desktops()
    shown = ':'.join(desktops) or 'this desktop'
    if _target_active():
        return YES, ('This session starts login apps through systemd '
                     '(xdg-desktop-autostart.target is active).')
    if any(d in SPEC_DESKTOPS for d in desktops):
        return MAYBE, f'{shown} starts login apps itself.'
    line = compositor_line()
    if line is not None:
        name = line[0]
        return NO, (f'{name} doesn’t start login apps by itself, unless it '
                    'was started with uwsm or dex runs them.')
    return UNKNOWN, (f'Can’t tell whether {shown} starts login apps. Most '
                     'desktops do.')
