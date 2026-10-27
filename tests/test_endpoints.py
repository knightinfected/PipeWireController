"""The Devices page's model: what each card says, from nodes and streams.

Pure: `pwctl.backend.endpoints` takes the lists the page already has and
never shells out, so every rule here runs without PipeWire or a window.
"""

from unittest import TestCase

from pwctl.backend import endpoints
from pwctl.backend.pw import AudioNode, Stream
from pwctl.backend.surround import Card


def node(nid, name, sink=True, desc=None, **props):
    return AudioNode(id=nid, name=name, description=desc or name,
                     media_class='Audio/Sink' if sink else 'Audio/Source',
                     props=props, serial=nid + 100)


def stream(sid, target, playback=True, name='app', **props):
    s = Stream(id=sid, serial=sid + 100, name=name, media='',
               media_class='Stream/Output/Audio' if playback
               else 'Stream/Input/Audio', props=props)
    s.target_id = target
    return s


def card(cid, profiles, active, dirs, name='alsa_card.x'):
    return Card(id=cid, name=name, description='Card', profiles=profiles,
                active_profile=active, dirs=dirs,
                prio={i: 10 * i for i, *_ in profiles})


SPEAKERS = node(1, 'alsa_output.pci', **{'device.api': 'alsa',
                                         'device.bus': 'pci',
                                         'device.id': 9})
MIC = node(2, 'alsa_input.pci', sink=False,
           **{'device.api': 'alsa', 'device.bus': 'pci', 'device.id': 9})


class ActivityTests(TestCase):
    def test_apps_and_monitor_recorders_land_on_their_device(self):
        acts = endpoints.activity(
            [SPEAKERS, MIC],
            [stream(10, 1, name='Firefox'),
             stream(11, 1, playback=False, name='OBS'),
             stream(12, 2, playback=False, name='Discord')])
        self.assertEqual([s.name for s in acts[1].apps], ['Firefox'])
        self.assertEqual([s.name for s in acts[1].monitors], ['OBS'])
        self.assertEqual([s.name for s in acts[2].apps], ['Discord'])

    def test_plumbing_streams_are_not_apps(self):
        leg = stream(10, 1, **{'node.link-group': 'loopback-1-1'})
        ours = stream(11, 1, **{'node.name': 'pwctl.test-sound'})
        acts = endpoints.activity([SPEAKERS], [leg, ours])
        self.assertEqual(acts[1].apps, [])

    def test_a_virtual_device_sends_to_its_legs_and_is_listed_as_a_feed(self):
        vd = node(5, 'pwctl.vd-bus', **{'node.link-group': 'loopback-1-1'})
        leg = stream(20, 1, **{'node.link-group': 'loopback-1-1'})
        acts = endpoints.activity([SPEAKERS, vd], [leg])
        self.assertEqual(acts[5].sends, [SPEAKERS])
        self.assertEqual(acts[1].feeds, [vd])
        self.assertEqual((acts[5].out_stream, acts[5].out_dest), (20, 1))

    def test_a_combined_device_has_every_leg_and_no_single_picker(self):
        other = node(3, 'alsa_output.usb')
        combo = node(6, 'pwctl.vd-both', **{'node.link-group': 'combine-1'})
        legs = [stream(21, 1, **{'node.link-group': 'combine-1'}),
                stream(22, 3, **{'node.link-group': 'combine-1'})]
        acts = endpoints.activity([SPEAKERS, other, combo], legs)
        self.assertEqual(acts[6].sends, [SPEAKERS, other])
        self.assertIsNone(acts[6].out_stream)
        self.assertEqual(acts[3].feeds, [combo])

    def test_an_unlinked_leg_is_a_none_destination_that_can_be_given_one(self):
        vd = node(7, 'pwctl.vd-rec', **{'node.link-group': 'loopback-2-2'})
        leg = stream(23, None, **{'node.link-group': 'loopback-2-2'})
        acts = endpoints.activity([vd], [leg])
        self.assertEqual(acts[7].sends, [None])
        self.assertEqual(acts[7].out_stream, 23)

    def test_a_virtual_mic_sink_sends_to_its_source(self):
        sink = node(8, 'pwctl.vd-mic.in', **{'node.link-group': 'loopback-3'})
        src = node(9, 'pwctl.vd-mic', sink=False,
                   **{'node.link-group': 'loopback-3'})
        acts = endpoints.activity([sink, src], [])
        self.assertEqual(acts[8].sends, [src])
        self.assertEqual(acts[9].feeds, [sink])
        self.assertIsNone(acts[8].out_stream)


