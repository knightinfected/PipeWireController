"""Parser and serializer for PipeWire's relaxed SPA JSON config format.

SPA JSON differs from strict JSON: keys may be bare words, `=` and `:` are
both accepted as separators, commas are optional, `#` starts a comment, and
top-level files are implicit objects.  The serializer emits the idiomatic
PipeWire style (bare keys, `=`, no commas).

Values are read the way PipeWire's own parser reads them (spa/utils/json-core.h,
the same from 1.4.2 to 1.6.9): `\\uXXXX` escapes decode, `0x10` is the integer
16 and `010` is 8 (strtol base 0), and `inf`, `nan` or `1_000` are words, not
numbers.  A number keeps the text it was written as, and the serializer writes
that text back, so a parsed file keeps its numbers exactly.  That matters
because PipeWire stores a value's text and each reader decides what it means:
the daemon reads its clock settings with atoi(), so `0x400` is 0 there.
"""

from __future__ import annotations

import math
import re


class SpaJsonError(ValueError):
    pass


class SpaInt(int):
    """An integer read from a file, remembering how the file wrote it."""

    def __new__(cls, value: int, text: str):
        obj = super().__new__(cls, value)
        obj.text = text
        return obj

    def __getnewargs__(self):
        return int(self), self.text


class SpaFloat(float):
    """A float read from a file, remembering how the file wrote it."""

    def __new__(cls, value: float, text: str):
        obj = super().__new__(cls, value)
        obj.text = text
        return obj

    def __getnewargs__(self):
        return float(self), self.text


_BARE_END = set('{}[]=:,#" \t\r\n')
_ESCAPES = {'n': '\n', 'r': '\r', 'b': '\b', 't': '\t', 'f': '\f',
            '"': '"', '\\': '\\', '/': '/'}
_HEX = set('0123456789abcdefABCDEF')


def _hex4(text: str, i: int):
    s = text[i:i + 4]
    if len(s) == 4 and all(ch in _HEX for ch in s):
        return int(s, 16)
    return None


def _string(text: str, i: int):
    """Decode the string whose opening quote is at i -> (value, next index).

    Mirrors spa_json_parse_stringn: a malformed \\u keeps the 'u', and a
    surrogate without its partner is dropped.
    """
    n = len(text)
    j = i + 1
    buf = []
    while j < n and text[j] != '"':
        ch = text[j]
        if ch != '\\' or j + 1 >= n:
            buf.append(ch)
            j += 1
            continue
        esc = text[j + 1]
        if esc != 'u':
            if esc in _ESCAPES:
                buf.append(_ESCAPES[esc])
            else:
                buf.append(esc)
            j += 2
            continue
        cp = _hex4(text, j + 2)
        if cp is None:
            buf.append('u')
            j += 2
            continue
        j += 6
        if 0xD800 <= cp <= 0xDBFF:
            lo = _hex4(text, j + 2) if text.startswith('\\u', j) else None
            if lo is None or not 0xDC00 <= lo <= 0xDFFF:
                continue
            j += 6
            cp = 0x10000 + (((cp & 0x3FF) << 10) | (lo & 0x3FF))
        elif 0xDC00 <= cp <= 0xDFFF:
            continue
        buf.append(chr(cp))
    if j >= n:
        raise SpaJsonError("unterminated string")
    return ''.join(buf), j + 1


def _tokenize(text: str):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ' \t\r\n,':
            i += 1
        elif c == '#':
            while i < n and text[i] != '\n':
                i += 1
        elif c in '{}[]=:':
            tokens.append(('=' if c == ':' else c, c))
            i += 1
        elif c == '"':
            val, i = _string(text, i)
            tokens.append(('str', val))
        else:
            j = i
            while j < n and text[j] not in _BARE_END:
                j += 1
            tokens.append(('bare', text[i:j]))
            i = j
    return tokens


# strtol(..., 0) consuming the whole word: hex, octal (a leading 0), decimal.
_INT_RE = re.compile(r'[+-]?(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)')
# spa_json_parse_float only looks at words made of these.
_FLOAT_CHARS = frozenset('+-0123456789.eE')


