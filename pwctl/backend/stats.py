"""Performance monitoring: pw-top samples, service CPU/RAM, journal tail.

Everything is poll-based (called from the UI through async_call); CPU usage
is computed by the caller from two consecutive ProcSample readings.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .system import run, sysctl_user

MONITOR_UNITS = ['pipewire.service', 'wireplumber.service',
                 'pipewire-pulse.service']


# ------------------------------------------------------------------ pw-top --

@dataclass
class TopRow:
    state: str          # S(uspended) R(unning) C(reated/idle) ...
    id: int
    quantum: int
    rate: int
    wait: str
    busy: str
    w_q: str            # fraction of the quantum spent waiting
    b_q: str            # fraction of the quantum spent busy (DSP load)
    errors: int         # xrun counter since the node started
    fmt: str
    name: str
    is_driver: bool = False


_TOP_RE = re.compile(
    r'^\s*(\S)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)'
    r'\s+(\d+)')


def top_sample() -> list[TopRow]:
    """One pw-top batch sample. Uses two iterations so timings are real."""
    rc, out, _ = run(['pw-top', '-b', '-n', '2'], timeout=10)
    if rc != 0:
        return []
    rows: list[TopRow] = []
    name_col = None
    for line in out.splitlines():
        if 'NAME' in line and line.lstrip().startswith('S'):
            rows = []                      # keep only the last block
            name_col = line.index('NAME')
            continue
        m = _TOP_RE.match(line)
        if not m or name_col is None:
            continue
        raw_name = line[name_col:] if len(line) > name_col else ''
        fmt = line[m.end():name_col].strip() if len(line) > name_col \
            else line[m.end():].strip()
        # follower rows are shown indented with "+ " under their driver
        is_driver = not raw_name.startswith(('+', ' +'))
        rows.append(TopRow(
            state=m.group(1), id=int(m.group(2)), quantum=int(m.group(3)),
            rate=int(m.group(4)), wait=m.group(5), busy=m.group(6),
            w_q=m.group(7), b_q=m.group(8), errors=int(m.group(9)),
            fmt=fmt, name=raw_name.strip().lstrip('+ '), is_driver=is_driver))
    return rows


def xrun_total(rows: list[TopRow]) -> int:
    return sum(r.errors for r in rows)


def dsp_load(rows: list[TopRow]) -> float:
    """Highest busy/quantum fraction among running nodes (0..1+)."""
    load = 0.0
    for r in rows:
        try:
            load = max(load, float(r.b_q))
        except ValueError:
            continue
    return load


# ------------------------------------------------------- process CPU / RAM --

@dataclass
class ProcSample:
    pid: int
    jiffies: int = 0        # utime+stime at sample time
    total_jiffies: int = 0  # whole-system jiffies at sample time
    rss_bytes: int = 0
    ok: bool = False


def service_pid(unit: str) -> int:
    rc, out, _ = sysctl_user('show', '-p', 'MainPID', '--value', unit)
    try:
        return int(out.strip())
    except ValueError:
        return 0


def _total_jiffies() -> int:
    try:
        with open('/proc/stat') as f:
            parts = f.readline().split()
        return sum(int(x) for x in parts[1:])
    except (OSError, ValueError):
        return 0


def proc_sample(pid: int) -> ProcSample:
    s = ProcSample(pid=pid)
    if pid <= 0:
        return s
    try:
        with open(f'/proc/{pid}/stat') as f:
            stat = f.read()
        # utime and stime are fields 14/15, counted after the (comm) field
        after = stat.rsplit(')', 1)[1].split()
        s.jiffies = int(after[11]) + int(after[12])
        with open(f'/proc/{pid}/statm') as f:
            s.rss_bytes = int(f.read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        s.total_jiffies = _total_jiffies()
        s.ok = True
    except (OSError, IndexError, ValueError):
        pass
    return s


def cpu_percent(prev: ProcSample, cur: ProcSample) -> float:
    """CPU use of the process between two samples, in percent of one core."""
    if not (prev.ok and cur.ok) or prev.pid != cur.pid:
        return 0.0
    dt = cur.total_jiffies - prev.total_jiffies
    if dt <= 0:
        return 0.0
    ncpu = os.cpu_count() or 1
    return max(0.0, (cur.jiffies - prev.jiffies) / dt * 100.0 * ncpu)


@dataclass
class HealthSnapshot:
    states: dict = field(default_factory=dict)      # unit -> active/failed/...
    procs: dict = field(default_factory=dict)       # unit -> ProcSample
    top: list = field(default_factory=list)         # TopRow list
    xruns: int = 0
    load: float = 0.0


def health_snapshot() -> HealthSnapshot:
    from .system import unit_state
    snap = HealthSnapshot()
    for unit in MONITOR_UNITS:
        snap.states[unit] = unit_state(unit)
        snap.procs[unit] = proc_sample(service_pid(unit))
    snap.top = top_sample()
    snap.xruns = xrun_total(snap.top)
    snap.load = dsp_load(snap.top)
    return snap


# ------------------------------------------------------------------- logs --

def journal_tail(units=None, lines=200, cursor: str | None = None,
                 priority: str | None = None) -> tuple[str, str | None]:
    """Last log lines for the PipeWire stack.

    Returns (text, cursor); pass the cursor back to get only newer lines,
    which makes cheap follow-style polling possible.
    """
    cmd = ['journalctl', '--user', '--no-pager', '--show-cursor',
           '-o', 'short-precise']
    for u in (units or MONITOR_UNITS):
        cmd += ['-u', u]
    if priority:
        cmd += ['-p', priority]
    if cursor:
        cmd += ['--after-cursor', cursor]
    else:
        cmd += ['-n', str(lines)]
    rc, out, err = run(cmd, timeout=10)
    if rc != 0:
        return err.strip(), cursor
    new_cursor = cursor
    body = []
    for line in out.splitlines():
        if line.startswith('-- cursor:'):
            new_cursor = line.split('cursor:', 1)[1].strip()
        elif not line.startswith('-- No entries --'):
            body.append(line)
    return '\n'.join(body), new_cursor
