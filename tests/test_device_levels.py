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


def state(node_id=4, touched=0.0):
    return {
        'node_id': node_id,
        'volume': Control(),
        'mute': Control(),
        'updating': False,
        'local_ts': touched,
    }


class DeviceLevelSyncTests(TestCase):
    def test_external_volume_and_mute_update_existing_controls(self):
        controls = {'headphones': state()}
        node = SimpleNamespace(name='headphones', id=4, volume=0.35,
                               muted=True)

        self.assertTrue(sync_device_levels(controls, [node], 10.0, 2.0))
        self.assertEqual(controls['headphones']['volume'].values, [0.35])
        self.assertEqual(controls['headphones']['mute'].values, [True])
        self.assertFalse(controls['headphones']['updating'])

    def test_recent_local_change_is_not_overwritten(self):
        controls = {'headphones': state(touched=9.0)}
        node = SimpleNamespace(name='headphones', id=4, volume=0.35,
                               muted=True)

        self.assertTrue(sync_device_levels(controls, [node], 10.0, 2.0))
        self.assertEqual(controls['headphones']['volume'].values, [])
        self.assertEqual(controls['headphones']['mute'].values, [])

    def test_recreated_node_requests_a_row_rebuild(self):
        controls = {'headphones': state(node_id=4)}
        node = SimpleNamespace(name='headphones', id=9, volume=0.35,
                               muted=True)

        self.assertFalse(sync_device_levels(controls, [node], 10.0, 2.0))
