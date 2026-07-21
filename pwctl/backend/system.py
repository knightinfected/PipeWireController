"""Subprocess and systemd helpers."""

from __future__ import annotations

import shutil
import subprocess

APP_DIR_NAME = 'pipewire-controller'


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
    """active | inactive | failed | activating | unknown..."""
    rc, out, _ = sysctl_user('is-active', unit)
    state = out.strip() or 'unknown'
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
