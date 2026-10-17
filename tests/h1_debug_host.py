"""Offline resident debugger host/protocol tests. No hardware acceptance claim."""
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import h1_debug as h


class FakeSerial:
    def __init__(self, handler=None, change=None, read_size=7):
        self.handler = handler or (lambda op, body: b"")
        self.change = change
        self.read_size = read_size
        self.pending = bytearray()
        self.writes = []
        self.closed = False

    def reset_input_buffer(self):
        self.pending.clear()

    def write(self, data):
        # A second request before a full reply would violate the device parser.
        assert not self.pending, "A request was sent before the previous reply was consumed"
        magic, version, op, seq, length, status = h.HEADER.unpack_from(data)
        assert (magic, version, status) == (h.MAGIC, 1, 0)
        assert len(data) == length + 16
        assert zlib.crc32(data[:-4]) == struct.unpack("<I", data[-4:])[0]
        self.writes.append((op, seq, data[12:-4]))
        reply = h.IDENTITY if op == 1 else self.handler(op, data[12:-4])
        status, body = reply if isinstance(reply, tuple) else (0, reply)
        packet = h.frame(op | 128, seq, body, status)
        if self.change and op != 1:
            packet = self.change(packet)
        self.pending.extend(packet)
        return len(data)

    def read(self, count):
        count = min(count, self.read_size)
        data = bytes(self.pending[:count])
        del self.pending[:count]
        return data

    def close(self):
        self.closed = True


def caps(features=0x7F):
    return struct.pack("<8I", 1, features, 512, 0x80000000, 0x4000000, 1, 0, 0)


def client(handler=None, change=None):
    transport = FakeSerial(handler, change)
    # Leave room for Windows scheduling while other MIPS tests run. The
    # dedicated timeout test below still controls its missing-reply case.
    return h.Client(transport=transport, timeout=.2), transport


class ProtocolTests(unittest.TestCase):
    def test_golden_crc_and_header(self):
        self.assertEqual(zlib.crc32(b"123456789"), 0xCBF43926)
        expected_head = bytes.fromhex("483144420103341208000000") + struct.pack("<II", 0x80000000, 512)
        self.assertEqual(h.frame(3, 0x1234, struct.pack("<II", 0x80000000, 512)),
                         expected_head + struct.pack("<I", zlib.crc32(expected_head)))

    def test_fragmented_frame_and_sequence_wrap(self):
        c, wire = client(lambda op, body: bytes(512))
        c.seq = 65535
        self.assertEqual(c.rpc(3), bytes(512))
        self.assertEqual(c.seq, 0)
        self.assertEqual([op for op, _, _ in wire.writes], [1, 3])
        self.assertFalse(wire.pending)

    def test_exact_64_response_uses_one_request(self):
        # Fixed device ZLP behavior: the host must not inject a second PING.
        c, wire = client(lambda op, body: bytes(48))
        self.assertEqual(c.rpc(33), bytes(48))
        self.assertEqual(len(wire.writes), 2)  # Identity plus one command.

    def test_crc_error_poisoned_session_never_retries(self):
        c, wire = client(change=lambda p: p[:-1] + bytes((p[-1] ^ 1,)))
        with self.assertRaisesRegex(h.ProtocolError, "CRC32"):
            c.rpc(4, bytes(8))
        with self.assertRaisesRegex(h.ProtocolError, "desynchronized"):
            c.rpc(2)
        self.assertEqual(len(wire.writes), 2)

    def test_reject_wrong_sequence_version_magic_opcode_and_oversize(self):
        def replace(offset, value):
            return lambda p: p[:offset] + value + p[offset + len(value):]
        changes = [replace(0, b"BAD!"), replace(4, b"\x02"), replace(5, b"\x84"),
                   replace(6, b"\xFF\xFF"), replace(8, struct.pack("<H", 513))]
        for change in changes:
            with self.subTest(change=change):
                c, _ = client(change=change)
                with self.assertRaisesRegex(h.ProtocolError, "header"):
                    c.rpc(2)

    def test_device_error_fully_drained_does_not_poison(self):
        c, wire = client(lambda op, body: (7, b"denied") if op == 4 else b"fine")
        with self.assertRaises(h.DeviceError) as caught:
            c.rpc(4)
        self.assertEqual((caught.exception.op, caught.exception.status, caught.exception.payload), (4, 7, b"denied"))
        self.assertFalse(wire.pending)
        self.assertEqual(c.rpc(2), b"fine")

    def test_timeout_and_short_write(self):
        c, wire = client(change=lambda p: p[:-3])
        with self.assertRaises(TimeoutError):
            c.rpc(4)
        self.assertTrue(c._broken)
        self.assertEqual(len(wire.writes), 2)
        c, wire = client()
        wire.write = lambda data: len(data) - 1
        with self.assertRaisesRegex(h.ProtocolError, "Incomplete USB write"):
            c.rpc(4)
        self.assertTrue(c._broken)

    def test_reject_other_bda_identity_and_close(self):
        wire = FakeSerial()
        original = wire.write
        def wrong_identity(data):
            result = original(data)
            wire.pending = bytearray(h.frame(129, 1, b"H1DBG/1 V1.41 USB-CDC RXONLY"))
            return result
        wire.write = wrong_identity
        with self.assertRaisesRegex(h.ProtocolError, "identity"):
            h.Client(transport=wire)
        self.assertTrue(wire.closed)

    def test_payload_bounds_precede_io(self):
        c, wire = client()
        for op, payload in ((128, b""), (0, b""), (3, bytes(513))):
            with self.assertRaises(ValueError):
                c.rpc(op, payload)
        self.assertEqual(len(wire.writes), 1)

    def test_only_matching_enumerated_port_can_open(self):
        found = type("Port", (), {"device": "COM4", "vid": h.VID, "pid": h.PID})()
        fake_module = mock.Mock()
        fake_module.Serial.return_value = FakeSerial()
        with mock.patch.object(h, "ports", return_value=[found]), mock.patch.object(h, "serial_module", return_value=(fake_module, None)):
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                h.Client("COM1")
            fake_module.Serial.assert_not_called()
            c = h.Client("com4")
            self.assertEqual(c.port, "COM4")
            c.close()


class MemoryKeyTests(unittest.TestCase):
    def test_read_and_write_chunking(self):
        memory = bytes(i % 251 for i in range(1300))
        written = bytearray()
        def handler(op, body):
            if op == 9: return caps()
            if op == 3:
                address, count = struct.unpack("<II", body)
                offset = address - 0x81000000
                return memory[offset:offset + count]
            if op == 4:
                self.assertEqual(struct.unpack_from("<I", body)[0], 0x81000000 + len(written))
                written.extend(body[4:])
                return b""
            self.fail(op)
        c, wire = client(handler)
        self.assertEqual(c.read_memory(0x81000000, 1300), memory)
        self.assertEqual([struct.unpack("<II", b)[1] for op, _, b in wire.writes if op == 3], [512, 512, 276])
        result = c.write_memory(0x81000000, memory)
        self.assertEqual(written, memory)
        self.assertEqual([len(b) - 4 for op, _, b in wire.writes if op == 4], [508, 508, 284])
        self.assertEqual(result["bytes_written"], 1300)
        self.assertEqual(sum(op == 9 for op, _, _ in wire.writes), 1)

    def test_ram_boundaries_and_no_uncached_alias(self):
        c, wire = client(lambda op, body: caps())
        c.capabilities()
        self.assertEqual(c.memory_range(0x83FFFFFF, 1), (0x83FFFFFF, 1))
        for address, count in ((0x83FFFFFF, 2), (0x7FFFFFFF, 1), (0xA0000000, 4),
                               (0xFFFFFFFF, 2), (0x80000000, 0), (0x80000000, -1)):
            with self.subTest(address=address, count=count), self.assertRaises(ValueError):
                c.read_memory(address, count)
        with self.assertRaises(ValueError):
            c.write_memory(0x80000000, b"")
        self.assertEqual([op for op, _, _ in wire.writes], [1, 9])

    def test_read_length_mismatch(self):
        c, _ = client(lambda op, body: caps() if op == 9 else b"x")
        with self.assertRaisesRegex(h.ProtocolError, "read length"):
            c.read_memory(0x81000000, 2)

    def test_physical_keys_and_finite_lease(self):
        self.assertEqual(h.key_masks([1, 32, 33, 42, 42]), (0x80000001, 0x201))
        c, wire = client()
        c.press([1, 32, 33, 42], 4000)
        self.assertEqual(wire.writes[-1][2], struct.pack("<III", 0x80000001, 0x201, 4000))
        c.release()
        self.assertEqual(wire.writes[-1][2], bytes(12))
        for keys, ticks in (([0], 1), ([43], 1), ([1], 0), ([1], 4001), ([], 1)):
            with self.assertRaises(ValueError):
                c.press(keys, ticks)
        with self.assertRaises(ValueError):
            c.keys(0, 1024, 8)
        self.assertEqual(len(wire.writes), 3)

    def test_named_physical_keys(self):
        self.assertEqual(h.key_number("up"), 37)
        self.assertEqual(h.key_number("H1_KEY_PAGE_DOWN"), 22)
        self.assertEqual(h.key_number("page-up"), 15)
        self.assertEqual(h.key_number("0x2a"), 42)
        self.assertEqual(h.key_masks(["q", "K", "L", "SHIFT"]), (0x80000001, 0x201))
        self.assertEqual(h.key_masks("UP"), (0, 16))
        self.assertEqual(h.parser().parse_args(["press", "UP", "CONFIRM"]).keys, [37, 39])
        for name in ("FN", "POWER", "unknown"):
            with self.assertRaises(ValueError):
                h.key_number(name)