class OutputChoiceTests(TestCase):
    def test_never_itself_and_never_a_ring(self):
        a = node(5, 'pwctl.vd-a', **{'node.link-group': 'g-a'})
        b = node(6, 'pwctl.vd-b', **{'node.link-group': 'g-b'})
        legs = [stream(20, 1, **{'node.link-group': 'g-a'}),
                stream(21, 5, **{'node.link-group': 'g-b'})]   # b -> a
        nodes = [SPEAKERS, a, b]
        acts = endpoints.activity(nodes, legs)
        names = [n.name for n in endpoints.output_choices(a, nodes, acts)]
        self.assertNotIn('pwctl.vd-a', names)
        self.assertNotIn('pwctl.vd-b', names)      # b feeds a: a ring
        self.assertIn('alsa_output.pci', names)

    def test_nothing_to_choose_without_a_single_leg(self):
        acts = endpoints.activity([SPEAKERS], [])
        self.assertEqual(endpoints.output_choices(SPEAKERS, [SPEAKERS],
                                                  acts), [])


class LabelTests(TestCase):
    def test_connection_comes_from_the_node_props(self):
        self.assertEqual(endpoints.connection(SPEAKERS), 'Built-in')
        self.assertEqual(endpoints.connection(
            node(3, 'alsa_output.usb-x', **{'device.bus': 'usb'})), 'USB')
        self.assertEqual(endpoints.connection(
            node(4, 'bluez_output.x', **{'device.api': 'bluez5'})),
            'Bluetooth')
        self.assertEqual(endpoints.connection(
            node(5, 'alsa_output.pci-0.hdmi-stereo',
                 **{'device.bus': 'pci'})), 'HDMI')

    def test_subtitle_names_the_port_and_the_codec(self):
        n = node(1, 'alsa_output.pci', **{'device.api': 'alsa',
                                          'device.bus': 'pci'})
        n.ports = [(2, 'Speakers', 'yes'), (3, 'Headphones', 'no')]
        n.active_port = 2
        self.assertEqual(endpoints.subtitle(n), 'Speakers · Built-in')
        bt = node(4, 'bluez_output.x', **{'device.api': 'bluez5',
                                          'api.bluez5.codec': 'ldac'})
        self.assertEqual(endpoints.subtitle(bt), 'Bluetooth · LDAC')

    def test_software_kinds(self):
        self.assertEqual(endpoints.kind(node(5, 'pwctl.vd-x')),
                         'Virtual device')
        self.assertEqual(endpoints.kind(node(6, 'pwctl.vd-x', sink=False)),
                         'Virtual microphone')
        self.assertEqual(endpoints.kind(node(7, 'pwctl.eq.enh-1')),
                         'Equalizer')
        self.assertEqual(endpoints.kind(SPEAKERS), '')

    def test_format_while_running_and_state_otherwise(self):
        n = node(1, 'alsa_output.pci', **{'device.api': 'alsa'})
        n.state, n.fmt = 'running', {'rate': 44100, 'format': 'S24_32LE',
                                     'channels': 2}
        self.assertEqual(endpoints.format_text(n), '44.1 kHz · 24-bit · stereo')
        self.assertEqual(endpoints.state_dot(n), 'ok')
        n.state, n.fmt = 'suspended', {}
        self.assertEqual(endpoints.format_text(n), 'Suspended')
        self.assertEqual(endpoints.state_dot(n), 'dim')
        n.state, n.fmt = 'running', {'rate': 48000, 'format': 'F32P',
                                     'channels': 8}
        self.assertEqual(endpoints.format_text(n),
                         '48 kHz · 32-bit float · 8 channels')

    def test_owner_page(self):
        self.assertEqual(endpoints.owner(node(1, 'pwctl.vd-x'))[0], 'virtual')
        self.assertEqual(endpoints.owner(node(2, 'pwctl.mix.pmix-1'))[0],
                         'paths')
        rack = node(3, 'effect_input.pwctl.r1')
        self.assertEqual(endpoints.owner(rack, {'r1': 'effect-rack'})[0],
                         'effects')
        self.assertEqual(endpoints.owner(rack, {'r1': 'raw'})[0], 'chains')
        self.assertIsNone(endpoints.owner(node(4, 'effect_input.sur_x')))
        self.assertIsNone(endpoints.owner(SPEAKERS))


