"""Audio plugin discovery for effect inserts.

PipeWire's filter-chain hosts LADSPA and LV2 plugins natively — those are
fully supported here.  LADSPA libraries are introspected through the stable
C ABI (ctypes), which yields exact labels and audio-port names; LV2 bundles
are scanned from their Turtle manifests (best effort — plugins whose audio
ports can't be determined can still be used as a single-plugin insert,
because filter-chain infers the ports of a one-node graph itself).

VST3 and CLAP have no native PipeWire host; we only detect their presence so
the UI can point users at a bridge host (Carla, Element) instead of
pretending to support them.
"""

from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

LADSPA_DIRS = [Path(p) for p in
               os.environ.get('LADSPA_PATH', '').split(':') if p] or \
              [Path('/usr/lib/ladspa'), Path('/usr/local/lib/ladspa'),
               Path.home() / '.ladspa']
LV2_DIRS = [Path(p) for p in os.environ.get('LV2_PATH', '').split(':') if p] or \
           [Path('/usr/lib/lv2'), Path('/usr/local/lib/lv2'),
            Path.home() / '.lv2']
VST3_DIRS = [Path('/usr/lib/vst3'), Path.home() / '.vst3']
CLAP_DIRS = [Path('/usr/lib/clap'), Path.home() / '.clap']


@dataclass
class Plugin:
    type: str                 # 'ladspa' | 'lv2'
    plugin: str               # library path (ladspa) or URI (lv2)
    label: str                # ladspa label ('' for lv2)
    name: str                 # human-readable
    maker: str = ''
    audio_in: list = field(default_factory=list)    # port names/symbols
    audio_out: list = field(default_factory=list)

    @property
    def stereo(self) -> bool:
        return len(self.audio_in) >= 2 and len(self.audio_out) >= 2

    @property
    def ports_known(self) -> bool:
        return bool(self.audio_in and self.audio_out)


# ------------------------------------------------------------------ LADSPA --

_PORT_INPUT, _PORT_OUTPUT, _PORT_CONTROL, _PORT_AUDIO = 0x1, 0x2, 0x4, 0x8


class _LadspaDescriptor(ctypes.Structure):
    _fields_ = [
        ('UniqueID', ctypes.c_ulong),
        ('Label', ctypes.c_char_p),
        ('Properties', ctypes.c_int),
        ('Name', ctypes.c_char_p),
        ('Maker', ctypes.c_char_p),
        ('Copyright', ctypes.c_char_p),
        ('PortCount', ctypes.c_ulong),
        ('PortDescriptors', ctypes.POINTER(ctypes.c_int)),
        ('PortNames', ctypes.POINTER(ctypes.c_char_p)),
    ]


def _scan_ladspa_lib(path: Path) -> list[Plugin]:
    out = []
    try:
        lib = ctypes.CDLL(str(path))
        fn = lib.ladspa_descriptor
    except (OSError, AttributeError):
        return out
    fn.restype = ctypes.POINTER(_LadspaDescriptor)
    fn.argtypes = [ctypes.c_ulong]
    i = 0
    while True:
        try:
            desc = fn(i)
        except Exception:
            break
        if not desc:
            break
        d = desc.contents
        audio_in, audio_out = [], []
        for p in range(d.PortCount):
            pd = d.PortDescriptors[p]
            if not pd & _PORT_AUDIO:
                continue
            pname = (d.PortNames[p] or b'').decode('utf-8', 'replace')
            (audio_in if pd & _PORT_INPUT else audio_out).append(pname)
        out.append(Plugin(
            type='ladspa', plugin=str(path),
            label=(d.Label or b'').decode('utf-8', 'replace'),
            name=(d.Name or b'').decode('utf-8', 'replace'),
            maker=(d.Maker or b'').decode('utf-8', 'replace'),
            audio_in=audio_in, audio_out=audio_out))
        i += 1
        if i > 256:            # defensive: misbehaving library
            break
    return out