class TouchTests(unittest.TestCase):
    def make_client(self, failure=None):
        contact = [0, 0, 0, 0]
        calls = []
        def handler(op, body):
            if op == 9: return caps(0xFF)
            if op == 12: return struct.pack("<4I", *contact)
            if op == 11:
                values = struct.unpack("<4I", body)
                calls.append(values)
                if failure:
                    status = failure(values, len(calls))
                    if status: return status, b""
                contact[:] = values
                return b""
            self.fail(op)
        c, wire = client(handler)
        return c, wire, contact, calls

    def test_down_move_up_and_independent_status(self):
        c, wire, contact, calls = self.make_client()
        c.touch(40, 132)
        self.assertEqual(c.touch_status(), {"x": 40, "y": 132, "pressed": 1, "lease_remaining": 80})
        c.touch(479, 271, lease_ticks=4000)
        c.release_touch()
        self.assertEqual(calls, [(40, 132, 1, 80), (479, 271, 1, 4000), (479, 271, 0, 0)])
        self.assertEqual(contact, [479, 271, 0, 0])
        self.assertEqual(sum(op == 9 for op, _, _ in wire.writes), 1)

    def test_new_session_release_uses_reported_coordinates(self):
        c, _, contact, calls = self.make_client()
        contact[:] = [175, 90, 1, 40]
        c.release_touch()
        self.assertEqual(calls, [(175, 90, 0, 0)])

    def test_touch_rejects_bounds_before_io(self):
        c, wire, _, _ = self.make_client()
        for args in ((-1, 0), (480, 0), (0, -1), (0, 272)):
            with self.assertRaises(ValueError): c.touch(*args)
        for kwargs in ({"pressed": 2}, {"lease_ticks": 0}, {"lease_ticks": 4001}):
            with self.assertRaises(ValueError): c.touch(0, 0, **kwargs)
        with self.assertRaises(ValueError): c.tap(0, 0, hold_seconds=0)
        with self.assertRaises(ValueError): c.swipe(0, 0, 1, 1, steps=121)
        with self.assertRaises(ValueError): c.swipe(0, 0, 1, 1, duration_seconds=float("nan"))
        self.assertEqual(len(wire.writes), 1)

    def test_old_build_rejected_without_touch_mutation(self):
        c, wire = client(lambda op, body: caps())
        with self.assertRaisesRegex(RuntimeError, "no touch support"):
            c.touch(20, 30)
        self.assertEqual([op for op, _, _ in wire.writes], [1, 9])

    def test_touch_status_bounds(self):
        for state in ((480, 0, 0, 0), (0, 272, 0, 0), (0, 0, 2, 0),
                      (0, 0, 0, 1), (0, 0, 1, 4001)):
            c, _ = client(lambda op, body: caps(0xFF) if op == 9 else struct.pack("<4I", *state))
            with self.assertRaises(h.ProtocolError): c.touch_status()

    def test_tap_releases_after_success_queue_failure_and_interrupt(self):
        c, _, _, calls = self.make_client()
        with mock.patch.object(h.time, "sleep"):
            result = c.tap(80, 50)
        self.assertTrue(result["release_acknowledged"])
        self.assertEqual(calls, [(80, 50, 1, 80), (80, 50, 0, 0)])
        c, _, _, calls = self.make_client(lambda values, count: 2 if count == 1 else 0)
        with self.assertRaises(h.DeviceError): c.tap(80, 50)
        self.assertEqual(calls, [(80, 50, 1, 80), (80, 50, 0, 0)])
        c, _, _, calls = self.make_client()
        with mock.patch.object(h.time, "sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            c.tap(80, 50)
        self.assertEqual(calls[-1], (80, 50, 0, 0))

    def test_failed_cleanup_is_recorded_without_retry(self):
        c, _, _, calls = self.make_client(lambda values, count: 2)
        with self.assertRaises(h.DeviceError) as caught:
            c.tap(80, 50)
        self.assertTrue(hasattr(caught.exception, "touch_release_error"))
        self.assertEqual(len(calls), 2)

    def test_long_tap_renews_finite_lease_without_moving(self):
        c, _, _, calls = self.make_client()
        now = [0.0]
        with mock.patch.object(h.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(h.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            c.tap(90, 80, hold_seconds=1)
        self.assertEqual(calls, [(90, 80, 1, 80)] * 4 + [(90, 80, 0, 0)])
        self.assertEqual(now[0], 1)

    def test_poisoned_touch_session_does_not_send_up_on_bad_wire(self):
        c, wire, _, _ = self.make_client()
        c.capabilities()
        wire.change = lambda data: data[:-1] + bytes((data[-1] ^ 1,))
        with self.assertRaises(h.ProtocolError): c.tap(80, 50)
        self.assertEqual([op for op, _, _ in wire.writes], [1, 9, 11])

    def test_swipe_linear_points_and_final_up(self):
        c, _, _, calls = self.make_client()
        now = [0.0]
        with mock.patch.object(h.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(h.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            result = c.swipe(0, 0, 400, 200, duration_seconds=.4, steps=4)
        self.assertEqual(result["points_sent"], [[0, 0], [100, 50], [200, 100], [300, 150], [400, 200]])
        self.assertEqual(calls[-1], (400, 200, 0, 0))
        self.assertAlmostEqual(result["host_elapsed_seconds"], .4)

    def test_sparse_swipe_renews_contact_between_waypoints(self):
        c, _, _, calls = self.make_client()
        now = [0.0]
        with mock.patch.object(h.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(h.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            result = c.swipe(0, 0, 400, 200, duration_seconds=1, steps=2)
        self.assertEqual(calls, [(0, 0, 1, 80)] * 2 + [(200, 100, 1, 80)] * 2 +
                         [(400, 200, 1, 80), (400, 200, 0, 0)])
        self.assertEqual(result["points_sent"], [[0, 0], [200, 100], [400, 200]])
        self.assertEqual(now[0], 1)

    def test_sparse_swipe_renewal_failure_still_releases(self):
        c, _, _, calls = self.make_client(lambda values, count: 2 if count == 2 else 0)
        now = [0.0]
        with mock.patch.object(h.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(h.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)), \
             self.assertRaises(h.DeviceError):
            c.swipe(0, 0, 400, 200, duration_seconds=1, steps=2)
        self.assertEqual(calls, [(0, 0, 1, 80)] * 2 + [(0, 0, 0, 0)])

    def test_slow_swipe_skips_overdue_points_but_keeps_endpoint(self):
        now = [0.0]
        def slow(values, count): now[0] += .25
        c, _, _, calls = self.make_client(slow)
        c.timeout = 4.0
        with mock.patch.object(h.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(h.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            result = c.swipe(400, 200, 0, 0, duration_seconds=.5, steps=12)
        self.assertLess(len(result["points_sent"]), 13)
        self.assertEqual(result["points_sent"][0], [400, 200])
        self.assertEqual(result["points_sent"][-1], [0, 0])
        self.assertEqual(calls[-1], (0, 0, 0, 0))

    def test_swipe_queue_failure_still_sends_up(self):
        c, _, _, calls = self.make_client(lambda values, count: 2 if count == 3 else 0)
        now = [0.0]
        with mock.patch.object(h.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(h.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)), \
             self.assertRaises(h.DeviceError):
            c.swipe(0, 0, 400, 200, duration_seconds=.4, steps=4)
        self.assertEqual(calls, [(0, 0, 1, 80), (100, 50, 1, 80),
                                 (200, 100, 1, 80), (200, 100, 0, 0)])

    def test_touch_cli(self):
        self.assertEqual(h.parser().parse_args(["touch", "40", "132"]).ticks, 80)
        self.assertEqual(h.parser().parse_args(["tap", "40", "132", "--seconds", ".2"]).seconds, .2)
        self.assertEqual(h.parser().parse_args(["swipe", "20", "200", "450", "200"]).steps, 12)
        self.assertEqual(h.parser().parse_args(["release-touch"]).command, "release-touch")


class ScreenTests(unittest.TestCase):
    def test_rgb565_primary_colors_with_stride_padding(self):
        meta = dict(zip(h.SCREEN_FIELDS, [1, 3, 2, 8, 1, 16, 0x100000, 88]))
        data = struct.pack("<8H", 0xF800, 0x07E0, 0x001F, 0xBAD0, 0xFFFF, 0, 0x8410, 0xBAD0)
        self.assertEqual(h.decode_screen(meta, data),
                         bytes((255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255, 0, 0, 0, 132, 130, 132)))

    def test_bgrx8888_with_padding(self):
        meta = dict(zip(h.SCREEN_FIELDS, [2, 2, 1, 12, 2, 12, 0, 0]))
        self.assertEqual(h.decode_screen(meta, bytes((3, 2, 1, 255, 6, 5, 4, 0, 99, 99, 99, 99))), bytes((1, 2, 3, 4, 5, 6)))

    def test_bad_metadata_and_raw_length(self):
        good = dict(zip(h.SCREEN_FIELDS, [1, 1, 1, 2, 1, 2, 0, 0]))
        for key, value in (("width", 0), ("height", 4097), ("stride", 1), ("format", 3), ("total", 0), ("token", 0)):
            with self.subTest(key=key), self.assertRaises(h.ProtocolError):
                h.decode_screen({**good, key: value}, b"\0\0")
        with self.assertRaises(h.ProtocolError):
            h.decode_screen(good, b"")

    def test_capture_chunking_token_and_release(self):
        data = bytes(i % 251 for i in range(1200))
        def handler(op, body):
            if op == 9: return caps()
            if op == 6: return struct.pack("<8I", 7, 300, 2, 600, 1, 1200, 0x100000, 42)
            if op == 7:
                token, offset, count = struct.unpack("<III", body)
                return struct.pack("<III", token, offset, len(data)) + data[offset:offset + count]
            if op == 8:
                self.assertEqual(body, struct.pack("<I", 7))
                return b""
            self.fail(op)
        c, wire = client(handler)
        meta, got = c.capture_screen()
        self.assertEqual(got, data)
        self.assertEqual(meta["token"], 7)
        self.assertEqual([struct.unpack("<III", b)[2] for op, _, b in wire.writes if op == 7], [500, 500, 200])
        self.assertEqual(wire.writes[-1][0], 8)

    def test_bad_chunk_token_still_releases_snapshot(self):
        def handler(op, body):
            if op == 9: return caps()
            if op == 6: return struct.pack("<8I", 7, 1, 1, 2, 1, 2, 0, 0)
            if op == 7: return struct.pack("<III", 8, 0, 2) + b"\0\0"
            return b""
        c, wire = client(handler)
        with self.assertRaisesRegex(h.ProtocolError, "Screenshot chunk"):
            c.capture_screen()
        self.assertEqual(wire.writes[-1][0], 8)


class RleScreenTests(unittest.TestCase):
    @staticmethod
    def encode_prefix(data, bpp, wanted):
        # Independent fixture encoder; singleton literals deliberately exercise
        # valid but inefficient inputs, and packets may end before wanted.
        encoded, consumed = bytearray(), 0
        while consumed < min(len(data), wanted) and len(encoded) + bpp + 1 <= 496:
            pixel = data[consumed:consumed + bpp]
            count = 1
            while (count < 129 and consumed + (count + 1) * bpp <= min(len(data), wanted)
                   and data[consumed + count * bpp:consumed + (count + 1) * bpp] == pixel):
                count += 1
            encoded.append(128 + count - 2 if count > 1 else 0)
            encoded.extend(pixel)
            consumed += count * bpp
        return consumed, bytes(encoded)

    def make_client(self, data, bpp=4, transform=None, features=0x1FF):
        pixels = len(data) // bpp
        height = 1 if pixels <= 4096 else 2
        width = pixels // height
        self.assertEqual(width * height * bpp, len(data))
        def handler(op, body):
            if op == 9: return caps(features)
            if op == 6: return struct.pack("<8I", 7, width, height, width * bpp, 1 if bpp == 2 else 2, len(data), 0, 42)
            if op in (7, 13):
                token, offset, wanted = struct.unpack("<III", body)
                if op == 7: return struct.pack("<III", token, offset, len(data)) + data[offset:offset + wanted]
                self.assertEqual((offset % bpp, wanted % bpp), (0, 0))
                self.assertLessEqual(wanted, 16384)
                count, encoded = self.encode_prefix(data[offset:], bpp, wanted)
                block = struct.pack("<4I", token, offset, len(data), count) + encoded
                return transform(block) if transform else block
            if op == 8:
                self.assertEqual(body, struct.pack("<I", 7))
                return b""
            self.fail(op)
        return client(handler)

    def test_literal_repeat_boundaries_preserve_x_bytes(self):
        unique = b"\x01\x02\x03\x04\x01\x02\x03\xff"
        self.assertEqual(h.decode_screen_rle(b"\x01" + unique, 4, 8), unique)
        self.assertEqual(h.decode_screen_rle(b"\xff\x34\x12", 2, 258), b"\x34\x12" * 129)
        literal = bytes(range(256))
        self.assertEqual(h.decode_screen_rle(b"\x7f" + literal, 2, 256), literal)
        mixed = b"\x80abcd\x00wxyz\xffABCD"
        self.assertEqual(h.decode_screen_rle(mixed, 4, 528), b"abcd" * 2 + b"wxyz" + b"ABCD" * 129)

    def test_decoder_rejects_truncation_overrun_trailing_and_misalignment(self):
        cases = [(b"", 4, 4), (b"\x00abc", 4, 4), (b"\x80abc", 4, 8),
                 (b"\x01abcd", 4, 8), (b"\x80abcd", 4, 4),
                 (b"\x00abcd", 4, 8), (b"\x00abcd\x00", 4, 4),
                 (b"\x00abcd", 4, 3), (b"\x00abcd", 4, 0),
                 (b"\x00abcd", 4, 16388), (b"\0" * 497, 4, 400),
                 (b"\x00abc", 3, 3)]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(h.ProtocolError):
                h.decode_screen_rle(*args)

    def test_auto_negotiates_rle_and_handles_maximum_decoded_request(self):
        data = b"ab\x00\xff" * 6000
        c, wire = self.make_client(data)
        meta, got = c.capture_screen()
        self.assertEqual(got, data)
        self.assertEqual(meta["codec"], "rle")
        requests = [struct.unpack("<III", b) for op, _, b in wire.writes if op == 13]
        self.assertEqual(requests, [(7, 0, 16384), (7, 16384, 7616)])
        stats = meta["transfer"]
        self.assertEqual(stats["decoded_bytes"], len(data))
        self.assertEqual(stats["requests"], 2)
        self.assertEqual(stats["protocol_bytes_sent"], 56)
        self.assertEqual(stats["protocol_bytes_received"], stats["encoded_bytes"] + 2 * 32)
        self.assertEqual(wire.writes[-1][0], 8)

    def test_partial_prefix_and_raw_auto_compatibility(self):
        data = bytes(i % 251 for i in range(2400))
        for bpp in (2, 4):
            c, wire = self.make_client(data, bpp)
            meta, got = c.capture_screen()
            self.assertEqual(got, data)
            self.assertGreater(meta["transfer"]["requests"], 1)
            self.assertEqual(wire.writes[-1][0], 8)
        for mode, features in (("auto", 0xFF), ("raw", 0x1FF)):
            c, wire = self.make_client(data, features=features)
            meta, got = c.capture_screen(codec=mode)
            self.assertEqual((meta["codec"], got), ("raw", data))
            self.assertNotIn(13, [op for op, _, _ in wire.writes])
            self.assertEqual(meta["transfer"]["encoded_bytes"], len(data))

    def test_invalid_codec_and_unsupported_rle_precede_capture(self):
        c, wire = self.make_client(b"abcd", features=0xFF)
        with self.assertRaises(ValueError): c.capture_screen(codec="jpeg")
        with self.assertRaisesRegex(RuntimeError, "no lossless"): c.capture_screen(codec="rle")
        self.assertEqual([op for op, _, _ in wire.writes], [1, 9])
        self.assertTrue(h.parser().parse_args(["screenshot", "screen.png", "--raw"]).raw)

    def test_progress_callback_runs_between_complete_rpcs(self):
        data = bytes(i % 251 for i in range(1200))
        c, wire = self.make_client(data)
        meta = h.words(c.rpc(6), h.SCREEN_FIELDS, "SCREEN_CAPTURE")
        seen = []
        def progress(done, total):
            seen.append((done, total))
            self.assertEqual(c.rpc(1), h.IDENTITY)
        got, stats = c.read_screen(meta, codec="rle", progress=progress)
        self.assertEqual(got, data)
        self.assertTrue(stats["progress_callback"])
        self.assertEqual(len(seen), stats["requests"])
        self.assertEqual(seen[-1], (1200, 1200))
        self.assertTrue(all(a[0] < b[0] for a, b in zip(seen, seen[1:])))
        self.assertEqual([op for op, _, _ in wire.writes][-2:], [13, 1])
        c.empty(8, struct.pack("<I", meta["token"]))

    def test_bad_rle_reply_releases_snapshot_without_fallback(self):
        mutations = [lambda b: struct.pack("<I", 8) + b[4:],
                     lambda b: b[:4] + struct.pack("<I", 4) + b[8:],
                     lambda b: b[:8] + struct.pack("<I", 8) + b[12:],
                     lambda b: b[:12] + struct.pack("<I", 0) + b[16:],
                     lambda b: b[:12] + struct.pack("<I", 3) + b[16:],
                     lambda b: b[:12] + struct.pack("<I", 8) + b[16:],
                     lambda b: b[:16], lambda b: b[:-1], lambda b: b + b"\x00"]
        for mutate in mutations:
            c, wire = self.make_client(b"abcd", transform=mutate)
            with self.subTest(mutation=mutate), self.assertRaises(h.ProtocolError): c.capture_screen()
            self.assertEqual(wire.writes[-1][0], 8)
            self.assertNotIn(7, [op for op, _, _ in wire.writes])

    def test_same_snapshot_benchmark_preserves_png_dimensions_and_releases(self):
        import h1_debug_screen_benchmark as bench
        from PIL import Image
        data = bytes((3, 2, 1, 255)) * 600
        c, wire = self.make_client(data)
        with tempfile.TemporaryDirectory() as directory:
            result = bench.compare_snapshot(c, directory)
            self.assertTrue(result["byte_identical"])
            self.assertEqual(result["raw_sha256"], result["rle_decoded_sha256"])
            self.assertEqual((Path(directory) / "screen.raw").read_bytes(), data)
            self.assertEqual((Path(directory) / "screen.rle-decoded.raw").read_bytes(), data)
            with Image.open(Path(directory) / "screen.png") as image:
                self.assertEqual(image.size, (600, 1))
        self.assertEqual(sum(op == 6 for op, _, _ in wire.writes), 1)
        self.assertEqual(sum(op == 8 for op, _, _ in wire.writes), 1)
        self.assertEqual(wire.writes[-1][0], 8)

    def test_same_snapshot_benchmark_mismatch_is_reported_and_released(self):
        import h1_debug_screen_benchmark as bench
        c, wire = self.make_client(b"abcd", transform=lambda b: b[:17] + b"wxyz")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(h.ProtocolError, "bytes differ"):
                bench.compare_snapshot(c, directory)
            self.assertEqual((Path(directory) / "screen.rle-decoded.raw").read_bytes(), b"wxyz")
        self.assertEqual(wire.writes[-1][0], 8)


class NativeTests(unittest.TestCase):
    @staticmethod
    def info(state=2, image_size=16, memory_size=32, crc=0, written=16):
        return struct.pack("<16I", 1, 0x81000020, 65536, 16384, 32768, 4096,
                           state, image_size, memory_size, 0, crc, written, 0, 0, 0, 28)

    def test_heap_resident_module_base_allowed(self):
        c, _ = client(lambda op, body: self.info())
        self.assertEqual(c.exec_info()["base"], 0x81000020)

    def test_upload_commit_crc_and_chunking(self):
        data = bytes(i % 251 for i in range(1056))
        metadata = {"abi": 1, "base": 0x81000020, "image_size": len(data),
                    "memory_size": 1104, "entry_offset": 0, "crc32": zlib.crc32(data), "sha256": h.sha(data)}
        uploaded = bytearray()
        def handler(op, body):
            if op == 26: return self.info(2, len(data), 1104, metadata["crc32"], len(data))
            if op == 27:
                self.assertEqual(struct.unpack("<5I", body), (len(data), 1104, 0, metadata["crc32"], 1))
            elif op == 28:
                self.assertEqual(struct.unpack_from("<I", body)[0], len(uploaded))
                uploaded.extend(body[4:])
            elif op == 29: self.assertEqual(uploaded, data)
            else: self.fail(op)
            return b""
        c, wire = client(handler)
        c.upload(metadata, data)
        self.assertEqual([len(b) - 4 for op, _, b in wire.writes if op == 28], [508, 508, 40])

    def test_bad_upload_package_precedes_mutation(self):
        c, wire = client(lambda op, body: self.info())
        metadata = {"abi": 1, "base": 0x81000020, "image_size": 16, "memory_size": 32,
                    "entry_offset": 0, "crc32": zlib.crc32(bytes(16)), "sha256": "bad"}
        with self.assertRaises(ValueError):
            c.upload(metadata, bytes(16))
        self.assertEqual([op for op, _, _ in wire.writes], [1, 26])

    def test_run_output_signed_return_and_tick_wrap(self):
        result_words = [5, 16, 32, 0, 0, 16, 1, 0, 0xFFFFFFFA, 5, 80, 3, 2, 600, -7]
        output = bytes(i % 251 for i in range(600))
        def handler(op, body):
            if op == 30:
                self.assertEqual(body, struct.pack("<II", 3, 80) + b"ab")
                return b""
            if op == 31: return struct.pack("<14Ii", *result_words)
            if op == 33:
                offset, count = struct.unpack("<IH", body)
                return struct.pack("<II", offset, 600) + output[offset:offset + count]
            self.fail(op)
        c, wire = client(handler)
        result, got = c.run_module(3, b"ab")
        self.assertEqual(result["return_value"], -7)
        self.assertEqual(result["elapsed_ticks"], 11)
        self.assertEqual(got, output)
        self.assertTrue(result["host_elapsed_includes_usb_and_polling"])
        self.assertEqual([struct.unpack("<IH", b)[1] for op, _, b in wire.writes if op == 33], [504, 96])

    def test_no_false_output_before_completion(self):
        c, wire = client()
        with self.assertRaises(RuntimeError):
            c.exec_output({"state": 4, "output_size": 1})
        self.assertEqual(len(wire.writes), 1)

    def test_run_arguments_precede_mutation(self):
        c, wire = client()
        for args in ({"budget_ticks": 0}, {"budget_ticks": 401}, {"args": bytes(257)},
                     {"opcode": -1}, {"wait_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                c.run_module(**args)
        self.assertEqual(len(wire.writes), 1)


class FileDesktopDevice:
    """Transaction fixture; replies still pass through the real framing client."""
    def __init__(self):
        self.files = {r"A:\应用\程序\测试.bda": b"old"}
        self.state = dict.fromkeys(h.FILE_STATUS_FIELDS, 0)
        self.state["version"] = 1
        self.paths = ["", "", ""]
        self.data = bytearray()
        self.commit_states = []
        self.shell = dict(zip(h.DESKTOP_FIELDS, (0, 0, 0, 0, 1, 0xFFFFFFFF, 7, 0)))
        self.shell_states = []
        self.shell_finish = 3
        self.override = None

    def __call__(self, op, body):
        if self.override:
            reply = self.override(op, body)
            if reply is not None:
                return reply
        if op == 9:
            return caps(0x7FF)
        if op == 40:
            if self.commit_states:
                self.state["state"] = self.commit_states.pop(0)
            return struct.pack("<16I", *(self.state[k] for k in h.FILE_STATUS_FIELDS))
        if op == 41:
            size, crc = struct.unpack_from("<II", body)
            target = body[8:].decode("gbk")
            self.state.update(token=self.state["token"] + 1, state=1, size=size, received=0,
                              expected_crc=crc, actual_crc=0, flags=2, error=0)
            self.paths = [target, r"A:\~H100000001.tmp", r"A:\~H100000001.bak" if target in self.files else ""]
            self.data = bytearray()
            return struct.pack("<II", self.state["token"], 0)
        if op == 42:
            token, offset = struct.unpack_from("<II", body)
            assert token == self.state["token"] and offset == len(self.data)
            assert 1 <= len(body[8:]) <= 504
            self.data.extend(body[8:])
            self.state["received"] = len(self.data)
            return struct.pack("<I", len(self.data))
        if op == 43:
            assert struct.unpack("<I", body)[0] == self.state["token"]
            self.files[self.paths[0]] = bytes(self.data)
            self.state.update(actual_crc=zlib.crc32(self.data), flags=8 | (4 if self.paths[2] else 0))
            self.commit_states = [2, 3, 4, 5]
            return b""
        if op == 44:
            self.state["state"] = 7
            self.commit_states = []
            return b""
        if op == 45:
            path = body.decode("gbk")
            self.data = bytearray(self.files[path])
            self.state.update(token=self.state["token"] + 1, size=len(self.data), received=0, state=10)
            self.paths = [path, "", ""]
            return struct.pack("<II", self.state["token"], len(self.data))
        if op == 46:
            token, offset, wanted = struct.unpack("<III", body)
            assert token == self.state["token"] and 1 <= wanted <= 512
            return bytes(self.data[offset:offset + wanted])
        if op == 47:
            self.state["state"] = 5
            return b""
        if op == 48:
            path = body.decode("gbk")
            return struct.pack("<III", path in self.files, len(self.files.get(path, b"")), 0)
        if op == 49:
            token, kind = struct.unpack("<II", body)
            assert token == self.state["token"]
            return self.paths[kind].encode("gbk")
        if op == 52:
            if self.shell_states:
                state = self.shell_states.pop(0)
                self.shell.update(state=state, menu_idle=int(state == 4))
                if self.shell["kind"] == 2 and state in (3, 4):
                    self.shell["entry_count"] += 1
            return struct.pack("<8I", *(self.shell[k] for k in h.DESKTOP_FIELDS))
        if op in (50, 51):
            assert self.shell["menu_idle"]
            self.shell.update(job_id=self.shell["job_id"] + 1, kind=1 if op == 50 else 2,
                              state=1, menu_idle=0)
            self.shell_states = [1, 2, 4 if op == 50 else self.shell_finish]
            return struct.pack("<I", self.shell["job_id"])
        raise AssertionError(op)


def bda_fixture(path):
    sys.path.insert(0, str(ROOT / "vendor/h1-sdk"))
    from h1_bda.header import HeaderFields, encode_header
    resource = struct.pack("<6H", 1, 1, 16, 1, 1, 1) + b"\x00\xf8"
    payload = b"\x08\x00\xe0\x03"  # jr ra; envelope fixture, never executed.
    data = encode_header(HeaderFields(0x47, 192, 192, 136, (14,) * 4), title="测试") + resource * 4 + payload
    path.write_bytes(data)
    return data


class FileTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "source.bin"
        self.target = r"A:\应用\程序\测试.bda"
        self.device = FileDesktopDevice()
        self.client, self.wire = client(self.device)

    def operations(self):
        return [op for op, _, _ in self.wire.writes]

    def test_gbk_paths_dbcs_trail_backslash_and_byte_limit(self):
        char = bytes((0x81, 0x5C)).decode("gbk")
        path, raw = h.device_path("a:/应用/程序/" + char + ".BDA", desktop=True)
        self.assertEqual(raw.decode("gbk"), path)
        self.assertIn(b"\x81\x5c", raw)
        self.assertEqual(h.device_path("A:\\" + "汉" * 118 + "x")[1].__len__(), 240)
        with self.assertRaisesRegex(ValueError, "240"):
            h.device_path("A:\\" + "汉" * 119)
        with self.assertRaisesRegex(ValueError, "temporary"):
            h.device_path("A:\\" + "x" * 223 + "\\file", for_put=True)

    def test_invalid_paths_and_desktop_location(self):
        for path in (r"F:\应用\程序\a.bda", "a.bda", r"A:\a\..\b", "A:\\a.\\b", "A:\\a ",
                     "A:\\a\0x", "A:\\a\\\\b", "A:\\emoji😀", "A:\\a:b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                h.device_path(path)
        for path in (r"B:\应用\程序\a.bda", r"A:\应用\程序\sub\a.bda", r"A:\a.bda", r"A:\应用\程序\.bda"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                h.device_path(path, desktop=True)

    def test_put_streaming_zero_and_chunk_boundaries(self):
        for size in (0, 1, 504, 505, 1513):
            with self.subTest(size=size), mock.patch.object(h.time, "sleep"):
                data = bytes(i % 251 for i in range(size))
                self.source.write_bytes(data)
                start = len(self.wire.writes)
                receipt = self.client.file_put(self.source, self.target, receipt_path=self.directory / "put.json")
                self.assertTrue(receipt["completed"] and receipt["device_crc_verified"])
                self.assertEqual(self.device.files[self.target], data)
                self.assertEqual(receipt["sha256"], h.sha(data))
                chunks = [len(body) - 8 for op, _, body in self.wire.writes[start:] if op == 42]
                self.assertEqual(sum(chunks), size)
                self.assertTrue(all(1 <= length <= 504 for length in chunks))
                self.assertEqual(json.loads((self.directory / "put.json").read_text(encoding="utf-8"))["token"], receipt["token"])

    def test_bad_write_ack_aborts_once_without_commit(self):
        self.source.write_bytes(bytes(600))
        self.device.override = lambda op, body: struct.pack("<I", 503) if op == 42 else None
        with self.assertRaisesRegex(h.ProtocolError, "offset"):
            self.client.file_put(self.source, self.target)
        self.assertEqual(self.operations().count(42), 1)
        self.assertEqual(self.operations().count(44), 1)
        self.assertNotIn(43, self.operations())

    def test_changed_local_source_never_commits(self):
        self.source.write_bytes(bytes(600))
        original = h.stream_digest
        def changed(stream):
            result = original(stream)
            self.source.write_bytes(b"x" * 600)
            return result
        with mock.patch.object(h, "stream_digest", changed), self.assertRaisesRegex(RuntimeError, "changed"):
            self.client.file_put(self.source, self.target)
        self.assertNotIn(43, self.operations())
        self.assertEqual(self.operations().count(44), 1)

    def test_lost_commit_ack_is_unknown_no_retries_or_abort(self):
        self.source.write_bytes(b"abc")
        self.wire.change = lambda packet: b"" if packet[5] == (43 | 128) else packet
        with self.assertRaises(TimeoutError) as caught:
            self.client.file_put(self.source, self.target)
        self.assertEqual(self.operations().count(43), 1)
        self.assertNotIn(44, self.operations())
        self.assertTrue(caught.exception.file_transfer["outcome_unknown"])
        self.assertEqual(caught.exception.file_transfer["token"], 1)

    def test_recovery_retains_backup_and_does_not_retry_commit(self):
        self.source.write_bytes(b"abc")
        def recovery(op, body):
            if op == 43:
                self.device.state.update(state=9, flags=36, error=9)
                return b""
        self.device.override = recovery
        with self.assertRaisesRegex(RuntimeError, "not verified") as caught:
            self.client.file_put(self.source, self.target)
        receipt = caught.exception.file_transfer
        self.assertEqual(receipt["status"]["state_name"], "recovery")
        self.assertTrue(receipt["paths"]["backup"])
        self.assertFalse(receipt["outcome_unknown"])
        self.assertEqual(self.operations().count(43), 1)
        self.assertNotIn(44, self.operations())

    def test_commit_wrong_crc_never_claims_success(self):
        self.source.write_bytes(b"abc")
        def corrupt(op, body):
            if op == 40 and self.device.commit_states:
                self.device.state["actual_crc"] = 123
        self.device.override = corrupt
        with mock.patch.object(h.time, "sleep"), self.assertRaisesRegex(RuntimeError, "not verified"):
            self.client.file_put(self.source, self.target)

    def test_status_foreign_token_and_deadline_do_not_recommit(self):
        self.source.write_bytes(b"abc")
        def forever(op, body):
            if op == 43:
                self.device.state["state"] = 2
                return b""
        self.device.override = forever
        with mock.patch.object(h.time, "sleep"), self.assertRaises(TimeoutError) as caught:
            self.client.file_put(self.source, self.target, wait_seconds=.1)
        self.assertEqual(self.operations().count(43), 1)
        self.assertEqual(caught.exception.file_status["state"], 2)
        self.device.state["token"] = 99
        with self.assertRaisesRegex(h.ProtocolError, "token changed"):
            self.client._file_wait(1, .1)

    def test_get_streaming_crc_is_host_only(self):
        for size in (0, 1, 512, 513, 1100):
            with self.subTest(size=size):
                data = bytes(i % 251 for i in range(size))
                self.device.files[self.target] = data
                output = self.directory / "download.bin"
                output.write_bytes(b"old local")
                start = len(self.wire.writes)
                result = self.client.file_get(self.target, output)
                self.assertEqual(output.read_bytes(), data)
                self.assertEqual(result["crc32"], zlib.crc32(data))
                self.assertFalse(result["device_crc_verified"])
                self.assertEqual([op for op, _, _ in self.wire.writes[start:]].count(47), 1)

    def test_get_truncation_preserves_old_output_and_partial(self):
        self.device.files[self.target] = b"abc"
        output = self.directory / "download.bin"
        output.write_bytes(b"old")
        self.device.override = lambda op, body: b"ab" if op == 46 else None
        with self.assertRaisesRegex(h.ProtocolError, "truncated") as caught:
            self.client.file_get(self.target, output)
        self.assertEqual(output.read_bytes(), b"old")
        self.assertTrue(Path(caught.exception.file_transfer["partial"]).exists())
        self.assertEqual(self.operations().count(47), 1)

    def test_get_close_error_is_not_retried(self):
        self.device.override = lambda op, body: (3, b"") if op == 47 else None
        with self.assertRaises(h.DeviceError):
            self.client.file_get(self.target, self.directory / "download.bin")
        self.assertEqual(self.operations().count(47), 1)

    def test_stat_and_recovery_paths(self):
        self.assertEqual(self.client.file_stat(self.target)["size"], 3)
        self.assertFalse(self.client.file_stat(r"A:\missing.bin")["exists"])
        self.source.write_bytes(b"")
        with mock.patch.object(h.time, "sleep"):
            put = self.client.file_put(self.source, self.target)
        self.assertEqual(self.client.file_paths(put["token"])["target"], self.target)

    def test_old_firmware_rejects_before_begin(self):
        self.source.write_bytes(b"abc")
        self.device.override = lambda op, body: caps(0x1FF) if op == 9 else None
        with self.assertRaises(RuntimeError):
            self.client.file_put(self.source, self.target)
        self.assertNotIn(41, self.operations())

    def test_status_and_stat_bounds_rejected(self):
        self.device.state["state"] = len(h.FILE_STATES)
        with self.assertRaises(h.ProtocolError):
            self.client.file_status()
        self.device.override = lambda op, body: struct.pack("<III", 0, 10, 0) if op == 48 else None
        with self.assertRaises(h.ProtocolError):
            self.client.file_stat(self.target)

    def test_begin_failure_still_queries_recovery_information(self):
        self.source.write_bytes(b"abc")
        self.device.state.update(token=8, state=9, error=4, flags=36)
        self.device.paths = [self.target, r"A:\left.tmp", r"A:\left.bak"]
        self.device.override = lambda op, body: (9, b"") if op == 41 else None
        with self.assertRaises(h.DeviceError) as caught:
            self.client.file_put(self.source, self.target)
        self.assertEqual(caught.exception.file_transfer["status"]["error_name"], "io")
        self.assertEqual(caught.exception.file_transfer["paths"]["backup"], r"A:\left.bak")
        self.assertNotIn(44, self.operations())  # Do not abort a transaction we did not begin.

    def test_evidence_disk_failure_keeps_original_transfer_error(self):
        self.source.write_bytes(b"abc")
        self.device.override = lambda op, body: struct.pack("<I", 1) if op == 42 else None
        original = h.save_receipt
        def disk_full(path, receipt):
            if "error" in receipt:
                raise OSError("disk full")
            return original(path, receipt)
        with mock.patch.object(h, "save_receipt", disk_full), self.assertRaises(h.ProtocolError) as caught:
            self.client.file_put(self.source, self.target, receipt_path=self.directory / "put.json")
        self.assertIn("disk full", caught.exception.file_transfer["receipt_save_error"])


class DesktopDeployTests(unittest.TestCase):
    setUp = FileTransferTests.setUp
    operations = FileTransferTests.operations
    def test_refresh_waits_past_queued(self):
        with mock.patch.object(h.time, "sleep"):
            result = self.client.desktop_refresh()
        self.assertEqual(result["status"]["state_name"], "done")
        self.assertEqual(self.operations().count(50), 1)
        self.assertGreaterEqual(self.operations().count(52), 4)

    def test_run_requires_observed_payload_entry_even_fast_exit(self):
        for terminal in (3, 4):
            self.device.shell.update(menu_idle=1, state=0)
            self.device.shell_finish = terminal
            with mock.patch.object(h.time, "sleep"):
                result = self.client.desktop_run(self.target)
            self.assertTrue(result["payload_started"])
            self.assertNotEqual(result["before"]["entry_count"], result["status"]["entry_count"])
        self.assertNotIn(5, self.operations())  # No automatic ESC/key injection.

    def test_busy_desktop_rejects_without_queue_or_upload(self):
        self.device.shell["menu_idle"] = 0
        self.source = self.directory / "app.bda"
        bda_fixture(self.source)
        for action in (lambda: self.client.desktop_run(self.target),
                       lambda: self.client.desktop_refresh(),
                       lambda: self.client.deploy(self.source, self.target)):
            with self.assertRaisesRegex(RuntimeError, "desktop|Desktop"):
                action()
        self.assertFalse(set(self.operations()) & {41, 50, 51, 5})

    def test_false_run_without_entry_is_rejected(self):
        original = self.device.__call__
        def handler(op, body):
            response = original(op, body)
            if op == 52 and self.device.shell["state"] == 3:
                fields = list(struct.unpack("<8I", response)); fields[6] = 7
                return struct.pack("<8I", *fields)
            return response
        self.wire.handler = handler
        with mock.patch.object(h.time, "sleep"), self.assertRaisesRegex(h.ProtocolError, "payload entry"):
            self.client.desktop_run(self.target)
        self.assertEqual(self.operations().count(51), 1)

    def test_queued_timeout_and_failure_never_requeue(self):
        self.device.shell_finish = 2
        with mock.patch.object(h.time, "sleep"), self.assertRaises(TimeoutError):
            self.client.desktop_run(self.target, wait_seconds=.1)
        self.assertEqual(self.operations().count(51), 1)
        self.device.shell.update(menu_idle=1, state=0)
        self.device.shell_finish = 5
        with mock.patch.object(h.time, "sleep"), self.assertRaisesRegex(RuntimeError, "failed") as caught:
            self.client.desktop_run(self.target)
        self.assertFalse(caught.exception.desktop_job["outcome_unknown"])

    def test_lost_desktop_ack_does_not_requeue(self):
        self.wire.change = lambda packet: b"" if packet[5] == (51 | 128) else packet
        with self.assertRaises(TimeoutError) as caught:
            self.client.desktop_run(self.target)
        self.assertEqual(self.operations().count(51), 1)
        self.assertTrue(caught.exception.desktop_job["outcome_unknown"])
        self.assertEqual(self.operations()[-1], 51)

    def test_desktop_queue_rejection_is_known_without_retry(self):
        self.device.override = lambda op, body: (2, b"") if op == 51 else None
        with self.assertRaises(h.DeviceError) as caught:
            self.client.desktop_run(self.target)
        self.assertEqual(self.operations().count(51), 1)
        self.assertFalse(caught.exception.desktop_job["outcome_unknown"])

    def test_bda_validation_and_deploy_order(self):
        self.source = self.directory / "app.bda"
        data = bda_fixture(self.source)
        self.assertTrue(h.validate_bda_file(self.source)["ok"])
        with mock.patch.object(h.time, "sleep"):
            result = self.client.deploy(self.source, self.target, receipt_path=self.directory / "deploy.json")
        self.assertTrue(result["completed"] and result["run"]["payload_started"])
        self.assertEqual(self.device.files[self.target], data)
        operations = self.operations()
        self.assertLess(operations.index(43), operations.index(50))
        self.assertLess(operations.index(50), operations.index(51))
        self.assertEqual(result["upload"]["sha256"], h.sha(data))

    def test_deploy_no_run_still_refreshes(self):
        self.source = self.directory / "app.bda"
        bda_fixture(self.source)
        with mock.patch.object(h.time, "sleep"):
            result = self.client.deploy(self.source, self.target, run=False)
        self.assertTrue(result["refresh"]["completed"])
        self.assertNotIn(51, self.operations())

    def test_deploy_freezes_validated_source_before_concurrent_rebuild(self):
        self.source = self.directory / "app.bda"
        data = bda_fixture(self.source)
        def rebuild(op, body):
            if op == 52:
                self.source.write_bytes(b"new unvalidated build")
        self.device.override = rebuild
        with mock.patch.object(h.time, "sleep"):
            self.client.deploy(self.source, self.target, run=False)
        self.assertEqual(self.device.files[self.target], data)

    def test_invalid_bda_fails_before_usb_or_upload(self):
        self.source = self.directory / "app.bda"
        data = bda_fixture(self.source)
        self.source.write_bytes(b"bad!" + data[4:])
        with self.assertRaisesRegex(ValueError, "Invalid H1 BDA"):
            self.client.deploy(self.source, self.target)
        self.assertEqual(self.operations(), [1])
        args = h.parser().parse_args(["deploy", str(self.source), self.target])
        with mock.patch.object(h, "Client") as construct, self.assertRaises(ValueError):
            h.execute(args)
        construct.assert_not_called()

    def test_deploy_failed_commit_never_refreshes(self):
        self.source = self.directory / "app.bda"
        bda_fixture(self.source)
        def failure(op, body):
            if op == 43:
                self.device.state.update(state=8, error=7)
                return b""
        self.device.override = failure
        with self.assertRaises(RuntimeError):
            self.client.deploy(self.source, self.target)
        self.assertNotIn(50, self.operations())
        self.assertNotIn(51, self.operations())

    def test_cli_namespaced_commands_and_json_failure_evidence(self):
        parsed = h.parser().parse_args(["file", "put", str(self.source), self.target, "--wait-seconds", "10"])
        self.assertEqual((parsed.command, parsed.file_command), ("file", "put"))
        parsed = h.parser().parse_args(["desktop", "run", self.target])
        self.assertEqual(parsed.desktop_command, "run")
        report = self.directory / "error.json"
        error = TimeoutError("uncertain commit")
        error.file_transfer = {"token": 19, "outcome_unknown": True}
        with mock.patch.object(h, "execute", side_effect=error), mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(h.main(["--report", str(report), "file", "status"]), 1)
        self.assertEqual(json.loads(report.read_text())["file_transfer"]["token"], 19)


class FileManagementTests(unittest.TestCase):
    def setUp(self):
        self.replies = []
        self.begin_reply = struct.pack("<I", 17)
        self.failures = {}
        self.client, self.wire = client(self.respond)

    def respond(self, op, body):
        if op in self.failures:
            return self.failures[op]
        if op == 9:
            return caps(0xFFF)
        if op == 60:
            return self.begin_reply
        if op == 61:
            self.assertEqual(body, struct.pack("<I", 17))
            return self.replies.pop(0)
        if op == 62:
            self.assertEqual(body, struct.pack("<I", 17))
            return b""
        if 63 <= op <= 66:
            return b""
        raise AssertionError(op)

    def operations(self):
        return [op for op, _, _ in self.wire.writes]

    @staticmethod
    def entry(name, size=0, attributes=0x20):
        raw = name.encode("gbk")
        return struct.pack("<4I", 0, size, attributes, len(raw)) + raw

    def test_root_listing_gbk_names_and_empty_directory(self):
        name = bytes((0x81, 0x5C)).decode("gbk") + ".bin"
        self.replies = [self.entry("应用", attributes=0x10), self.entry(name, 1024), bytes(16)]
        self.replies[-1] = struct.pack("<4I", 1, 0, 0, 0)
        result = self.client.file_list("a:/")
        self.assertEqual((result["path"], result["count"], result["completed"]), ("A:\\", 2, True))
        self.assertTrue(result["entries"][0]["is_directory"])
        self.assertEqual(result["entries"][1], {"name": name, "size": 1024,
                                               "attributes": 0x20, "is_directory": False})
        self.assertEqual(self.operations(), [1, 9, 60, 61, 61, 61, 62])
        self.replies = [struct.pack("<4I", 1, 0, 0, 0)]
        self.assertEqual(self.client.file_list("b:/empty/")["entries"], [])
        self.assertEqual(self.wire.writes[-3][2], b"B:\\empty")

    def test_mutations_send_exact_gbk_paths_and_no_followup_changes(self):
        self.assertTrue(self.client.file_delete(r"A:\测试.bin")["completed"])
        self.assertEqual(self.wire.writes[-1][0::2], (63, r"A:\测试.bin".encode("gbk")))
        self.assertTrue(self.client.file_mkdir(r"B:\data")["completed"])
        self.assertTrue(self.client.file_rmdir(r"B:\data")["completed"])
        result = self.client.file_rename(r"a:\旧目录", r"A:\新目录")
        source = r"A:\旧目录".encode("gbk")
        self.assertEqual(self.wire.writes[-1][2], struct.pack("<I", len(source)) + source + r"A:\新目录".encode("gbk"))
        self.assertEqual(result["source"], r"A:\旧目录")
        self.assertEqual(self.operations(), [1, 9, 63, 65, 66, 64])

    def test_listing_can_report_a_name_longer_than_manageable_path(self):
        name = "汉" * 200
        self.replies = [self.entry(name), struct.pack("<4I", 1, 0, 0, 0)]
        self.assertEqual(self.client.file_list("A:\\")["entries"][0]["name"], name)
        before = len(self.wire.writes)
        with self.assertRaisesRegex(ValueError, "240"):
            self.client.file_delete("A:\\" + name)
        self.assertEqual(len(self.wire.writes), before)

    def test_root_mutations_invalid_paths_cross_volume_and_subdirectory_rejected_before_usb(self):
        for action in (self.client.file_delete, self.client.file_mkdir, self.client.file_rmdir):
            for path in ("A:\\", "B:/", r"F:\x", r"A:\..\x", "A:\\emoji😀"):
                with self.subTest(action=action.__name__, path=path), self.assertRaises(ValueError):
                    action(path)
        for source, target in (("A:\\", r"A:\x"), (r"A:\x", "A:\\"),
                               (r"A:\x", r"B:\x"), (r"A:\Foo", r"a:\foo"),
                               (r"A:\Dir", r"a:\dir\child")):
            with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                self.client.file_rename(source, target)
        self.assertEqual(self.operations(), [1])

    def test_listing_feature_negotiation_never_opens_cursor_on_old_build(self):
        self.failures[9] = caps(0x7FF)
        for action in (lambda: self.client.file_list("A:\\"),
                       lambda: self.client.file_delete(r"A:\file"),
                       lambda: self.client.file_mkdir(r"A:\dir"),
                       lambda: self.client.file_rmdir(r"A:\dir"),
                       lambda: self.client.file_rename(r"A:\old", r"A:\new")):
            with self.assertRaisesRegex(RuntimeError, "file management"):
                action()
        self.assertEqual(self.operations(), [1, 9])

    def test_bad_entry_replies_close_cursor_and_preserve_partial_listing(self):
        malformed = [b"short", struct.pack("<4I", 2, 0, 0, 0),
                     struct.pack("<4I", 1, 1, 0, 0), struct.pack("<4I", 0, 0, 0, 0),
                     struct.pack("<4I", 0, 0x80000000, 0, 1) + b"a",
                     struct.pack("<4I", 0, 0, 0, 1) + b"\x81",
                     struct.pack("<4I", 0, 0, 0, 2) + b"a\0",
                     self.entry(".."), self.entry("sub\\file"),
                     struct.pack("<4I", 0, 0, 0, 2) + b"a"]
        for raw in malformed:
            with self.subTest(raw=raw):
                self.replies = [self.entry("valid.bin", 7), raw]
                with self.assertRaises(h.ProtocolError) as caught:
                    self.client.file_list("A:\\")
                receipt = caught.exception.file_listing
                self.assertFalse(receipt["completed"])
                self.assertEqual(receipt["entries"][0]["name"], "valid.bin")
                self.assertEqual(self.operations()[-1], 62)

    def test_listing_limit_closes_cursor_without_recursive_requests(self):
        self.replies = [self.entry("one", attributes=0x10), self.entry("two")]
        with self.assertRaisesRegex(RuntimeError, "entry limit") as caught:
            self.client.file_list("A:\\", max_entries=1)
        self.assertEqual(len(caught.exception.file_listing["entries"]), 1)
        self.assertEqual(self.operations(), [1, 9, 60, 61, 61, 62])

    def test_next_lost_ack_is_not_retried_and_no_broken_session_cleanup(self):
        self.replies = [self.entry("one")]
        self.wire.change = lambda p: p[:-1] + bytes((p[-1] ^ 1,)) if p[5] == 0xBD else p
        with self.assertRaises(h.ProtocolError) as caught:
            self.client.file_list("A:\\")
        self.assertTrue(caught.exception.file_listing["outcome_unknown"])
        self.assertEqual(self.operations(), [1, 9, 60, 61])

    def test_mutation_lost_ack_preserves_unknown_outcome_without_retry(self):
        self.wire.change = lambda p: p[:-1] + bytes((p[-1] ^ 1,)) if p[5] == 0xBF else p
        with self.assertRaises(h.ProtocolError) as caught:
            self.client.file_delete(r"A:\file.bin")
        receipt = caught.exception.file_management
        self.assertTrue(receipt["outcome_unknown"])
        self.assertFalse(receipt["completed"])
        self.assertEqual(receipt["path"], r"A:\file.bin")
        self.assertEqual(self.operations(), [1, 9, 63])

    def test_native_errors_expose_errno_and_do_not_retry_or_fallback(self):
        for op, error_code, action in ((63, 16, lambda: self.client.file_delete(r"A:\dir")),
                                      (64, 14, lambda: self.client.file_rename(r"A:\old", r"A:\existing")),
                                      (65, 10, lambda: self.client.file_mkdir(r"A:\missing\child")),
                                      (66, 17, lambda: self.client.file_rmdir(r"A:\nonempty"))):
            with self.subTest(op=op):
                self.failures[op] = (error_code, struct.pack("<I", 0x51))
                before = len(self.wire.writes)
                with self.assertRaises(h.DeviceError) as caught:
                    action()
                receipt = caught.exception.file_management
                self.assertEqual(receipt["fs_errno"], 0x51)
                self.assertEqual(receipt["error_name"], h.FILE_ERRORS[error_code])
                self.assertFalse(receipt["outcome_unknown"])
                self.assertEqual([o for o, _, _ in self.wire.writes[before:] if o != 9], [op])

    def test_native_io_failure_does_not_claim_no_changes_occurred(self):
        self.failures[64] = (4, struct.pack("<I", 0x44))
        with self.assertRaises(h.DeviceError) as caught:
            self.client.file_rename(r"A:\old", r"A:\new")
        self.assertTrue(caught.exception.file_management["outcome_unknown"])
        self.assertEqual(self.operations(), [1, 9, 64])

    def test_list_close_failure_is_not_retried(self):
        self.replies = [struct.pack("<4I", 1, 0, 0, 0)]
        self.failures[62] = (4, struct.pack("<I", 0x42))
        with self.assertRaises(h.DeviceError) as caught:
            self.client.file_list("A:\\")
        self.assertFalse(caught.exception.file_listing["completed"])
        self.assertEqual(self.operations(), [1, 9, 60, 61, 62])

    def test_zero_list_token_and_limits_fail_without_next(self):
        self.begin_reply = bytes(4)
        with self.assertRaises(h.ProtocolError):
            self.client.file_list("B:\\")
        self.assertEqual(self.operations(), [1, 9, 60])
        for value in (0, 1000001, -1):
            with self.assertRaises(ValueError):
                self.client.file_list("A:\\", max_entries=value)
        self.assertEqual(self.operations(), [1, 9, 60])

    def test_list_native_error_closes_cursor_and_retains_errno(self):
        self.failures[61] = (4, struct.pack("<I", 0x33))
        with self.assertRaises(h.DeviceError) as caught:
            self.client.file_list("A:\\")
        self.assertEqual(caught.exception.file_listing["fs_errno"], 0x33)
        self.assertEqual(self.operations(), [1, 9, 60, 61, 62])

    def test_listing_status_extension(self):
        state = dict.fromkeys(h.FILE_STATUS_FIELDS, 0)
        state.update(version=1, state=11, token=17)
        self.failures[40] = struct.pack("<16I", *(state[k] for k in h.FILE_STATUS_FIELDS))
        self.assertEqual(self.client.file_status()["state_name"], "listing")

    def test_cli_all_management_actions_and_early_validation(self):
        cases = [("list", ["A:\\"]), ("delete", [r"A:\file.bin"]),
                 ("rename", [r"A:\old", r"A:\new"]), ("mkdir", [r"A:\dir"]),
                 ("rmdir", [r"A:\dir"])]
        with mock.patch.object(h, "Client") as constructor:
            constructor.return_value.__enter__.return_value = self.client
            for action, args in cases:
                self.replies = [struct.pack("<4I", 1, 0, 0, 0)]
                parsed = h.parser().parse_args(["file", action, *args])
                self.assertTrue(h.execute(parsed)["completed"])
        for action in ("delete", "mkdir", "rmdir"):
            with mock.patch.object(h, "Client") as constructor, self.assertRaises(ValueError):
                h.execute(h.parser().parse_args(["file", action, "A:\\"]))
            constructor.assert_not_called()

    def test_cli_error_report_includes_file_management_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "error.json"
            error = TimeoutError("lost delete ACK")
            error.file_management = {"operation": "file_delete", "path": r"A:\file", "outcome_unknown": True}
            with mock.patch.object(h, "execute", side_effect=error), mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(h.main(["--report", str(report), "file", "delete", r"A:\file"]), 1)
            self.assertTrue(json.loads(report.read_text())["file_management"]["outcome_unknown"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
