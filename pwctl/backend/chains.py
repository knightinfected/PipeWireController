"""Filter-chain manager.

Each chain is stored as JSON metadata plus a generated standalone PipeWire
config, and runs as its own systemd user unit (pwctl-chain@<id>.service →
`pipewire -c <id>.conf`).  Swapping an HRIR regenerates the config and
restarts only that unit — the main audio graph is never interrupted.

Two kinds of chains:
  * template — fully regenerated from ChainMeta parameters
  * raw      — imported .conf kept verbatim; HRIR swap rewrites the
               convolver/spatializer `filename` fields via the SPA parser
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .. import spa_json
from . import system, templates
from .config import XDG_CONFIG
from .hrir import analyze

APP_DIR = XDG_CONFIG / 'pipewire-controller'
META_DIR = APP_DIR / 'chains'
GEN_DIR = APP_DIR / 'generated'
UNIT_NAME = 'pwctl-chain@.service'
UNIT_PATH = Path.home() / '.config/systemd/user' / UNIT_NAME

UNIT_TEXT = f"""# Managed by PipeWire Controller — runs one filter chain per instance.
[Unit]
Description=PipeWire Controller filter chain: %i
After=pipewire.service
BindsTo=pipewire.service

[Service]
Type=simple
Environment=PIPEWIRE_CONFIG_DIR=%h/.config/pipewire-controller/generated
ExecStart=/usr/bin/pipewire -c %i.conf
Restart=on-failure
RestartSec=2

