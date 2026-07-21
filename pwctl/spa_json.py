"""Parser and serializer for PipeWire's relaxed SPA JSON config format.

SPA JSON differs from strict JSON: keys may be bare words, `=` and `:` are
both accepted as separators, commas are optional, `#` starts a comment, and
top-level files are implicit objects.  The serializer emits the idiomatic
PipeWire style (bare keys, `=`, no commas).
"""

from __future__ import annotations


class SpaJsonError(ValueError):
    pass


_BARE_END = set('{}[]=:,#" \t\r\n')


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
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == '\\' and j + 1 < n:
                    esc = text[j + 1]
                    buf.append({'n': '\n', 't': '\t', 'r': '\r',
                                '"': '"', '\\': '\\'}.get(esc, esc))
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            if j >= n:
                raise SpaJsonError("unterminated string")
            tokens.append(('str', ''.join(buf)))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in _BARE_END:
                j += 1
            tokens.append(('bare', text[i:j]))
            i = j
    return tokens


def _bare_value(word: str):
    if word == 'true':
        return True
    if word == 'false':
        return False
    if word == 'null':
        return None
    try:
        return int(word)
    except ValueError:
        pass
    try:
        return float(word)
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


def _fmt_key(key: str) -> str:
    if key and all(ch in _SAFE_BARE for ch in key):
        return key
    return '"' + key.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _fmt_scalar(val) -> str:
    if val is True:
        return 'true'
    if val is False:
        return 'false'
    if val is None:
        return 'null'
    if isinstance(val, (int, float)):
        return repr(val)
    s = str(val)
    if s and all(ch in _SAFE_BARE for ch in s) and s not in ('true', 'false', 'null'):
        try:
            float(s)
        except ValueError:
            return s
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


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
