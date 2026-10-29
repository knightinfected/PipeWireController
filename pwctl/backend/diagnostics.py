"""Debug information for a bug report (Preferences → About).

Everything here is read from the system as it is now, the same probes the
app itself relies on, so a report says what this machine really has rather
than what a version number suggests.  Plain text, ready to paste.  Nothing
is sent anywhere: the user copies it.
"""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path

from .. import __version__
from . import autostart, backends
from .system import have, run

ROOT = Path(__file__).resolve().parent.parent.parent


def install_kind() -> str:
    """How this copy was installed, from what is on disk."""
    if (ROOT / '.git').exists():
        rc, branch, _ = run(['git', '-C', str(ROOT), 'rev-parse',
                             '--abbrev-ref', 'HEAD'])
        rc2, sha, _ = run(['git', '-C', str(ROOT), 'rev-parse', '--short',
                           'HEAD'])
        where = f'{branch.strip()} @ {sha.strip()}' if rc == rc2 == 0 \
            else 'unknown commit'
        return f'git checkout ({where}) at {ROOT}'
    if str(ROOT).startswith('/usr/'):
        if Path('/var/lib/dpkg/info/pipewire-control-center.list').exists():
            return f'Debian package (.deb) at {ROOT}'
        local = Path('/var/lib/pacman/local')
        if local.is_dir() and any(local.glob('pipewire-control-center-*')):
            return f'Arch package (AUR) at {ROOT}'
        return f'system package at {ROOT}'
    return f'copy at {ROOT}'


def os_release() -> str:
    for path in ('/etc/os-release', '/usr/lib/os-release'):
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        fields = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', text, re.M))
        name = fields.get('PRETTY_NAME') or fields.get('NAME') or '?'
        ids = fields.get('ID', '?')
        like = fields.get('ID_LIKE')
        return f'{name} (ID={ids}' + (f', ID_LIKE={like})' if like else ')')
    return 'unknown'


def _version(argv, pattern) -> str:
    if not have(argv[0]):
        return 'not installed'
    rc, out, err = run(argv)
    m = re.search(pattern, out + err)
    return m.group(1) if m else '?'


def collect(gtk: str = '?', adw: str = '?', gi_version: str = '?',
            notifications: str = '?') -> str:
    """The report.  GUI versions come from the caller (this module stays
    free of GTK); everything else is asked here."""
    lines = [
        f'PipeWire Controller {__version__}',
        f'Installed as: {install_kind()}',
        f'Python {platform.python_version()}, PyGObject {gi_version}, '
        f'GTK {gtk}, libadwaita {adw}',
        'PipeWire ' + _version(['pipewire', '--version'],
                               r'libpipewire\s+([\d.]+)'),
        'WirePlumber ' + _version(['wireplumber', '--version'],
                                  r'Linked with libwireplumber\s+([\d.]+)'),
        f'Distribution: {os_release()}',
        'Desktop: ' + (os.environ.get('XDG_CURRENT_DESKTOP') or '?')
        + ', ' + (os.environ.get('XDG_SESSION_TYPE') or '?') + ' session',
        f'Login apps: {autostart.session_support()[0]}',
        f'Notification service: {notifications}',
    ]
    tools = [t for t in ('pw-dump', 'pw-cli', 'pw-link', 'pw-metadata',
                         'pw-record', 'pw-top', 'wpctl', 'pactl') if not have(t)]
    lines.append('Missing tools: ' + (', '.join(tools) if tools else 'none'))
    for subdir, label in ((backends.FILTER_GRAPH, 'Filter backends'),
                          ('aec', 'Echo-cancel backends')):
        found = backends.probe(subdir)
        if found is None:
            text = 'not split out (PipeWire built in)'
        else:
            text = ', '.join(sorted(found)) or 'none'
        lines.append(f'{label}: {text}')
    return '\n'.join(lines) + '\n'
