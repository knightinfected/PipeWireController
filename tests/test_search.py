"""App-wide search: folding, matching, ranking, highlights, speed.

Pure: no PipeWire, no GTK.  The items mirror real ones (the schema's titles
and descriptions, page names), so a ranking change that would surprise a
user shows up here.
Run: python3 -m unittest discover -s tests
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pwctl.backend import schema  # noqa: E402
from pwctl.backend.search import (Index, Item, fold, group_hits,  # noqa: E402
                                  words)


def page(name, title, keywords=()):
    return Item(f'page:{name}', title, 'page', keywords=keywords,
                title_boost=30)


PAGES = [
    page('enhance', 'Equalizer', ('mic', 'noise', 'autoeq')),
    page('server', 'Server', ('sample rate', 'quantum', 'latency')),
    page('wireplumber', 'Session & Bluetooth', ('codec', 'ldac')),
    page('surround', 'Surround Setup', ('5.1', '7.1', 'speaker test')),
    page('devices', 'Devices', ('speakers', 'headphones')),
]
SETTINGS = [Item(f'setting:{s.key}', s.title, 'setting',
                 subtitle='Server · Clock and buffering', keywords=(s.key,),
                 text=s.subtitle, boost=4 if s.advanced else 8)
            for s in schema.PIPEWIRE_CLOCK + schema.PIPEWIRE_ADVANCED
            + schema.STREAM]
SETTINGS += [Item(f'wp:{k}', t, 'setting', subtitle='Session & Bluetooth',
                  text=s, boost=8) for k, t, s in schema.WIREPLUMBER]
OTHER = [
    Item('row:surround/virtual71', 'Virtual 7.1 Headphones', 'action',
         subtitle='Surround Setup'),
    Item('device:out:cafe', 'Café Speakers', 'device'),
    Item('pref:color_scheme', 'Style', 'pref', subtitle='General',
         keywords=('dark', 'light', 'theme', 'appearance')),
    Item('app:firefox', 'Firefox', 'app'),
]
INDEX = Index(PAGES + SETTINGS + OTHER)


def top(query, n=1):
    return [h.item.key for h in INDEX.search(query)[:n]]


class Folding(unittest.TestCase):
    def test_accents_and_case(self):
        self.assertEqual(fold('Équaliseur CAFÉ'), 'equaliseur cafe')

    def test_numbers_keep_decimals(self):
        self.assertEqual(words('Virtual 7.1 at 44.1 kHz'),
                         ['virtual', '7.1', 'at', '44.1', 'khz'])

    def test_config_keys_split(self):
        self.assertEqual(words('default.clock.quantum'),
                         ['default', 'clock', 'quantum'])


class Ranking(unittest.TestCase):
    def test_empty_query_finds_nothing(self):
        self.assertEqual(INDEX.search(''), [])
        self.assertEqual(INDEX.search('  ·  '), [])

    def test_page_by_name(self):
        self.assertEqual(top('equa'), ['page:enhance'])
        self.assertEqual(top('Server'), ['page:server'])

    def test_abbreviations(self):
        self.assertEqual(top('eq'), ['page:enhance'])
        self.assertEqual(top('bt'), ['page:wireplumber'])

    def test_spelling(self):
        self.assertEqual(top('equaliser'), ['page:enhance'])

    def test_setting_by_title_words(self):
        self.assertEqual(top('sample rate'), ['setting:default.clock.rate'])
        self.assertEqual(top('min quantum'),
                         ['setting:default.clock.min-quantum'])

    def test_setting_by_config_key(self):
        self.assertEqual(top('default.clock.quantum'),
                         ['setting:default.clock.quantum'])
        self.assertEqual(top('channelmix.lfe-cutoff'),
                         ['setting:channelmix.lfe-cutoff'])

    def test_all_words_must_match(self):
        keys = [h.item.key for h in INDEX.search('upmix method')]
        self.assertEqual(keys, ['setting:channelmix.upmix-method'])

    def test_filler_words_are_optional(self):
        self.assertEqual(top('dark mode'), ['pref:color_scheme'])
        self.assertEqual(top('how to change the sample rate'),
                         ['setting:default.clock.rate'])

    def test_description_finds_a_symptom(self):
        hits = INDEX.search('crackling')
        keys = [h.item.key for h in hits]
        self.assertIn('wp:alsa_headroom', keys)
        why = {h.item.key: h.why for h in hits}
        self.assertEqual(why['wp:alsa_headroom'], 'text')

    def test_title_beats_description(self):
        hits = INDEX.search('surround')
        last_title = max(i for i, h in enumerate(hits) if h.why == 'title')
        first_text = min(i for i, h in enumerate(hits) if h.why == 'text')
        self.assertLess(last_title, first_text)

    def test_a_page_named_comes_first(self):
        self.assertEqual(top('bluetooth'), ['page:wireplumber'])
        self.assertEqual(top('surround'), ['page:surround'])

    def test_a_page_only_about_a_word_does_not(self):
        keys = [h.item.key for h in INDEX.search('quantum')]
        self.assertTrue(keys[0].startswith('setting:'), keys[:3])
        self.assertIn('page:server', keys)

    def test_numbers(self):
        self.assertEqual(top('7.1'), ['row:surround/virtual71'])

    def test_accented_title(self):
        self.assertEqual(top('cafe'), ['device:out:cafe'])

    def test_typo_still_finds_the_page(self):
        self.assertEqual(top('equlizer'), ['page:enhance'])

    def test_plural(self):
        self.assertIn('setting:default.clock.allowed-rates',
                      [h.item.key for h in INDEX.search('allowed rate')])

    def test_nonsense_finds_nothing(self):
        self.assertEqual(INDEX.search('zzqx'), [])


class Highlight(unittest.TestCase):
    def test_spans_are_on_the_title(self):
        hit = INDEX.search('samp')[0]
        self.assertEqual(hit.item.title, 'Default sample rate')
        self.assertEqual(hit.spans, [(8, 12)])

    def test_spans_survive_accents(self):
        hit = INDEX.search('cafe')[0]
        a, b = hit.spans[0]
        self.assertEqual(hit.item.title[a:b], 'Café')

    def test_two_words_two_spans(self):
        hit = INDEX.search('min quant')[0]
        self.assertEqual([hit.item.title[a:b] for a, b in hit.spans],
                         ['Min', 'quant'])


class Grouping(unittest.TestCase):
    def test_sections_follow_their_best_hit(self):
        groups = group_hits(INDEX.search('bluetooth'), per_kind=2)
        self.assertEqual(groups[0][0], 'page')
        kinds = [k for k, _h, _n in groups]
        self.assertEqual(len(kinds), len(set(kinds)))
        for _k, shown, _more in groups:
            self.assertLessEqual(len(shown), 2)
        setting = next(g for g in groups if g[0] == 'setting')
        self.assertGreater(setting[2], 0)       # more than two BT settings


class Speed(unittest.TestCase):
    def test_a_keystroke_is_quick(self):
        many = [Item(f'x{i}', f'{t} {i}', 'device', subtitle='Devices',
                     text='A long description of where this one is ' * 3)
                for i, t in enumerate(['Speakers', 'Headphones', 'Mic',
                                       'HDMI Output', 'USB Interface'] * 200)]
        index = Index(many + PAGES + SETTINGS + OTHER)
        queries = ['s', 'sp', 'spe', 'spea', 'speak', 'quantum',
                   'head phones', 'equlizer', 'zzqx', 'how to set the rate']
        t0 = time.perf_counter()
        for q in queries:
            index.search(q)
        per = (time.perf_counter() - t0) / len(queries) * 1000
        # ~1 100 items; a frame is 16 ms.  Generous for a slow machine.
        self.assertLess(per, 40, f'{per:.1f} ms per keystroke')


if __name__ == '__main__':
    unittest.main()