def scan_ladspa() -> list[Plugin]:
    plugins = []
    for d in LADSPA_DIRS:
        if not d.is_dir():
            continue
        for lib in sorted(d.glob('*.so')):
            plugins += _scan_ladspa_lib(lib)
    return plugins


# --------------------------------------------------------------------- LV2 --

_TTL_PLUGIN_RE = re.compile(
    r'<([^>\s]+)>\s+(?:\n\s*)?a\s+(?:[^;.]*\b)?lv2:Plugin\b', re.M)
_TTL_SEEALSO_RE = re.compile(r'rdfs:seeAlso\s+<([^>]+)>')
_TTL_NAME_RE = re.compile(r'doap:name\s+"((?:[^"\\]|\\.)*)"')
_TTL_SUBJECT_SPLIT = re.compile(r'^<([^>\s]+)>', re.M)


def _lv2_ports(block: str) -> tuple[list, list]:
    """Audio in/out port symbols from one plugin's Turtle block."""
    ins, outs = [], []
    for pm in re.finditer(r'\[(?:[^][]|\[[^]]*\])*\]', block, re.S):
        chunk = pm.group(0)
        if 'lv2:AudioPort' not in chunk:
            continue
        sym = re.search(r'lv2:symbol\s+"([^"]+)"', chunk)
        if not sym:
            continue
        if 'lv2:InputPort' in chunk:
            ins.append(sym.group(1))
        elif 'lv2:OutputPort' in chunk:
            outs.append(sym.group(1))
    return ins, outs


def _scan_lv2_bundle(bundle: Path) -> list[Plugin]:
    manifest = bundle / 'manifest.ttl'
    try:
        text = manifest.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    uris = []
    for m in _TTL_PLUGIN_RE.finditer(text):
        uri = m.group(1)
        # the manifest entry usually points to the file with the details
        tail = text[m.start():m.start() + 500]
        see = _TTL_SEEALSO_RE.search(tail)
        uris.append((uri, see.group(1) if see else None))
    plugins = []
    for uri, detail_file in uris:
        name, ins, outs = uri.rsplit('/', 1)[-1], [], []
        if detail_file:
            try:
                detail = (bundle / detail_file).read_text(
                    encoding='utf-8', errors='replace')
            except OSError:
                detail = ''
            # isolate this plugin's block in a possibly multi-plugin file
            block = detail
            subjects = [(m.start(), m.group(1))
                        for m in _TTL_SUBJECT_SPLIT.finditer(detail)]
            for i, (pos, subj) in enumerate(subjects):
                if subj == uri:
                    end = subjects[i + 1][0] if i + 1 < len(subjects) \
                        else len(detail)
                    block = detail[pos:end]
                    break
            nm = _TTL_NAME_RE.search(block)
            if nm:
                name = nm.group(1)
            ins, outs = _lv2_ports(block)
        plugins.append(Plugin(type='lv2', plugin=uri, label='', name=name,
                              audio_in=ins, audio_out=outs))
    return plugins


def scan_lv2() -> list[Plugin]:
    plugins = []
    for d in LV2_DIRS:
        if not d.is_dir():
            continue
        for bundle in sorted(d.iterdir()):
            if bundle.is_dir():
                plugins += _scan_lv2_bundle(bundle)
    return plugins


# ---------------------------------------------------------- VST3 / CLAP -----

def detect_unsupported() -> dict:
    """Presence of plugin formats PipeWire cannot host natively."""
    found = {}
    for fmt, dirs, pat in (('VST3', VST3_DIRS, '*.vst3'),
                           ('CLAP', CLAP_DIRS, '*.clap')):
        count = 0
        for d in dirs:
            if d.is_dir():
                count += len(list(d.glob(pat)))
        if count:
            found[fmt] = count
    return found


def have_bridge_host() -> str | None:
    """A plugin host that can bridge VST3/CLAP into PipeWire, if installed."""
    import shutil
    for host in ('carla', 'carla-rack', 'element'):
        if shutil.which(host):
            return host
    return None
