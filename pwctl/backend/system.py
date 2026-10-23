"""Subprocess and systemd helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

APP_DIR_NAME = 'pipewire-controller'


def atomic_write(path, text: str):
    """Write a file via temp + rename so a crash never leaves it truncated."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(argv, timeout=10, input_text=None):
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            input=input_text)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, '', f'{argv[0]}: not found'
    except subprocess.TimeoutExpired:
        return 124, '', f'{argv[0]}: timed out'


def have(cmd) -> bool:
    return shutil.which(cmd) is not None


# ---------------------------------------------------------------- systemd ---

def sysctl_user(*args, timeout=15):
    return run(['systemctl', '--user', *args], timeout=timeout)


def unit_state(unit: str) -> str:
    """active | inactive | failed | activating | unknown...

    Asked over D-Bus when systemd is on the session bus, which gives the same
    answer as `systemctl is-active` without starting it (see units.py);
    `systemctl` when it is not.
    """
    from . import units
    state = units.active_state(unit)
    if state is not None:
        return state
    rc, out, _ = sysctl_user('is-active', unit)
    state = out.strip() or 'unknown'
    return state


# What `service_state()` says for a stopped service whose socket is
# listening: the first client will start it, so nothing is wrong.
ON_DEMAND = 'on demand'
ON_DEMAND_TIP = ('Not started yet: it starts by itself when the first app '
                 'that uses it connects. Nothing is wrong.')


def service_state(unit: str) -> str:
    """`unit_state()`, except a socket-activated service that is only idle.

    pipewire-pulse is started by `pipewire-pulse.socket` when the first
    PulseAudio app connects, so after a login it is `inactive` while its
    socket listens, which is healthy.  Then this says ON_DEMAND.  The sockets
    are asked for, never assumed: the service's `TriggeredBy`, and each
    socket's own state (measured on a transient socket-activated pair,
    docs.local/probes/2026-10-23-pulse-socket/).
    """
    state = unit_state(unit)
    if state != 'inactive':
        return state
    from . import units
    socks = units.triggered_by(unit)
    if socks is None:
        _rc, out, _ = sysctl_user('show', '-p', 'TriggeredBy', '--value', unit)
        socks = out.split()
    if any(unit_state(s) == 'active' for s in socks if s.endswith('.socket')):
        return ON_DEMAND
    return state


def unit_enabled(unit: str) -> bool:
    rc, out, _ = sysctl_user('is-enabled', unit)
    return out.strip() in ('enabled', 'enabled-runtime', 'linked', 'static')


def daemon_reload():
    return sysctl_user('daemon-reload')


def restart_unit(unit: str):
    return sysctl_user('restart', unit, timeout=30)


def restart_pipewire():
    """Restart the whole PipeWire stack cleanly."""
    return sysctl_user('restart', 'pipewire.service', 'pipewire-pulse.service',
                       'wireplumber.service', timeout=30)


def restart_wireplumber():
    return sysctl_user('restart', 'wireplumber.service', timeout=30)


def unit_journal(unit: str, lines=40) -> str:
    rc, out, err = run(['journalctl', '--user', '-u', unit, '-n', str(lines),
                        '--no-pager', '-o', 'cat'], timeout=10)
    return out or err
