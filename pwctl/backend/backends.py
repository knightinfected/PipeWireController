"""Which SPA backends this machine can actually load.

The house rule this module exists to enforce: **probe, never infer.**  Do not
read a PipeWire version, do not assume a sibling file implies its neighbours,
and do not treat "the plugin is on disk" as "PipeWire can host it".  Ask the
filesystem for the exact object that will be dlopened, the same way
`pw.smart_filters_supported()` asks for a capability instead of parsing a
version string.

Why it exists
-------------
PipeWire >= 1.4 splits filter-graph support into one shared object per plugin
type under ``spa-0.2/filter-graph/``, and echo-cancel into one per canceller
under ``spa-0.2/aec/``.  Arch's ``pipewire-audio`` ships all of them in the
base package; Fedora splits ``-lv2``, ``-sofa`` and ``-onnx`` into subpackages
a stock install does not pull in.  A conf naming a backend that is not
installed does not degrade — PipeWire refuses the whole config and the process
dies with ``exit 254`` in tens of milliseconds, which systemd reports only as
"control process exited with error code" (issue #15).

Because detection and playback go through different mechanisms, nothing ever
checked that they agreed.  `required_backends()` closes that by reading the
*generated conf* rather than trusting any one UI page: every surface that can
emit a node — Signal Paths, the Effects rack, the SOFA templates, rnnoise,
microphone cleanup — is covered by construction, including surfaces added
later.

Everything here is filesystem-only: no subprocess, no PipeWire connection, so
it is safe to call from a UI thread and cheap enough to call per dialog.
"""

from __future__ import annotations

import os
from pathlib import Path

# /usr/lib64 is a real tree on Fedora/openSUSE/RHEL and a symlink on Arch.
SPA_DIRS = [Path('/usr/lib/spa-0.2'), Path('/usr/lib64/spa-0.2'),
            Path('/usr/local/lib/spa-0.2'), Path('/usr/local/lib64/spa-0.2')]

FILTER_GRAPH = 'filter-graph'
AEC = 'aec'

# Node `type` values that are compiled into libspa-filter-graph.so itself and
# so can never be missing.  Everything else is a separate object.
_ALWAYS = {'builtin'}

# Fedora is the only distro seen splitting these so far; the hint is advice in
# a message, never a branch — an unknown distro just gets the generic line.
_PACKAGE_HINTS = {
    'lv2': 'pipewire-module-filter-chain-lv2',
    'sofa': 'pipewire-module-filter-chain-sofa',
    'onnx': 'pipewire-module-filter-chain-onnx',
}


def _roots() -> list[Path]:
    """Where PipeWire will look for SPA plugins, right now.

    ``$SPA_PLUGIN_DIR`` wins when set, because PipeWire honours it — probing a
    different tree than the chain will load from would be worse than not
    probing.  **Read at call time, never cached at import**: a cached answer is
    an inferred answer, and packages get installed while the app is open.
    """
    env = os.environ.get('SPA_PLUGIN_DIR')
    return [Path(p) for p in env.split(':') if p] if env else list(SPA_DIRS)


def _spa_dirs() -> list[Path]:
    """Existing SPA roots, de-duplicated by resolved real path."""
    seen, out = set(), []
    for d in _roots():
        try:
            if not d.is_dir():
                continue
            real = d.resolve()
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        out.append(d)
    return out


def probe(subdir: str) -> set[str] | None:
    """The backend names present in one SPA subdirectory.

    Returns ``None`` when no SPA root has that subdirectory at all, which is
    **not** the same as "none installed": PipeWire 1.2 has no `filter-graph`
    directory because nothing was split out yet, and on that machine every
    type is built in and works.  Callers must read ``None`` as "cannot tell,
    assume fine" or they will break every machine on an older PipeWire.
    """
    found, saw_dir = set(), False
    for root in _spa_dirs():
        d = root / subdir
        if not d.is_dir():
            continue
        saw_dir = True
        for so in d.glob('libspa-*.so'):
            stem = so.stem                      # libspa-filter-graph-plugin-lv2
            for prefix in (f'libspa-{subdir}-plugin-', f'libspa-{subdir}-'):
                if stem.startswith(prefix):
                    found.add(stem[len(prefix):])
                    break
    return found if saw_dir else None


def available(kind: str, subdir: str = FILTER_GRAPH) -> bool:
    """Whether a node `type` (or aec method) can be loaded on this machine."""
    if subdir == FILTER_GRAPH and kind in _ALWAYS:
        return True
    have = probe(subdir)
    if have is None:            # no split-out backends here at all — 1.2-era
        return True
    return kind in have


def package_hint(kind: str) -> str:
    """The package a missing backend usually lives in, or '' if unknown."""
    return _PACKAGE_HINTS.get(kind, '')


# ------------------------------------------------------- reading a conf ----

