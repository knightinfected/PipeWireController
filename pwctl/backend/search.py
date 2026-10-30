"""Search the whole app: the index and the matching.  No GTK.

The window turns what it can show into `Item`s (`ui/search.py`): the pages,
the rows the pages draw, the live devices and apps, what the user has made,
and Preferences.  This module only matches a query against them, and it has
to be fast enough to run on every keystroke, so everything that can be is
worked out once, when the items are indexed.

How a query matches
-------------------
Text is folded first (accents dropped, case folded), then split into words;
a number keeps its decimals, so "7.1" and "44.1" stay one word.  Each query
word must match somewhere in the item:

  * an exact word beats the start of a word, which beats a word the query
    is a plural or "-ing" of, which beats the middle of a word (three
    letters or more);
  * where it matched counts: the title, then hidden keywords (synonyms, a
    setting's config key), then where the item is (its page and section),
    then its description;
  * a few abbreviations and spellings stand in for each other (`SYNONYMS`),
    at a little less than the word itself;
  * filler words ("mode", "settings", "how", "the") are optional: they
    count when they match, and are never the reason nothing does.

A long word with no match anywhere gets one last, weak chance against the
title as a subsequence ("equlizer" still finds the Equalizer).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Letters and digits; a number keeps its decimals ("7.1", "44.1").
_WORD = re.compile(r'[^\W_]+(?:[.,][0-9]+)*')

# What each kind is called as a heading, in the order a tie is broken.
KINDS = {
    'page': 'Pages',
    'section': 'Sections',
    'setting': 'Settings',
    'action': 'Actions',
    'device': 'Devices',
    'app': 'Apps',
    'mine': 'Your setup',
    'pref': 'Preferences',
}
_KIND_ORDER = {k: i for i, k in enumerate(KINDS)}

# Abbreviations, spellings and near words.  One word each way it is typed;
# the words it stands for are matched at SYNONYM_WEIGHT.
SYNONYMS = {
    'eq': ('equalizer', 'equalizers'),
    'eqs': ('equalizer', 'equalizers'),
    'equaliser': ('equalizer',),
    'equalisers': ('equalizer',),
    'bt': ('bluetooth',),
    'mic': ('microphone',),
    'mics': ('microphone',),
    'microphone': ('mic',),
    'favorite': ('favourite',),
    'favorites': ('favourites',),
    'favourite': ('favorite',),
    'favourites': ('favorites',),
    'color': ('colour', 'style'),
    'colour': ('color', 'style'),
    'theme': ('style', 'appearance'),
    'dark': ('style', 'appearance'),
    'light': ('style', 'appearance'),
    'latency': ('quantum', 'buffer'),
    'buffer': ('quantum',),
    'samplerate': ('rate',),
    'crackling': ('crackle', 'crackles', 'xruns', 'dropouts'),
    'crackle': ('crackles', 'xruns', 'dropouts'),
    'crackles': ('crackle', 'xruns', 'dropouts'),
    'stutter': ('xruns', 'dropouts'),
    'xrun': ('xruns', 'dropouts'),
    'xruns': ('dropouts',),
    'dropout': ('dropouts', 'xruns'),
    'dropouts': ('xruns',),
    'aggregate': ('combined', 'combine'),
    'loopback': ('virtual',),
    'hrtf': ('hrir',),
    'binaural': ('hrir',),
    'spatial': ('surround', 'hrir'),
    'autostart': ('login',),
    'boot': ('login',),
    'tray': ('background',),
    'log': ('journal', 'logs'),
    'logs': ('journal', 'log'),
    'cable': ('patchbay', 'links'),
    'cables': ('patchbay', 'links'),
    'wire': ('patchbay', 'links'),
    'wires': ('patchbay', 'links'),
    'vst': ('plugins',),
    'vst3': ('plugins',),
    'clap': ('plugins',),
    'denoise': ('noise',),
    'rnnoise': ('noise',),
    'speaker': ('speakers', 'output'),
    'speakers': ('speaker', 'output'),
    'headphone': ('headphones',),
    'headset': ('headphones', 'bluetooth'),
}
SYNONYM_WEIGHT = 0.9

# Optional words: they add to a match, never stop one.
STOP = frozenset((
    'a', 'an', 'and', 'are', 'can', 'change', 'do', 'does', 'enable', 'for',
    'how', 'i', 'in', 'is', 'it', 'make', 'me', 'mode', 'my', 'of', 'on',
    'option', 'options', 'set', 'setting', 'settings', 'show', 'the', 'to',
    'turn', 'use', 'what', 'where', 'with',
))

# Weights of the four places a word can match.
W_TITLE, W_KEYWORDS, W_WHERE, W_TEXT = 1.0, 0.85, 0.55, 0.4
# The whole query is the title, or how it starts.  Kept small: generic
# titles ("Sample rate") exist in more than one place.
EXACT_BONUS, PREFIX_BONUS = 25.0, 12.0


@dataclass
class Item:
    """One thing search can find.

    `subtitle` says where it is ("Server · Clock and buffering"); it is
    shown, and matched as the place.  `keywords` are matched but never
    shown (synonyms, a setting's config key); `text` is a description,
    matched at a low weight and shown only when it is why the item matched.
    `target` is the window's to open; this module never looks at it.
    """
    key: str
    title: str
    kind: str = 'setting'
    subtitle: str = ''
    icon: str = ''
    keywords: tuple = ()
    text: str = ''
    tag: str = ''
    target: object = None
    boost: float = 0.0
    # added only when every query word matched the title: a page whose
    # name you typed comes first, one that is only about the word doesn't
    title_boost: float = 0.0


@dataclass
class Hit:
    item: Item
    score: float
    # (start, end) character ranges of the title the query matched
    spans: list = field(default_factory=list)
    # 'title' | 'keywords' | 'where' | 'text': the weakest place a query
    # word needed ('text': only the description has it)
    why: str = 'title'


def fold(text: str) -> str:
    """Accents dropped, case folded: "Équaliseur" → "equaliseur"."""
    out = unicodedata.normalize('NFKD', text or '')
    return ''.join(c for c in out if not unicodedata.combining(c)).casefold()


def words(text: str) -> list[str]:
    return _WORD.findall(fold(text))


def _fold_map(text: str) -> tuple[str, list[int]]:
    """The folded text, and for each of its characters the index of the
    character it came from, so a match can be drawn on the original."""
    out, where = [], []
    for i, c in enumerate(text or ''):
        f = fold(c)
        out.append(f)
        where.extend([i] * len(f))
    return ''.join(out), where


class _Prepared:
    __slots__ = ('item', 'folded', 'where_map', 'twords', 'kwords',
                 'swords', 'xwords', 'hay', 'flat')

    def __init__(self, item: Item):
        self.item = item
        self.folded, self.where_map = _fold_map(item.title)
        self.twords = [(m.group(0), m.start())
                       for m in _WORD.finditer(self.folded)]
        self.kwords = [w for k in item.keywords for w in words(k)]
        self.swords = words(item.subtitle)
        self.xwords = words(item.text)
        # Every word, for the cheap first test (a substring search in C).
        self.hay = ' '.join([w for w, _s in self.twords] + self.kwords
                            + self.swords + self.xwords)
        self.flat = ''.join(w for w, _s in self.twords)


def _word_score(t: str, w: str) -> tuple[float, int]:
    """How well query word `t` matches word `w`, and where in `w`."""
    if w == t:
        return 100.0, 0
    if w.startswith(t):
        return 72.0 + 20.0 * len(t) / len(w), 0
    if len(w) >= 4 and t.startswith(w) and len(t) - len(w) <= 3:
        return 70.0, 0                      # "speakers" for "speaker"
    if len(t) >= 3:
        at = w.find(t)
        if at > 0:
            return 40.0, at
    return 0.0, 0


def _subsequence(t: str, flat: str) -> bool:
    """`t` in order inside `flat`, spread over at most twice its length."""
    start = flat.find(t[0])
    while start >= 0:
        i, j = 0, start
        while i < len(t) and j < len(flat) and j - start < 2 * len(t):
            if flat[j] == t[i]:
                i += 1
            j += 1
        if i == len(t):
            return True
        start = flat.find(t[0], start + 1)
    return False


class Index:
    """The items, prepared once; `search` runs against them."""

    def __init__(self, items=()):
        self._items = [_Prepared(i) for i in items]

    def __len__(self):
        return len(self._items)

    @property
    def items(self) -> list[Item]:
        return [p.item for p in self._items]

    def _match(self, t: str, p: _Prepared):
        """Best (score, place, span) for query word `t` alone."""
        best, why, span = 0.0, None, None
        for n, (w, start) in enumerate(p.twords):
            s, at = _word_score(t, w)
            if s:
                s += 6.0 if n == 0 else 0.0
                if s > best:
                    end = start + at + min(len(t), len(w) - at)
                    best, why, span = s, 'title', (start + at, end)
        for place, weight, ws in (('keywords', W_KEYWORDS, p.kwords),
                                  ('where', W_WHERE, p.swords),
                                  ('text', W_TEXT, p.xwords)):
            if best >= 100.0 * weight:
                continue                    # it can't do better here
            for w in ws:
                s, at = _word_score(t, w)
                if place == 'text' and (len(t) < 3 or at):
                    continue                # descriptions: whole words only
                if s * weight > best:
                    best, why, span = s * weight, place, None
        return best, why, span

    @staticmethod
    def _may_match(t: str, alts, p: _Prepared) -> bool:
        """The cheap test first (substring searches run in C): can query
        word `t` match this item at all?  Never says no to a real match."""
        if any(a in p.hay for a in alts):
            return True
        if len(t) < 4:
            return False
        # a plural or "-ing" of a word that is there, or a subsequence
        return t[:max(4, len(t) - 3)] in p.hay or _subsequence(t, p.flat)

    def _score_word(self, t: str, p: _Prepared):
        best = self._match(t, p)
        for alt in SYNONYMS.get(t, ()):
            if alt in p.hay:
                s, why, span = self._match(alt, p)
                if s * SYNONYM_WEIGHT > best[0]:
                    best = (s * SYNONYM_WEIGHT, why, span)
        if not best[0] and len(t) >= 4 and _subsequence(t, p.flat):
            best = (22.0, 'title', None)
        return best

    def search(self, query: str, limit: int | None = None) -> list[Hit]:
        toks = words(query)
        if not toks:
            return []
        need = [t for t in toks if t not in STOP] or toks
        extra = [t for t in toks if t not in need]
        qfold = ' '.join(toks)
        alts = {t: (t,) + SYNONYMS.get(t, ()) for t in need}
        hits = []
        for p in self._items:
            if not all(self._may_match(t, alts[t], p) for t in need):
                continue
            total, spans, places = 0.0, [], set()
            for t in need:
                s, why, span = self._score_word(t, p)
                if not s:
                    break
                total += s
                places.add(why)
                if span:
                    spans.append(span)
            else:
                for t in extra:
                    s, why, span = self._match(t, p)
                    if s:
                        total += s * 0.25
                        if span:
                            spans.append(span)
                score = total / len(need)
                title = ' '.join(w for w, _s in p.twords)
                if title == qfold:
                    score += EXACT_BONUS
                elif title.startswith(qfold):
                    score += PREFIX_BONUS
                if places == {'title'}:
                    score += p.item.title_boost
                score += p.item.boost - 0.05 * len(p.item.title)
                # The weakest place a word needed: 'text' means the
                # description is why it matched, so the UI shows it.
                why = next(w for w in ('text', 'where', 'keywords', 'title')
                           if w in places)
                hits.append(Hit(p.item, score, self._spans(p, spans), why))
        hits.sort(key=lambda h: (-h.score, _KIND_ORDER.get(h.item.kind, 99),
                                 h.item.title.casefold()))
        return hits[:limit] if limit else hits

    @staticmethod
    def _spans(p: _Prepared, spans) -> list[tuple[int, int]]:
        """Folded ranges back onto the original title, merged."""
        out = []
        for a, b in sorted(spans):
            if a >= len(p.where_map) or b <= a:
                continue
            start = p.where_map[a]
            end = p.where_map[min(b, len(p.where_map)) - 1] + 1
            if out and start <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], end))
            else:
                out.append((start, end))
        return out


def group_hits(hits: list[Hit], per_kind: int | None = None):
    """Hits in sections: [(kind, [hits], hidden)], sections ordered by their
    best hit, hits in each by score, at most `per_kind` shown (`hidden`
    counts the rest).  So the first row is always the best match."""
    order, by_kind = [], {}
    for h in hits:
        k = h.item.kind
        if k not in by_kind:
            by_kind[k] = []
            order.append(k)
        by_kind[k].append(h)
    out = []
    for k in order:
        hs = by_kind[k]
        shown = hs if per_kind is None else hs[:per_kind]
        out.append((k, shown, len(hs) - len(shown)))
    return out
