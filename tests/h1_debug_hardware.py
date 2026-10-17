"""Offline validation of the hardware acceptance tool's heap ownership policy.

Uses the resident MIPS model and executes the actual compiled ownership module.
Only the firmware allocator/free entries are stubbed here; no USB port is opened.
"""
from pathlib import Path
import json
import struct
import sys
import tempfile
import unittest

from unicorn import UC_HOOK_CODE
from unicorn.mips_const import UC_MIPS_REG_A0, UC_MIPS_REG_V0, UC_MIPS_REG_PC, UC_MIPS_REG_RA

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import h1_debug as h
import test_h1_debug_hardware as acceptance
from h1_debug_device import ResidentUSB


class OwnedHeapTests(unittest.TestCase):
    def setUp(self):
        self.usb = ResidentUSB()
        self.client = h.Client(transport=self.usb)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "build/h1-debug", prefix="hardware-offline-")
        self.directory = Path(self.temp.name)
        self.state = self.directory / "state.json"
        self.pointer = 0x81200000
        self.allocations = self.frees = 0
        self.live = False
        self.usb.put(self.usb.base + 68, struct.pack("<I", 0x80310000))
        self.usb.put(0x80310008, struct.pack("<II", 0x800DC998, 0x800DBB54))
        self.usb.put(self.pointer - 16, bytes([0xCC]) * (512 + 32))
        self.usb.u.hook_add(UC_HOOK_CODE, self.allocator)

    def tearDown(self):
        self.temp.cleanup()

    def allocator(self, uc, address, size, _):
        if address == 0x800DC998:
            self.assertEqual(uc.reg_read(UC_MIPS_REG_A0), 512)
            self.assertFalse(self.live)
            self.allocations += 1
            self.live = True
            result = self.pointer
        elif address == 0x800DBB54:
            self.assertEqual(uc.reg_read(UC_MIPS_REG_A0), self.pointer)
            self.assertTrue(self.live)
            self.assertEqual(self.usb.get(self.pointer, 512), acceptance.initial_bytes())
            self.frees += 1
            self.live = False
            result = 0
        else:
            return
        uc.reg_write(UC_MIPS_REG_V0, result)
        uc.reg_write(UC_MIPS_REG_PC, uc.reg_read(UC_MIPS_REG_RA))

    def allocate_for_recovery(self):
        source = self.directory / "owned.c"
        source.write_text(acceptance.OWNED_SOURCE, encoding="ascii")
        info, status = self.client.exec_info(), self.client.status()
        metadata, data = h.build_module(source, info["base"], self.directory / "module")
        self.client.upload(metadata, data)
        cookie = [0x12345678, 0xCAFEBABE]
        receipt, _ = acceptance.run_owned(self.client, acceptance.ALLOC, cookie)
        state = {"released": False, "module": metadata, "cookie": cookie, "pointer": receipt["pointer"],
                 "resident": [status["resident_base"], status["resident_size"]]}
        acceptance.save_json(self.state, state)
        return state

    def test_safe_write_restore_free_and_guards(self):
        result = acceptance.memory(self.client, self.directory, self.state)
        self.assertTrue(result["write_readback_verified"])
        self.assertTrue(result["native_visibility_verified"])
        self.assertEqual((self.allocations, self.frees, self.live), (1, 1, False))
        self.assertEqual(self.usb.get(self.pointer - 16, 16), bytes([0xCC]) * 16)
        self.assertEqual(self.usb.get(self.pointer + 512, 16), bytes([0xCC]) * 16)
        self.assertTrue(json.loads(self.state.read_text())["released"])
        acceptance.guard_no_allocation(self.state)

    def test_wrong_cookie_cannot_write_or_free(self):
        state = self.allocate_for_recovery()
        corrupted = {**state, "cookie": [1, 2]}
        acceptance.save_json(self.state, corrupted)
        with self.assertRaisesRegex(RuntimeError, "cookie"):
            acceptance.cleanup_owned(self.client, self.state)
        self.assertTrue(self.live)
        self.assertEqual(self.frees, 0)
        self.assertEqual(self.usb.get(self.pointer, 512), acceptance.initial_bytes())
        acceptance.save_json(self.state, state)
        self.assertTrue(acceptance.cleanup_owned(self.client, self.state)["released"])

    def test_replaced_module_refuses_cleanup_and_new_upload(self):
        state = self.allocate_for_recovery()
        wrong = {**state, "module": {**state["module"], "crc32": state["module"]["crc32"] ^ 1}}
        acceptance.save_json(self.state, wrong)
        with self.assertRaisesRegex(RuntimeError, "module was replaced"):
            acceptance.cleanup_owned(self.client, self.state)
        with self.assertRaisesRegex(RuntimeError, "may still exist"):
            acceptance.guard_no_allocation(self.state)
        self.assertTrue(self.live)
        acceptance.save_json(self.state, state)
        acceptance.cleanup_owned(self.client, self.state)

    def test_failed_verification_still_restores_and_frees(self):
        original_read = self.client.read_memory
        corrupted = False
        def corrupt_once(address, count):
            nonlocal corrupted
            data = original_read(address, count)
            if address == self.pointer and data == acceptance.replacement_bytes() and not corrupted:
                corrupted = True
                return bytes([data[0] ^ 1]) + data[1:]
            return data
        self.client.read_memory = corrupt_once
        with self.assertRaisesRegex(RuntimeError, "readback mismatch"):
            acceptance.memory(self.client, self.directory, self.state)
        self.assertTrue(corrupted)
        self.assertEqual((self.allocations, self.frees, self.live), (1, 1, False))
        self.assertTrue(json.loads(self.state.read_text())["released"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