[Install]
WantedBy=pipewire.service
"""


@dataclass
class ChainMeta:
    id: str
    name: str
    template: str = 'virtual-surround-7.1'   # or 'raw'
    description: str = ''
    hrir: str = ''                            # absolute path to IR/HRIR/EQ file
    hrir_channels: int = 0
    target: str = ''                          # output sink / input source name
    enabled: bool = False
    params: dict = field(default_factory=dict)

    @property
    def is_raw(self) -> bool:
        return self.template == 'raw'

    @property
    def unit(self) -> str:
        return f'pwctl-chain@{self.id}.service'

    @property
    def conf_path(self) -> Path:
        return GEN_DIR / f'{self.id}.conf'

    @property
    def meta_path(self) -> Path:
        return META_DIR / f'{self.id}.json'


def _slug(name: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'chain'
    return s[:40]


def ensure_dirs():
    META_DIR.mkdir(parents=True, exist_ok=True)
    GEN_DIR.mkdir(parents=True, exist_ok=True)


def ensure_unit() -> bool:
    """Install/refresh the systemd template unit. True if it (re)wrote it."""
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if UNIT_PATH.is_file() and UNIT_PATH.read_text() == UNIT_TEXT:
        return False
    UNIT_PATH.write_text(UNIT_TEXT)
    system.daemon_reload()
    return True


def list_chains() -> list[ChainMeta]:
    ensure_dirs()
    out = []
    for f in sorted(META_DIR.glob('*.json')):
        try:
            data = json.loads(f.read_text())
            known = {k for k in ChainMeta.__dataclass_fields__}
            out.append(ChainMeta(**{k: v for k, v in data.items() if k in known}))
        except (ValueError, TypeError):
            continue
    return out


def save_meta(meta: ChainMeta):
    ensure_dirs()
    meta.meta_path.write_text(json.dumps(asdict(meta), indent=2))


def new_chain(name: str, template: str, **kw) -> ChainMeta:
    base = _slug(name)
    cid = base
    existing = {m.id for m in list_chains()}
    while cid in existing:
        cid = f'{base}-{uuid.uuid4().hex[:4]}'
    meta = ChainMeta(id=cid, name=name, template=template, **kw)
    if meta.hrir:
        info = analyze(meta.hrir)
        meta.hrir_channels = info.channels
    return meta


# ------------------------------------------------------------- generation --

def _rewrite_filenames(conf_text: str, new_path: str) -> str:
    """Swap every convolver/spatializer filename in a raw conf (regex-based,
    so the user's own comments and formatting are preserved)."""
    pat = re.compile(r'(filename\s*=\s*)"(?![/]hilbert|[/]dirac|[/]ir:)[^"]*"')
    return pat.sub(lambda m: f'{m.group(1)}"{new_path}"', conf_text)


def generate(meta: ChainMeta):
    """(Re)write the standalone conf for this chain."""
    ensure_dirs()
    if meta.is_raw:
        raw = meta.params.get('raw_text', '')
        if meta.hrir:
            raw = _rewrite_filenames(raw, meta.hrir)
            meta.params['raw_text'] = raw
        text = _wrap_raw(raw, meta)
    else:
        text = templates.render(meta)
    spa_json.loads(text)          # sanity check before writing
    meta.conf_path.write_text(text)
    save_meta(meta)


def _wrap_raw(raw: str, meta: ChainMeta) -> str:
    """Imported drop-ins only contain context.modules; a standalone process
    also needs the base modules. Merge them if missing."""
    data = spa_json.loads(raw)
    mods = data.get('context.modules')
    if not isinstance(mods, list):
        raise spa_json.SpaJsonError('no context.modules section found')
    have = {m.get('name') for m in mods if isinstance(m, dict)}
    base = templates._base_conf({})['context.modules'][:-1]
    prepend = [m for m in base if m['name'] not in have]
    if not prepend and 'context.spa-libs' in data:
        return raw           # already standalone; keep untouched
    data['context.modules'] = prepend + mods
    data.setdefault('context.spa-libs', {
        'audio.convert.*': 'audioconvert/libspa-audioconvert',
        'support.*': 'support/libspa-support'})
    data.setdefault('context.properties', {'log.level': 2})
    return spa_json.dumps(
        data, header=f'{meta.name}\nImported into PipeWire Controller.')


# -------------------------------------------------------------- lifecycle --

def apply(meta: ChainMeta) -> tuple[bool, str]:
    """Regenerate config and (re)start/stop the unit to match `enabled`."""
    try:
        generate(meta)
    except spa_json.SpaJsonError as e:
        return False, f'Invalid config: {e}'
    ensure_unit()
    if meta.enabled:
        rc, _, err = system.sysctl_user('enable', '--now', meta.unit)
        if rc != 0:
            return False, err.strip() or 'failed to start unit'
        rc, _, err = system.sysctl_user('restart', meta.unit, timeout=30)
        return (rc == 0), (err.strip() if rc else '')
    rc, _, err = system.sysctl_user('disable', '--now', meta.unit)
    return True, ''


def set_enabled(meta: ChainMeta, enabled: bool) -> tuple[bool, str]:
    meta.enabled = enabled
    save_meta(meta)
    if enabled and not meta.conf_path.is_file():
        return apply(meta)
    ensure_unit()
    if enabled:
        rc, _, err = system.sysctl_user('enable', '--now', meta.unit)
        return (rc == 0), (err.strip() if rc else '')
    system.sysctl_user('disable', '--now', meta.unit)
    return True, ''


def restart(meta: ChainMeta) -> tuple[bool, str]:
    rc, _, err = system.sysctl_user('restart', meta.unit, timeout=30)
    return (rc == 0), (err.strip() if rc else '')


def status(meta: ChainMeta) -> str:
    return system.unit_state(meta.unit)


def delete(meta: ChainMeta):
    system.sysctl_user('disable', '--now', meta.unit)
    for p in (meta.conf_path, meta.meta_path):
        if p.is_file():
            p.unlink()


def clone(meta: ChainMeta) -> ChainMeta:
    dup = new_chain(f'{meta.name} copy', meta.template,
                    description=meta.description, hrir=meta.hrir,
                    target=meta.target, params=dict(meta.params))
    dup.hrir_channels = meta.hrir_channels
    dup.enabled = False
    save_meta(dup)
    return dup


# ----------------------------------------------------------------- import --

def sniff_conf(path) -> dict:
    """Inspect an existing filter-chain .conf: name + IR files it references."""
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    info = {'text': text, 'name': Path(path).stem, 'irs': [], 'valid': False}
    try:
        data = spa_json.loads(text)
    except spa_json.SpaJsonError:
        return info
    mods = data.get('context.modules')
    if not isinstance(mods, list):
        return info
    for mod in mods:
        if not isinstance(mod, dict):
            continue
        if mod.get('name') != 'libpipewire-module-filter-chain':
            continue
        info['valid'] = True
        args = mod.get('args') or {}
        if args.get('node.description'):
            info['name'] = str(args['node.description'])
        graph = args.get('filter.graph') or {}
        for node in graph.get('nodes') or []:
            cfg = node.get('config') if isinstance(node, dict) else None
            fn = cfg.get('filename') if isinstance(cfg, dict) else None
            if isinstance(fn, str) and not fn.startswith('/hilbert') \
                    and not fn.startswith('/dirac') and not fn.startswith('/ir:'):
                if fn not in info['irs']:
                    info['irs'].append(fn)
    return info


def import_conf(path) -> ChainMeta | None:
    info = sniff_conf(path)
    if not info['valid']:
        return None
    meta = new_chain(info['name'], 'raw',
                     description=f'Imported from {path}',
                     params={'raw_text': info['text']})
    if info['irs']:
        meta.hrir = info['irs'][0]
        meta.hrir_channels = analyze(meta.hrir).channels
    generate(meta)
    return meta


def scan_importable() -> list[Path]:
    """Find existing user filter-chain drop-ins that could be imported."""
    found = []
    for d in (XDG_CONFIG / 'pipewire/filter-chain.conf.d',
              XDG_CONFIG / 'pipewire/filter-chain.conf.d/inactive',
              Path('/etc/pipewire/filter-chain.conf.d')):
        if d.is_dir():
            found += sorted(p for p in d.glob('*.conf') if p.is_file())
    return found