def _walk_nodes(obj, out: set):
    """Every filter.graph node `type` anywhere in a parsed conf."""
    if isinstance(obj, dict):
        graph = obj.get('filter.graph')
        if isinstance(graph, dict):
            for node in graph.get('nodes') or []:
                if isinstance(node, dict) and node.get('type'):
                    out.add(str(node['type']))
        for value in obj.values():
            _walk_nodes(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _walk_nodes(value, out)


def _walk_aec(obj, out: set):
    """Every echo-cancel `library.name`, as its bare method name."""
    if isinstance(obj, dict):
        lib = obj.get('library.name')
        if isinstance(lib, str) and lib.startswith(f'{AEC}/'):
            stem = lib.rsplit('/', 1)[-1]
            out.add(stem[len(f'libspa-{AEC}-'):]
                    if stem.startswith(f'libspa-{AEC}-') else stem)
        for value in obj.values():
            _walk_aec(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _walk_aec(value, out)


def required_backends(conf) -> dict[str, set[str]]:
    """What a generated conf needs in order to load, by SPA subdirectory.

    `conf` is a parsed conf dict, or the text of one.  Reading the conf rather
    than the UI state is the whole point: a surface added next year is covered
    without anyone remembering to add it here.
    """
    if isinstance(conf, str):
        from pwctl import spa_json
        conf = spa_json.loads(conf)
    nodes: set[str] = set()
    aec: set[str] = set()
    _walk_nodes(conf, nodes)
    _walk_aec(conf, aec)
    return {FILTER_GRAPH: nodes - _ALWAYS, AEC: aec}


def missing_backends(conf) -> list[tuple[str, str]]:
    """`(subdir, name)` for every backend the conf needs and cannot get.

    Empty means the conf will at least get as far as starting — it says
    nothing about whether the audio is right.
    """
    missing = []
    for subdir, kinds in required_backends(conf).items():
        for kind in sorted(kinds):
            if not available(kind, subdir):
                missing.append((subdir, kind))
    return missing


def explain(conf) -> str:
    """A user-facing reason a conf cannot start, or '' if nothing is missing.

    Written to replace systemd's "control process exited with error code",
    which is what the user sees today and names neither the cause nor the cure.
    """
    missing = missing_backends(conf)
    if not missing:
        return ''
    parts = []
    for subdir, kind in missing:
        pkg = package_hint(kind)
        where = 'plugin support' if subdir == FILTER_GRAPH else 'echo-cancel'
        parts.append(f'{kind} {where}' + (f' (package: {pkg})' if pkg else ''))
    return ('PipeWire on this system cannot load: ' + ', '.join(parts) +
            '. The chain will not start until it is installed.')


# ------------------------------------------------------ using a plugin ----

def plugin_problem(plugin) -> str:
    """Why this plugin cannot go in a chain, or '' if it can.

    Two independent reasons, both of which used to reach the user as a broken
    chain instead of a greyed-out row (issue #15):

    * **PipeWire cannot load its type here.** Loud: the chain is refused and
      the unit dies with `exit 254` before it ever runs.
    * **The graph cannot wire its ports.** Silent, and worse: `_effect_lanes`
      instantiates per channel (1-in/1-out) or per pair (2-in/2-out) and
      binds `ins[:width]`/`outs[:width]`, so anything else comes up green with
      ports dangling and processes audio wrongly, with nothing to say so.

    The second is not distro-specific — it happens on a fully-equipped machine
    — which is how it stayed hidden behind the first.
    """
    kind = getattr(plugin, 'type', None) or (
        plugin.get('type') if isinstance(plugin, dict) else '')
    if kind and not available(kind):
        pkg = package_hint(kind)
        return (f'PipeWire here cannot load {kind.upper()} plugins'
                + (f' — install {pkg}' if pkg else
                   ' — the filter-graph backend is not installed'))
    from pwctl.backend import path_templates
    if isinstance(plugin, dict):
        from pwctl.backend.plugins import Plugin
        plugin = Plugin(type=kind, plugin=plugin.get('plugin', ''),
                        label=plugin.get('label', ''),
                        name=plugin.get('name', ''),
                        audio_in=list(plugin.get('audio_in') or []),
                        audio_out=list(plugin.get('audio_out') or []))
    if not plugin.ports_known:
        return ''          # unknown ports are allowed alone in a rack
    if not path_templates.usable(plugin):
        n_in, n_out = len(plugin.audio_in), len(plugin.audio_out)
        return (f'{n_in} in / {n_out} out — this app can only wire mono '
                '(1/1) or stereo (2/2) plugins')
    return ''


# --------------------------------------------------- resolving a plugin ----

def resolve_ladspa_path(filename: str) -> str:
    """An absolute LADSPA path that exists here, or `filename` unchanged.

    A bare name is left alone — filter-chain resolves those against the LADSPA
    path itself.  An absolute default written as `/usr/lib/ladspa/...` is
    re-resolved because that is the `/usr/lib64` trap again: on Fedora the same
    file is only ever in `/usr/lib64/ladspa/`.
    """
    if not filename or '/' not in filename:
        return filename
    p = Path(filename)
    if p.exists():
        return filename
    from pwctl.backend import plugins
    for d in plugins._dirs(plugins.LADSPA_DIRS):
        cand = d / p.name
        if cand.exists():
            return str(cand)
    return filename


__all__ = ['FILTER_GRAPH', 'AEC', 'SPA_DIRS', 'probe', 'available',
           'package_hint', 'required_backends', 'missing_backends', 'explain',
           'plugin_problem', 'resolve_ladspa_path']