def _bare_value(word: str):
    if word == 'true':
        return True
    if word == 'false':
        return False
    if word == 'null':
        return None
    m = _INT_RE.fullmatch(word)
    if m:
        body = m.group(1)
        if body[1:2] in ('x', 'X'):
            base = 16
        elif body.startswith('0') and len(body) > 1:
            base = 8
        else:
            base = 10
        return SpaInt(int(word, base), word)
    if word and set(word) <= _FLOAT_CHARS:
        try:
            return SpaFloat(float(word), word)
        except ValueError:
            pass
    return word


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def next(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def parse_value(self):
        kind, val = self.next()
        if kind == '{':
            return self.parse_object()
        if kind == '[':
            return self.parse_array()
        if kind == 'str':
            return val
        if kind == 'bare':
            return _bare_value(val)
        raise SpaJsonError(f"unexpected token {val!r}")

    def parse_object(self, until='}'):
        obj = {}
        while True:
            kind, val = self.peek()
            if kind is None:
                if until is None:
                    return obj
                raise SpaJsonError("unterminated object")
            if kind == until:
                self.next()
                return obj
            if kind not in ('str', 'bare'):
                raise SpaJsonError(f"expected key, got {val!r}")
            self.next()
            key = val
            kind2, _ = self.peek()
            if kind2 == '=':
                self.next()
            obj[key] = self.parse_value()

    def parse_array(self):
        arr = []
        while True:
            kind, val = self.peek()
            if kind is None:
                raise SpaJsonError("unterminated array")
            if kind == ']':
                self.next()
                return arr
            arr.append(self.parse_value())


def loads(text: str):
    """Parse SPA JSON text. Top level may be a bare object body."""
    tokens = _tokenize(text)
    if not tokens:
        return {}
    p = _Parser(tokens)
    kind, _ = p.peek()
    if kind in ('{', '['):
        val = p.parse_value()
        # A top-level file may still continue with more keys (rare); if the
        # first value consumed everything, return it.
        if p.pos >= len(p.tokens):
            return val
        p.pos = 0
    return p.parse_object(until=None)


def load_file(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return loads(f.read())


_SAFE_BARE = set('abcdefghijklmnopqrstuvwxyz'
                 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+*~@/')
# PipeWire refuses a raw control character inside quotes, so each one is
# written as an escape it decodes back.
_WRITE_ESCAPES = {'\n': '\\n', '\r': '\\r', '\t': '\\t', '\b': '\\b',
                  '\f': '\\f', '"': '\\"', '\\': '\\\\'}


def quote(s: str) -> str:
    """`s` as a quoted SPA JSON string that PipeWire reads back as `s`."""
    out = ['"']
    for ch in s:
        if ch in _WRITE_ESCAPES:
            out.append(_WRITE_ESCAPES[ch])
        elif ch < ' ':
            out.append('\\u%04x' % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def _fmt_key(key: str) -> str:
    if key and all(ch in _SAFE_BARE for ch in key):
        return key
    return quote(key)


def _fmt_scalar(val) -> str:
    if val is True:
        return 'true'
    if val is False:
        return 'false'
    if val is None:
        return 'null'
    if isinstance(val, (SpaInt, SpaFloat)):
        return val.text
    if isinstance(val, float) and not math.isfinite(val):
        raise ValueError(f'{val!r} cannot be written: PipeWire would read it '
                         f'as a word, not a number')
    if isinstance(val, (int, float)):
        return repr(val)
    s = str(val)
    if s and all(ch in _SAFE_BARE for ch in s) and s not in ('true', 'false', 'null'):
        try:
            float(s)
        except ValueError:
            return s
    return quote(s)


def _dump(val, indent: int, top: bool = False) -> str:
    pad = '    ' * indent
    pad_in = '    ' * (indent + 1)
    if isinstance(val, dict):
        if not val:
            return '{ }'
        lines = [] if top else ['{']
        inner = pad if top else pad_in
        for k, v in val.items():
            lines.append(f"{inner}{_fmt_key(k)} = {_dump(v, indent if top else indent + 1)}")
        if not top:
            lines.append(pad + '}')
        return '\n'.join(lines)
    if isinstance(val, list):
        if not val:
            return '[ ]'
        if all(not isinstance(x, (dict, list)) for x in val):
            return '[ ' + ' '.join(_fmt_scalar(x) for x in val) + ' ]'
        lines = ['[']
        for x in val:
            lines.append(f"{pad_in}{_dump(x, indent + 1)}")
        lines.append(pad + ']')
        return '\n'.join(lines)
    return _fmt_scalar(val)


def dumps(obj, header: str | None = None) -> str:
    """Serialize a dict to SPA JSON conf-file text (top-level braces omitted)."""
    out = ''
    if header:
        out += ''.join(f"# {line}\n" for line in header.splitlines()) + '\n'
    out += _dump(obj, 0, top=True) + '\n'
    return out