class CardTests(TestCase):
    def test_a_fully_off_card_is_offered_in_each_direction_it_can_give(self):
        hdmi = card(9, [(0, 'Off', 'yes'), (1, 'HDMI Output', 'yes')], 0,
                    {0: (False, False), 1: (True, False)})
        self.assertEqual(endpoints.cards_without_endpoint([hdmi], [], True),
                         [hdmi])
        self.assertEqual(endpoints.cards_without_endpoint([hdmi], [], False),
                         [])

    def test_a_card_in_use_is_offered_only_with_a_working_profile(self):
        # a USB mic whose card also lists an unavailable duplex profile
        mic_card = card(9, [(0, 'Off', 'yes'), (1, 'Input', 'yes'),
                            (2, 'Duplex', 'no')], 1,
                        {0: (False, False), 1: (False, True),
                         2: (True, True)})
        self.assertEqual(endpoints.cards_without_endpoint(
            [mic_card], [MIC], True), [])
        # a headset on its playback profile: its mic can be turned on
        headset = card(9, [(0, 'Off', 'yes'), (1, 'A2DP', 'yes'),
                           (2, 'HFP', 'yes')], 1,
                       {0: (False, False), 1: (True, False), 2: (True, True)})
        self.assertEqual(endpoints.cards_without_endpoint(
            [headset], [SPEAKERS], False), [headset])
        self.assertEqual(endpoints.cards_without_endpoint(
            [headset], [SPEAKERS], True), [])

    def test_profile_state_flags_unavailable_and_offers_the_best_fix(self):
        c = card(9, [(0, 'Off', 'yes'), (1, 'Analog', 'yes'),
                     (2, 'HDMI', 'no'), (3, 'Pro Audio', 'unknown')], 2,
                 {0: (False, False), 1: (True, True), 2: (True, False),
                  3: (True, True)})
        st = endpoints.profile_state(c, True)
        self.assertTrue(st.unavailable and st.stuck)
        self.assertEqual(st.fix, 1)
        c.active_profile = 3                    # unknown is playable
        st = endpoints.profile_state(c, True)
        self.assertFalse(st.unavailable or st.gives_nothing)


def hw(nid, name, card_id, sink=True, desc=None, api='alsa', **props):
    return node(nid, name, sink=sink, desc=desc,
                **{'device.api': api, 'device.id': card_id, **props})


def named_card(cid, name, desc, dirs, active=1):
    c = card(cid, [(i, f'P{i}', 'yes') for i in dirs], active, dirs,
             name=name)
    c.description = desc
    return c


