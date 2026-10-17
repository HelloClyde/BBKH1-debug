"""Offline MIPS LCD preview sampling, strict bounds and descriptor tests."""
from pathlib import Path
import struct
import sys
import unittest

from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import h1_debug as h
import h1_debug_preview as preview
from h1_debug_io import VM
from h1_debug_device import ResidentUSB


class PreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = ROOT / "build/h1-debug/preview-offline"
        h.build_module(preview.SOURCE, 0x81000020, cls.directory)

    def setUp(self):
        self.vm = VM(self.directory / "module.elf")
        self.reads = []
        self.writes = []
        self.vm.u.hook_add(UC_HOOK_MEM_READ, self.pixel_read, begin=0x01F00000, end=0x01F00000 + 522240 - 1)
        self.vm.u.hook_add(UC_HOOK_MEM_WRITE, self.forbidden_write, begin=0x01F00000, end=0x01F00000 + 522240 - 1)
        self.vm.u.hook_add(UC_HOOK_MEM_WRITE, self.forbidden_write, begin=0x13050000, end=0x13050FFF)
        self.vm.u.hook_add(UC_HOOK_MEM_WRITE, self.forbidden_write, begin=0x0057BBC0, end=0x0057BBCF)
        self.vm.write(0x80003000, struct.pack("<3I", 0x2402007B, 0x03E00008, 0))
        self.vm.write(0x81200000, struct.pack("<7I", 1, 28, 0x80003000, 0, 0, 0, 0))
        self.vm.write(0x812FFFF0, b"\xCC" * 16)
        self.vm.write(0x81301000, b"\xDD" * 16)

    def pixel_read(self, uc, access, address, size, value, _):
        self.reads.append((address, size))

    def forbidden_write(self, uc, access, address, size, value, _):
        self.writes.append((address, size))

    def invoke(self, bits=18, first=0, rows=18, overrides=None):
        self.raw = self.vm.screen(bits)
        bytes_per_pixel = 2 if bits == 16 else 4
        args = [1, 0xA1F00000, 480, 272, 480 * bytes_per_pixel, 1 if bits == 16 else 2, first, rows]
        if overrides:
            for index, value in overrides.items(): args[index] = value
        self.vm.write(0x81200100, struct.pack("<8I", *args))
        self.vm.write(0x81200200, struct.pack("<10I", 1, 40, 0, 32, 0x81200100, 0x81300000, 4096, 0, 0, 0))
        result = self.vm.call("h1_module_entry", 0x81200000, 0x81200200)
        size = self.vm.word(0x81200200 + 28)
        return result, self.vm.read(0x81300000, size)

    def expected(self, bits, first, rows):
        output = bytearray()
        bpp = 2 if bits == 16 else 4
        for y in range(first, first + rows):
            sy = y * 272 // 54
            for x in range(96):
                offset = (sy * 480 + x * 480 // 96) * bpp
                value = int.from_bytes(self.raw[offset:offset + bpp], "little")
                if bits != 16:
                    value = ((value >> 8) & 0xF800) | ((value >> 5) & 0x7E0) | ((value >> 3) & 31)
                output.extend(struct.pack("<H", value))
        return bytes(output)

    def test_all_tiles_rgb565_and_bgrx_exact(self):
        for bits in (16, 18, 32):
            for first in (0, 18, 36):
                with self.subTest(bits=bits, first=first):
                    self.reads.clear()
                    result, data = self.invoke(bits, first)
                    self.assertEqual(result, 0)
                    meta = {"width": 480, "height": 272, "stride": 480 * (2 if bits == 16 else 4),
                            "format": 1 if bits == 16 else 2, "source": 0xA1F00000}
                    tile, pixels = preview.parse_tile(data, meta, first, 18)
                    self.assertEqual(pixels, self.expected(bits, first, 18))
                    self.assertEqual((tile["tick_begin"], tile["tick_end"]), (123, 123))
                    self.assertEqual(len(self.reads), 96 * 18)
                    self.assertTrue(all(size == (2 if bits == 16 else 4) for _, size in self.reads))
        self.assertFalse(self.writes)
        self.assertEqual(self.vm.read(0x812FFFF0, 16), b"\xCC" * 16)
        self.assertEqual(self.vm.read(0x81301000, 16), b"\xDD" * 16)

    def test_invalid_layout_never_reads_pixels(self):
        for overrides in ({0: 2}, {1: 0xB3040000}, {1: 0xA3FFFFFC}, {2: 481}, {3: 273},
                          {4: 1919}, {5: 3}, {6: 54}, {6: 50, 7: 18}, {7: 0}, {7: 19}):
            with self.subTest(overrides=overrides):
                self.reads.clear()
                result, output = self.invoke(overrides=overrides)
                self.assertNotEqual(result, 0)
                self.assertEqual(output, b"")
                self.assertFalse(self.reads)
        self.assertFalse(self.writes)

    def test_descriptor_mismatch_refuses_before_pixels(self):
        # Expected source is a valid RAM span, but it is not the displayed one.
        result, data = self.invoke(overrides={1: 0xA1912000})
        self.assertNotEqual(result, 0)
        self.assertEqual(data, b"")
        self.assertFalse(self.reads)

    def test_descriptor_swap_invalidates_tile(self):
        def swap(uc, access, address, size, value, _):
            self.vm.word(0x8057BBC0 + 4, 0x01912000)
        self.vm.u.hook_add(UC_HOOK_MEM_READ, swap, begin=0x01F00000, end=0x01F00000)
        result, data = self.invoke()
        self.assertEqual(result, (-7) & 0xFFFFFFFF)
        self.assertEqual(data, b"")
        self.assertFalse(self.writes)

    def test_host_assembles_three_tiles_on_existing_client(self):
        usb = ResidentUSB()
        usb.u.mem_map(0x13050000, 4096)
        # Execute the resident's real screen capture, with validated modeled
        # firmware LCD words and RAM. No physical device or serial port.
        del usb.stubs[usb.symbols["rd_screen_capture"]]
        for address in (0x8000CDDC, 0x8000CDE0, 0x80005B48, 0x80005B4C,
                        0x80005824, 0x80005828, 0x8000582C):
            usb.put(address, self.vm.read(address, 4))
        raw = self.vm.screen(18)
        for address in (0x803F2404, 0x803F2408, 0x803F240C, 0xB3050030, 0xB3050040):
            usb.put(address, self.vm.read(address, 4))
        usb.put(0x8057BBC0, self.vm.read(0x8057BBC0, 16))
        usb.put(0x81F00000, raw)
        client = h.Client(transport=usb)
        directory = self.directory / "assembled"
        receipt = preview.preview(client, directory)
        self.raw = raw
        expected = self.expected(18, 0, 54)
        self.assertEqual((directory / "preview.rgb565").read_bytes(), expected)
        self.assertEqual(receipt["pixel_sha256"], h.sha(expected))
        self.assertFalse(receipt["atomic_snapshot"])
        self.assertEqual(len(receipt["tiles"]), 3)
        self.assertFalse(usb.closed)
        self.assertEqual(usb.word(usb.symbols["screen_valid"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
