"""The equalizer's curve model, its response, and the APO/AutoEQ format.

Pure: no PipeWire, no GTK.  The response numbers are the ones PipeWire
1.6.9's own biquads measured as (docs.local/probes/2026-10-27-eq-engine).
Run: python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pwctl.backend import eqcurve as E  # noqa: E402
from pwctl.backend.enhance import band_controls, eq_controls  # noqa: E402

PROBE = E.Curve(-4.0, (E.Band('PK', 1000, 6.0, 1.0),
                       E.Band('LSC', 100, 4.0, 0.7),
                       E.Band('HSC', 8000, -3.0, 0.7),
                       E.Band('PK', 3500, -5.0, 4.0)))


class Response(unittest.TestCase):
    def test_matches_the_daemon(self):
        # measured through PipeWire at the FFT bin nearest each frequency
        want = {1000.0: 1.97, 3500.0: -8.55, 20000.0: -7.00}
        got = PROBE.response(list(want), 48000)
        for (f, w), g in zip(want.items(), got):
            self.assertAlmostEqual(g, w, delta=0.02, msg=f'{f} Hz')

    def test_every_type_has_an_exact_pass_through(self):
        for code, kind in E.TYPES.items():
            ident = dict(kind.identity)
            band = E.Band(code, ident.get('Freq', 1000.0),
                          ident.get('Gain', 0.0), ident.get('Q', 1.0))
            for db in band.response([30, 1000, 15000]):
                self.assertAlmostEqual(db, 0.0, places=9, msg=code)

    def test_an_off_band_is_silent_in_the_sum(self):
        c = E.Curve(0, (E.Band('PK', 1000, 12, 1, on=False),))
        self.assertEqual(c.response([1000])[0], 0.0)

    def test_auto_preamp_never_clips(self):
        c = PROBE.auto_preamp()
        self.assertLessEqual(c.peak_db(), 0.0)
        self.assertGreater(c.peak_db(), -0.15)
        cuts = E.Curve(3, (E.Band('PK', 1000, -6, 1),)).auto_preamp()
        self.assertEqual(cuts.preamp, 0.0)

    def test_flat_is_flat(self):
        c = PROBE.added(E.Band('HPQ', 60, 0, 0.7)).flat()
        self.assertLess(max(abs(x) for x in c.response(E.log_freqs(50))),
                        1e-9)


class Model(unittest.TestCase):
    def test_stored_values_load_unclamped(self):
        c = E.Curve.from_params({'preamp': 22, 'bands': [
            {'type': 'PK', 'freq': 1000, 'gain': 23, 'q': 12}]})
        self.assertEqual(c.bands[0].gain, 23)
        self.assertFalse(c.fits_live())
        self.assertTrue(c.clamped().fits_live())

    def test_unknown_keys_survive_a_round_trip(self):
        d = {'on': True, 'type': 'PK', 'freq': 100, 'gain': 1, 'q': 1,
             'channels': ['FL']}
        self.assertEqual(E.Band.from_dict(d).to_dict()['channels'], ['FL'])

    def test_bad_values_do_not_raise(self):
        b = E.Band.from_dict({'type': 'XX', 'freq': 'nan', 'gain': None})
        self.assertEqual((b.type, b.freq, b.gain), ('PK', 1000.0, 0.0))

    def test_bandwidth_conversion_round_trips(self):
        for q in (0.3, 0.71, 1, 4.3):
            self.assertAlmostEqual(E.octaves_to_q(E.q_to_octaves(q)), q)


class Text(unittest.TestCase):
    def test_round_trip(self):
        c = PROBE.added(E.Band('HPQ', 30, 0, 0.71, on=False))
        self.assertEqual(E.parse_text(E.to_text(c)).curve, c)

    def test_writer_keeps_the_file_engine_layout(self):
        self.assertEqual(
            E.to_text(E.Curve(-6.4, (E.Band('LSC', 105, 6.0, 0.7),))),
            'Preamp: -6.4 dB\nFilter 1: ON LSC Fc 105 Hz Gain 6 dB Q 0.7\n')

    def test_apo_variants(self):
        p = E.parse_text('''
            Preamp: -3 dB
            Preamp: -1,5 dB
            Filter: ON PEQ Fc 1 kHz Gain +2.0 dB BW Oct 1.0
            Filter 2: ON HP Fc 30 Hz
            Filter 3: OFF LSC 12 dB Fc 100 Hz Gain 4 dB
            Filter 4: ON IIR Order 2 Coefficients 1 0 0 1 0 0
            Filter 5: ON PK Fc 300 Hz Gain 40 dB Q 50
        ''')
        bands = p.curve.bands
        self.assertEqual(p.curve.preamp, -4.5)
        self.assertEqual([b.type for b in bands], ['PK', 'HPQ', 'LSC', 'PK'])
        self.assertEqual(bands[0].freq, 1000)
        self.assertAlmostEqual(bands[0].q, E.octaves_to_q(1.0))
        self.assertFalse(bands[2].on)
        self.assertEqual((bands[3].gain, bands[3].q), (E.GAIN_MAX, E.Q_MAX))
        notes = ' '.join(p.notes)
        self.assertIn('IIR', notes)
        self.assertIn('brought inside', notes)

    def test_only_the_first_channel_section_is_used(self):
        p = E.parse_text('Channel: L\nFilter: ON PK Fc 100 Hz Gain 1 dB Q 1\n'
                         'Channel: R\nFilter: ON PK Fc 200 Hz Gain 1 dB Q 1\n')
        self.assertEqual([b.freq for b in p.curve.bands], [100])
        self.assertTrue(p.notes)


class Controls(unittest.TestCase):
    def test_names_follow_the_graph(self):
        ctl = eq_controls(PROBE)
        self.assertAlmostEqual(ctl['pre:Mult'], 10 ** (-4 / 20))
        self.assertEqual(ctl['b1:Gain'], 6.0)
        self.assertEqual(ctl['b2:Freq'], 100.0)

    def test_bypass_and_off_are_pass_throughs(self):
        ctl = eq_controls(PROBE, bypass=True)
        self.assertEqual(ctl['pre:Mult'], 1.0)
        self.assertTrue(all(ctl[f'b{i}:Gain'] == 0.0 for i in range(1, 5)))
        off = band_controls(E.Band('HPQ', 80, 0, 0.7, on=False), 3)
        self.assertEqual(off['b3:Freq'], 0.0)
        self.assertNotIn('b3:Gain', off)


    def test_level_matched_bypass(self):
        ctl = eq_controls(PROBE, bypass=True, bypass_db=-6.0)
        self.assertAlmostEqual(ctl['pre:Mult'], 10 ** (-6 / 20))
        self.assertEqual(ctl['b1:Gain'], 0.0)
        # past the preamp's range it is clamped like a preamp
        ctl = eq_controls(PROBE, bypass=True, bypass_db=40.0)
        self.assertAlmostEqual(ctl['pre:Mult'], 10.0)


class Loudness(unittest.TestCase):
    """Curve.level_db: pink noise's loudness change, K-weighted."""

    def test_a_preamp_is_its_own_level(self):
        self.assertAlmostEqual(E.Curve(-6.0, ()).level_db(), -6.0, places=6)
        self.assertAlmostEqual(E.Curve().level_db(), 0.0, places=6)

    def test_what_you_hardly_hear_hardly_counts(self):
        low_cut = E.Curve(0.0, (E.Band('HPQ', 30, 0.0, 0.7),))
        self.assertGreater(low_cut.level_db(), -0.2)
        mid = E.Curve(0.0, (E.Band('PK', 2000, 6.0, 1.0),)).level_db()
        bass = E.Curve(0.0, (E.Band('PK', 40, 6.0, 1.0),)).level_db()
        self.assertGreater(mid, bass)
        self.assertTrue(0.0 < mid < 6.0)

    def test_off_bands_change_nothing(self):
        off = E.Curve(-2.0, (E.Band('PK', 1000, 9.0, 1.0, on=False),))
        self.assertAlmostEqual(off.level_db(), -2.0, places=6)


if __name__ == '__main__':
    unittest.main()