class ListGroupTests(TestCase):
    def test_kinds_come_from_the_api_the_node_names(self):
        self.assertEqual(endpoints.device_kind(SPEAKERS), 'alsa')
        bt = node(3, 'bluez_output.x', **{'device.api': 'bluez5'})
        self.assertEqual(endpoints.device_kind(bt), 'bluez')
        self.assertEqual(endpoints.device_kind(node(4, 'pwctl.vd-x')),
                         'virtual')
        cam = node(5, 'v4l2.x', **{'device.api': 'v4l2'})
        self.assertEqual(endpoints.device_kind(cam), 'other')

    def test_a_card_with_two_rows_gets_a_heading_and_lists_outputs_first(self):
        c = named_card(9, 'alsa_card.pci', 'Built-in Audio',
                       {1: (True, True)})
        out = hw(1, 'alsa_output.pci', 9, desc='Built-in Audio Analog Stereo')
        mic = hw(2, 'alsa_input.pci', 9, sink=False,
                 desc='Built-in Audio Analog Stereo')
        groups = endpoints.list_groups([mic, out], [c])
        self.assertEqual(len(groups), 1)
        self.assertIs(groups[0].card, c)
        self.assertEqual([n.id for n in groups[0].nodes], [1, 2])
        self.assertEqual(endpoints.part_name(out, c), 'Analog Stereo')

    def test_a_card_with_one_row_is_listed_on_its_own(self):
        c = named_card(9, 'alsa_card.hdmi', 'HDMI', {1: (True, False)})
        out = hw(1, 'alsa_output.hdmi', 9, desc='LG TV')
        groups = endpoints.list_groups([out], [c])
        self.assertIsNone(groups[0].card)
        self.assertEqual(groups[0].title, 'HDMI')

    def test_an_off_direction_is_a_row_of_its_card(self):
        # Output-only profile active; the card can also record.
        c = named_card(9, 'alsa_card.pci', 'Built-in Audio',
                       {1: (True, False), 2: (True, True)}, active=1)
        out = hw(1, 'alsa_output.pci', 9)
        groups = endpoints.list_groups([out], [c])
        self.assertEqual(groups[0].off, [(c, False)])
        self.assertIs(groups[0].card, c)

    def test_a_fully_off_card_is_still_listed(self):
        c = named_card(9, 'alsa_card.usb', 'USB Mic',
                       {0: (False, False), 1: (False, True)}, active=0)
        groups = endpoints.list_groups([], [c])
        self.assertEqual([g.title for g in groups], ['USB Mic'])
        self.assertEqual(groups[0].off, [(c, False)])

    def test_order_is_kinds_then_names_and_software_outputs_first(self):
        c = named_card(9, 'alsa_card.pci', 'Zeta Card', {1: (True, False)})
        hwout = hw(1, 'alsa_output.pci', 9)
        bt = node(3, 'bluez_output.x', desc='Buds', **{'device.api': 'bluez5'})
        vmic = node(4, 'pwctl.vd-mic', sink=False, desc='A mic')
        veq = node(5, 'pwctl.eq.enh-1', desc='Z eq')
        groups = endpoints.list_groups([veq, vmic, bt, hwout], [c])
        self.assertEqual([g.title for g in groups],
                         ['Zeta Card', 'Buds', 'Z eq', 'A mic'])

    def test_row_subtitle_says_the_direction_only_on_its_own(self):
        n = node(1, 'alsa_output.usb', **{'device.api': 'alsa',
                                         'device.bus': 'usb'})
        self.assertEqual(endpoints.row_subtitle(n), 'Output · USB')
        self.assertEqual(endpoints.row_subtitle(n, in_card=True), 'USB')

    def test_facts_never_guess_while_idle(self):
        n = node(1, 'alsa_output.pci')
        n.channel_map = ['FL', 'FR']
        n.state = 'suspended'
        self.assertEqual(endpoints.facts(n), [('Rate', '—'), ('Format', '—'),
                                              ('Channels', 'Stereo')])
        n.state = 'running'
        n.fmt = {'rate': 48000, 'format': 'S24_32LE', 'channels': 2}
        self.assertEqual(endpoints.facts(n), [('Rate', '48 kHz'),
                                              ('Format', '24-bit'),
                                              ('Channels', 'Stereo')])

    def test_search_reaches_port_names(self):
        n = node(1, 'alsa_output.pci', desc='Built-in')
        n.ports = [(0, 'Speakers', 'yes'), (1, 'Headphones', 'no')]
        self.assertIn('headphones', endpoints.search_text(n))
