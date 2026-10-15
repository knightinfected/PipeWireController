from types import SimpleNamespace
from unittest import TestCase

from pwctl.ui.device_levels import sync_device_levels


class Control:
    def __init__(self):
        self.values = []

    def set_value(self, value):
        self.values.append(value)

    def set_active(self, value):
        self.values.append(value)


def state(touched=0.0):
    stars = []
    return {
        'volume': Control(),
        'mute': Control(),
        'star': stars.append,
        'stars': stars,
        'updating': False,
        'local_ts': touched,
    }


def node(name='headphones', node_id=4, serial=40, volume=0.35, muted=True,
         default=False):
    return SimpleNamespace(name=name, id=node_id, serial=serial,
                           volume=volume, muted=muted, is_default=default)


class DeviceLevelSyncTests(TestCase):
    def test_external_volume_and_mute_update_existing_controls(self):
        controls = {(4, 40): state()}

        self.assertTrue(sync_device_levels(controls, [node()], 10.0, 2.0))
        self.assertEqual(controls[(4, 40)]['volume'].values, [0.35])
        self.assertEqual(controls[(4, 40)]['mute'].values, [True])
        self.assertFalse(controls[(4, 40)]['updating'])

    def test_recent_local_change_is_not_overwritten(self):
        controls = {(4, 40): state(touched=9.0)}

        self.assertTrue(sync_device_levels(controls, [node()], 10.0, 2.0))
        self.assertEqual(controls[(4, 40)]['volume'].values, [])
        self.assertEqual(controls[(4, 40)]['mute'].values, [])

    def test_recreated_node_requests_a_row_rebuild(self):
        controls = {(4, 40): state()}

        self.assertFalse(sync_device_levels(
            controls, [node(node_id=9, serial=90)], 10.0, 2.0))

    def test_reused_id_requests_a_row_rebuild(self):
        # Same id, new object: PipeWire hands a freed id to the next node.
        controls = {(4, 40): state()}

        self.assertFalse(sync_device_levels(
            controls, [node(serial=41)], 10.0, 2.0))

    def test_nodes_sharing_a_name_are_tracked_separately(self):
        controls = {(4, 40): state(), (5, 50): state()}
        nodes = [node(node_id=4, serial=40, volume=0.1),
                 node(node_id=5, serial=50, volume=0.2)]

        self.assertTrue(sync_device_levels(controls, nodes, 10.0, 2.0))
        self.assertEqual(controls[(4, 40)]['volume'].values, [0.1])
        self.assertEqual(controls[(5, 50)]['volume'].values, [0.2])

    def test_default_star_follows_the_graph(self):
        controls = {(4, 40): state(), (5, 50): state()}
        nodes = [node(node_id=4, serial=40, default=False),
                 node(node_id=5, serial=50, default=True)]

        self.assertTrue(sync_device_levels(controls, nodes, 10.0, 2.0))
        self.assertEqual(controls[(4, 40)]['stars'], [False])
        self.assertEqual(controls[(5, 50)]['stars'], [True])

    def test_default_star_is_held_after_a_local_click(self):
        controls = {(4, 40): state()}

        self.assertTrue(sync_device_levels(
            controls, [node(default=True)], 10.0, 2.0, default_held=True))
        self.assertEqual(controls[(4, 40)]['stars'], [])

    def test_default_star_ignores_the_rows_volume_grace(self):
        # Dragging a slider holds that row's volume, not the default.
        controls = {(4, 40): state(touched=9.0)}

        self.assertTrue(sync_device_levels(
            controls, [node(default=True)], 10.0, 2.0))
        self.assertEqual(controls[(4, 40)]['stars'], [True])
        self.assertEqual(controls[(4, 40)]['volume'].values, [])
