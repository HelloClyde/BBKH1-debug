"""Offline workflow checks; no physical device or emulator is contacted."""
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import h1_debug_foreground as check


class FakeClient:
    _broken = False

    def __init__(self, *, changed_snapshot=False, failed_download=False):
        self.events = []
        self.in_time = False
        self.switches = 0
        self.changed_snapshot = changed_snapshot
        self.failed_download = failed_download
        self.meta = dict(token=1, width=480, height=272, stride=960,
                         format=1, total=261120, source=0x82000000, tick=1)
        self.pinned = bytes(self.meta['total'])

    def capabilities(self):
        return {'features': 0x180}

    def status(self):
        self.switches += 1
        return dict(state=1, usb_fault=0, last_error=0, resident_base=0x82400000)

    def read_memory(self, address, size):
        if address == 0x80a6d6f0 + 40:
            return struct.pack('<I', 0x80a60000)
        if address == 0x80a60030:
            return bytes(5) + bytes([10]) + \
                bytes(6) + struct.pack('<I', self.switches)
        assert address == 0x83c00020 and size == 256
        return bytes([int(self.in_time)]) * 256

    def rpc(self, op):
        assert op == 6
        self.events.append('capture')
        return struct.pack('<8I', *self.meta.values())

    def tap(self, x, y):
        assert (x, y) == (105, 52)
        self.events.append('tap')
        self.in_time = True
        return {'ok': True}

    def read_screen(self, meta, *, codec, progress=None):
        self.events.append('read_' + codec)
        if self.failed_download:
            raise RuntimeError('injected read failure')
        if progress:
            for quarter in range(1, 5):
                progress(meta['total'] * quarter // 4, meta['total'])
        result = bytes([1]) * meta['total'] if codec == 'rle' and self.changed_snapshot else self.pinned
        return result, {'codec': codec, 'progress_callback': progress is not None}

    def empty(self, op, body):
        assert op == 8 and body == struct.pack('<I', 1)
        self.events.append('release_snapshot')

    def capture_screen(self, *, codec):
        self.events.append('capture_' + codec)
        return self.meta.copy(), bytes([2 if self.in_time else 0]) * self.meta['total']

    def press(self, keys, ticks):
        assert keys == ['ESCAPE'] and ticks == 8
        self.events.append('escape')
        self.in_time = False
        return {'ok': True}

    def touch_status(self):
        return {'pressed': 0}


class ForegroundWorkflow(unittest.TestCase):
    def run_check(self, client):
        with tempfile.TemporaryDirectory() as directory, patch.object(check.time, 'sleep'):
            return check.run_foreground_check(client, directory)

    def test_single_token_interleaving_and_release(self):
        client = FakeClient()
        result = self.run_check(client)
        self.assertTrue(result['ok'])
        self.assertTrue(result['same_token_raw_rle_equal'])
        self.assertEqual(client.events, ['capture', 'read_raw', 'tap', 'read_rle',
                                        'release_snapshot', 'capture_rle', 'escape', 'capture_rle'])
        self.assertGreater(result['gui_switches_during_transfer'], 0)

    def test_snapshot_mutation_fails_and_releases(self):
        client = FakeClient(changed_snapshot=True)
        with self.assertRaisesRegex(AssertionError, 'Pinned snapshot changed'):
            self.run_check(client)
        self.assertEqual(client.events.count('release_snapshot'), 1)
        self.assertNotIn('escape', client.events)

    def test_download_failure_releases_snapshot(self):
        client = FakeClient(failed_download=True)
        with self.assertRaisesRegex(RuntimeError, 'injected read failure'):
            self.run_check(client)
        self.assertEqual(client.events[-1], 'release_snapshot')


if __name__ == '__main__':
    unittest.main()
