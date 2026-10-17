"""Execute relocated resident MIPS + native USB FIFO driver against the PC client.

USB registers/FIFOs and firmware tick/key/screen services are explicit models.
The actual frame parser, dispatcher, RAM access, loader, module trampoline and
USB short-packet/ZLP paths execute in Unicorn. This is not hardware evidence;
firmware task lifecycle and physical key/LCD code have separate tests.

The bulk endpoints maintain independent host/device DATA0/DATA1 state. DTR
changes preserve the host state; bus reset, configuration and CLEAR_FEATURE
reset it. Duplicate packets are ACKed but not delivered, as on the USB bus.
"""
from pathlib import Path
import json
import struct
import sys
import unittest

from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn.mips_const import (UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2,
    UC_MIPS_REG_A3, UC_MIPS_REG_V0, UC_MIPS_REG_RA, UC_MIPS_REG_SP,
    UC_MIPS_REG_PC, UC_MIPS_REG_CP0_STATUS)

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build/h1-debug"
sys.path.insert(0, str(ROOT / "tools"))
import h1_debug as host


class ResidentUSB:
    """Serial-compatible host retaining full IN packets until short/ZLP."""
    def __init__(self, base=0x82007FE0):
        relocation = json.loads((BUILD / "relocations.json").read_text())
        image = bytearray((BUILD / "resident.bin").read_bytes())
        delta = base - relocation["base"]
        for offset, kind, value in relocation["records"]:
            word = struct.unpack_from("<I", image, offset)[0]
            value = (value + delta) & 0xFFFFFFFF
            if kind == 1: word = value
            elif kind == 2: word = word & 0xFC000000 | value >> 2 & 0x3FFFFFF
            elif kind == 3: word = word & 0xFFFF0000 | (value + 0x8000) >> 16 & 0xFFFF
            elif kind == 4: word = word & 0xFFFF0000 | value & 0xFFFF
            else: raise AssertionError(kind)
            struct.pack_into("<I", image, offset, word)
        self.base, self.size = base, relocation["memory_size"]
        self.symbols = {name: value + delta for name, value in relocation["symbols"].items() if value}
        self.u = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        self.u.mem_map(0, 64 * 1024 * 1024)
        self.u.mem_map(0x10000000, 0x20000)
        self.u.mem_map(0x13040000, 4096)
        self.u.reg_write(UC_MIPS_REG_CP0_STATUS, 0x10000000)
        self.put(base, image)
        self.put(base + 8, struct.pack("<III", base, base, self.size))
        self.ticks = 0
        self.keys = (0, 0)
        self.lease_end = 0
        self.contact = (0, 0, 0)
        self.touch_lease_end = 0
        self.file_busy = False
        self.shell_state = 0
        self.file_cancellations = []
        self.screen_data = bytes(i % 251 for i in range(1200))
        self.screen_meta = (300, 2, 600, 1, 1200, 0x100000)
        self.pending = bytearray()
        self.available = bytearray()
        self.held = bytearray()
        self.packet_sizes = []
        self.closed = False
        self.index = 0
        self.ui = {0: 0, 1: 0x60, 6: 3, 8: 2, 0xB: 5, 0xE: 0}
        self.ep = {i: {0x10: 64, 0x14: 64 if i == 1 else 0, 0x12: 0, 0x16: 0, 0x18: 0} for i in range(3)}
        self.rx = {i: bytearray() for i in range(3)}
        self.tx = {i: bytearray() for i in range(3)}
        self.host_in_toggle = {1: 0, 2: 0}
        self.host_out_toggle = {1: 0, 2: 0}
        self.device_in_toggle = {1: 0, 2: 0}
        self.device_out_toggle = {1: 0, 2: 0}
        self.duplicates = {"in": 0, "out": 0}
        self.events = self.imr = self.pdmask = 0
        self.stub_names = ("fw_matches", "fw_tick", "fw_delay", "rd_io_init", "rd_io_shutdown",
                           "rd_keys_set", "rd_keys_get", "rd_keys_poll", "rd_screen_capture",
                           "fw_wake_install", "fw_wake_remove", "rd_touch_set",
                           "rd_touch_get", "rd_touch_poll", "rd_files_init", "rd_files_busy",
                           "rd_files_cancel", "rd_files_poll", "rd_shell_init", "rd_shell_shutdown",
                           "rd_shell_active", "rd_shell_stop_blocked", "rd_shell_status", "rd_shell_poll")
        self.stubs = {self.symbols[name]: name for name in self.stub_names if name in self.symbols}
        self.u.hook_add(UC_HOOK_CODE, self.code)
        self.u.hook_add(UC_HOOK_MEM_READ, self.mmio_read, begin=0x10000000, end=0x1001FFFF)
        self.u.hook_add(UC_HOOK_MEM_WRITE, self.mmio_write, begin=0x10000000, end=0x1001FFFF)
        self.u.hook_add(UC_HOOK_MEM_READ, self.usb_read, begin=0x13040000, end=0x13040FFF)
        self.u.hook_add(UC_HOOK_MEM_WRITE, self.usb_write, begin=0x13040000, end=0x13040FFF)
        for address in (0x800615D0, 0x80062104, 0x800623E0):
            self.put(address, struct.pack("<I", 0x27BDFFE8))
        assert self.call("resident_init", base) == 0
        self.put(base + 24, struct.pack("<I", 1))
        self.bus_reset()
        self.control(0, 9, 1)

    def bus_reset(self):
        self.host_in_toggle = {1: 0, 2: 0}
        self.host_out_toggle = {1: 0, 2: 0}
        self.events = 4
        self.ui[6], self.ui[8] = 7, 2  # Bus reset enables endpoint IRQs.
        self.pump()

    def control(self, request_type, request, value=0, index=0, length=0):
        assert not self.rx[0] and not self.ep[0][0x12] & 1
        # USB standard requests synchronize host endpoint toggles; CDC class
        # SET_CONTROL_LINE_STATE changes DTR/RTS only, not endpoint state.
        if request_type == 0 and request == 9:
            self.host_in_toggle = {1: 0, 2: 0}
            self.host_out_toggle = {1: 0, 2: 0}
        elif request_type == 2 and request == 1 and value == 0:
            toggles = self.host_in_toggle if index & 0x80 else self.host_out_toggle
            toggles[index & 15] = 0
        self.rx[0] = bytearray(struct.pack("<BBHHH", request_type, request, value, index, length))
        self.ep[0][0x18], self.ep[0][0x12] = 8, 1
        self.pump()

    def put(self, address, data):
        self.u.mem_write(address & 0x1FFFFFFF, bytes(data))

    def get(self, address, count):
        return bytes(self.u.mem_read(address & 0x1FFFFFFF, count))

    def word(self, address):
        return struct.unpack("<I", self.get(address, 4))[0]

    def call(self, name, *args):
        sp, ret = 0x83AFF000, 0x83BFF000
        self.u.reg_write(UC_MIPS_REG_SP, sp)
        self.u.reg_write(UC_MIPS_REG_RA, ret)
        for reg, value in zip((UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3), args):
            self.u.reg_write(reg, value)
        if len(args) > 4:
            self.put(sp + 16, struct.pack("<" + "I" * (len(args) - 4), *args[4:]))
        self.u.emu_start(self.symbols[name], ret, count=10_000_000)
        assert self.u.reg_read(UC_MIPS_REG_PC) == ret, (name, hex(self.u.reg_read(UC_MIPS_REG_PC)))
        return self.u.reg_read(UC_MIPS_REG_V0)

    def code(self, uc, address, size, _):
        name = self.stubs.get(address)
        if name is None:
            return
        a, b, c, d = (uc.reg_read(r) for r in (UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3))
        result = 0
        if name == "fw_matches": result = 1
        elif name == "fw_tick": result = self.ticks
        elif name == "rd_files_busy": result = int(self.file_busy)
        elif name == "rd_files_cancel": self.file_cancellations.append((a,b))
        elif name == "rd_shell_active": result = int(self.shell_state in (1,2,3))
        elif name == "rd_shell_stop_blocked": result = int(self.shell_state in (1,2))
        elif name == "rd_shell_status":
            self.put(a, struct.pack("<8I",0,0,self.shell_state,0,int(self.shell_state not in (1,2,3)),0,0,0))
        elif name == "rd_keys_set":
            if b > 1023 or ((a or b) and not 1 <= c <= 4000): result = 1
            else: self.keys, self.lease_end = (a, b), d + c
        elif name == "rd_keys_get":
            self.put(a, struct.pack("<I", self.keys[0])); self.put(b, struct.pack("<I", self.keys[1]))
        elif name == "rd_keys_poll" and a >= self.lease_end: self.keys = (0, 0)
        elif name == "rd_touch_set":
            now = self.word(uc.reg_read(UC_MIPS_REG_SP) + 16)
            if c > 1 or (c and (a > 479 or b > 271 or not 1 <= d <= 4000)):
                result = 1
            elif c:
                self.contact, self.touch_lease_end = (a, b, 1), now + d
            else:
                self.contact = (*self.contact[:2], 0)
                self.touch_lease_end = 0
        elif name == "rd_touch_poll":
            if self.touch_lease_end and a >= self.touch_lease_end:
                self.contact = (*self.contact[:2], 0)
                self.touch_lease_end = 0
        elif name == "rd_touch_get":
            remaining = max(0, self.touch_lease_end - b) if self.contact[2] else 0
            self.put(a, struct.pack("<4I", *self.contact, remaining))
        elif name == "rd_screen_capture":
            self.put(a, self.screen_data)
            self.put(c, struct.pack("<6I", *self.screen_meta))
        uc.reg_write(UC_MIPS_REG_V0, result)
        uc.reg_write(UC_MIPS_REG_PC, uc.reg_read(UC_MIPS_REG_RA))

    def mmio_read(self, uc, access, address, size, value, _):
        if address == 0x10001004: self.put(address, self.imr.to_bytes(size, "little"))
        if address == 0x10010320: self.put(address, self.pdmask.to_bytes(size, "little"))

    def mmio_write(self, uc, access, address, size, value, _):
        if address == 0x10001008: self.imr |= value
        if address == 0x1000100C: self.imr &= ~value
        if address == 0x10010324: self.pdmask |= value
        if address == 0x10010328: self.pdmask &= ~value

    def usb_read(self, uc, access, address, size, value, _):
        off = address - 0x13040000
        if off in (0x20, 0x24, 0x28):
            ep = (off - 0x20) // 4
            value = self.rx[ep].pop(0) if self.rx[ep] else 0
        elif off == 0xA: value, self.events = self.events, 0
        elif 0x10 <= off <= 0x18: value = self.ep[self.index].get(off, 0)
        else: value = self.ui.get(off, 0)
        self.put(address, value.to_bytes(size, "little"))

    def usb_write(self, uc, access, address, size, value, _):
        off = address - 0x13040000
        if off in (0x20, 0x24, 0x28):
            self.tx[(off - 0x20) // 4].append(value & 255)
        elif off == 0xE:
            self.index = value & 15
            self.ui[off] = value
        elif 0x10 <= off <= 0x18:
            ep = self.ep[self.index]
            if off == 0x12 and self.index == 0:
                if value & 0x40: ep[off] &= ~1; self.rx[0].clear(); ep[0x18] = 0
                if value & 0x80: ep[off] &= ~0x10
                if value & 2: ep[off] |= 2
                if value == 0: ep[off] = 0
            elif off == 0x12:
                if value & 8:
                    assert ep[off] & 1, "TX FLUSHFIFO without INPKTRDY corrupts data"
                    self.tx[self.index].clear()
                if value & 0x40: self.device_in_toggle[self.index] = 0
                ep[off] = value & 1
            elif off == 0x16:
                if value & 0x10:
                    assert ep[off] & 1, "RX FLUSHFIFO without OUTPKTRDY corrupts data"
                    self.rx[self.index].clear(); ep[0x18] = 0
                if value & 0x80: self.device_out_toggle[self.index] = 0
                ep[off] = value & 1
            else: ep[off] = value
        else: self.ui[off] = value

    def pump(self):
        self.ticks += 1
        if self.pending and not self.ep[1][0x16] & 1:
            block = self.pending[:17]
            del self.pending[:17]
            if self.host_out_toggle[1] == self.device_out_toggle[1]:
                self.rx[1] = bytearray(block)
                self.ep[1][0x18], self.ep[1][0x16] = len(block), 1
                self.device_out_toggle[1] ^= 1
            else:
                self.duplicates["out"] += 1
            self.host_out_toggle[1] ^= 1  # ACK, including a duplicate packet.
        self.call("transport_poll" if "transport_poll" in self.symbols else "transport_poll.part.0")
        if self.ep[1][0x12] & 1:
            block = bytes(self.tx[1]); self.tx[1].clear(); self.ep[1][0x12] = 0
            self.packet_sizes.append(len(block))
            if self.device_in_toggle[1] == self.host_in_toggle[1]:
                self.held.extend(block)
                if len(block) < 64:
                    self.available.extend(self.held); self.held.clear()
                self.host_in_toggle[1] ^= 1
            else:
                self.duplicates["in"] += 1
            self.device_in_toggle[1] ^= 1  # ACK, including a duplicate packet.
        if self.ep[0][0x12] & 2:
            self.tx[0].clear(); self.ep[0][0x12] &= ~2
        # Same launch condition as resident_worker, outside the active RPC.
        if (self.word(self.symbols["exec_diag"]) == 3 and
                not self.word(self.symbols["protocol"] + 1060) and not self.ep[1][0x12] & 1):
            self.call("exec_invoke", self.symbols["module_api"])

    def reset_input_buffer(self):
        self.available.clear()

    def write(self, data):
        assert not self.pending and not self.available and not self.held
        self.pending.extend(data)
        return len(data)

    def read(self, count):
        for _ in range(200):
            if self.available: break
            self.pump()
        data = bytes(self.available[:count]); del self.available[:count]
        return data

    def close(self):
        self.closed = True


class ActualResidentTests(unittest.TestCase):
    def setUp(self):
        self.usb = ResidentUSB()
        self.c = host.Client(transport=self.usb)

    def test_identity_status_caps_and_relocated_memory(self):
        self.assertEqual(self.c.identity, host.IDENTITY.decode())
        self.assertEqual(self.c.capabilities()["features"], 0xFFF)
        state = self.c.status()
        self.assertEqual(state["resident_base"], self.usb.base)
        self.assertEqual((state["usb_configured"], state["usb_resets"], state["usb_fault"]), (1, 1, 0))
        data = bytes(i % 251 for i in range(1300))
        self.c.write_memory(0x83C80000, data)
        self.assertEqual(self.c.read_memory(0x83C80000, len(data)), data)
        before = self.usb.get(self.usb.base, 80)
        with self.assertRaises(host.DeviceError): self.c.write_memory(self.usb.base + 1, b"bad")
        self.assertEqual(self.usb.get(self.usb.base, 80), before)

    def test_exact_64_frame_emits_zlp(self):
        self.usb.packet_sizes.clear()
        self.assertEqual(len(self.c.read_memory(0x83C80000, 48)), 48)
        self.assertEqual(self.usb.packet_sizes[-2:], [64, 0])
        self.assertEqual(self.c.rpc(1), host.IDENTITY)
        self.assertFalse(self.usb.held)

    def test_file_transaction_blocks_module_and_desktop_start(self):
        self.usb.file_busy = True
        for op in (27,30,50,51):
            with self.assertRaises(host.DeviceError) as error:self.c.rpc(op)
            self.assertEqual(error.exception.status,2)
        self.assertEqual(self.c.rpc(1),host.IDENTITY)
        self.assertEqual(len(self.c.rpc(40)),64)
        self.assertEqual(len(self.c.rpc(52)),32)

    def test_loading_blocks_file_changes_but_running_allows_files_and_stop(self):
        for state in (1,2):
            self.usb.shell_state=state
            for op in (10,27,30,41,43,45,60,61,62,63,64,65,66):
                with self.assertRaises(host.DeviceError) as error:self.c.rpc(op)
                self.assertEqual(error.exception.status,2,(state,op))
        self.usb.shell_state=3
        # The FS binding is deliberately absent in this USB-only fixture:
        # reaching BINDING proves the RUNNING shell no longer blocks files.
        for op in (41,45,60,63):
            with self.assertRaises(host.DeviceError) as error:self.c.rpc(op)
            self.assertEqual(error.exception.status,3)
        self.assertEqual(self.c.rpc(10),b'')

    def test_dtr_requests_deferred_file_cleanup(self):
        self.usb.file_cancellations.clear()
        self.usb.control(0x21,0x22,0)
        self.usb.control(0x21,0x22,1)
        self.assertTrue(any(reason==12 for reason,_ in self.usb.file_cancellations))

    def test_invalid_requests_and_crc_recovery(self):
        for op, body in ((3, struct.pack("<II", 0x83FFFFFF, 2)),
                         (3, struct.pack("<II", 0x80000000, 513)),
                         (4, struct.pack("<I", 0xA0000000) + b"x"), (127, b"")):
            with self.assertRaises(host.DeviceError): self.c.rpc(op, body)
        damaged = bytearray(host.frame(1, 700)); damaged[-1] ^= 1
        self.usb.pending.extend(b"junk" + damaged)
        while self.usb.pending: self.usb.pump()
        self.assertEqual(self.c.rpc(1), host.IDENTITY)
        self.assertEqual(self.c.status()["bad_frames"], 1)

    def test_snapshot_token_bounds_and_key_lease(self):
        meta, data = self.c.capture_screen()
        self.assertEqual(data, bytes(i % 251 for i in range(1200)))
        with self.assertRaises(host.DeviceError): self.c.rpc(7, struct.pack("<III", meta["token"], 0, 1))
        self.c.press(["UP", "CONFIRM"], 100)
        self.assertNotEqual(self.c.status()["keys_high"], 0)
        self.usb.ticks += 100
        self.usb.pump()
        self.assertEqual(self.c.status()["keys_high"], 0)
        self.c.release()

    def test_rle_and_raw_read_same_immutable_snapshot(self):
        for bpp in (2, 4):
            pixel = bytes(range(bpp))
            raw = pixel * 18000 + bytes(i % 251 for i in range(8000))
            total = len(raw)
            self.usb.screen_data = raw
            self.usb.screen_meta = (1000, total // (1000 * bpp), 1000 * bpp,
                                    1 if bpp == 2 else 2, total, 0x100000)
            meta = host.words(self.c.rpc(6), host.SCREEN_FIELDS, "SCREEN_CAPTURE")
            uncompressed, raw_stats = self.c.read_screen(meta, codec="raw")
            decoded, rle_stats = self.c.read_screen(meta, codec="rle")
            self.assertEqual((uncompressed, decoded), (raw, raw))
            self.assertLess(rle_stats["protocol_bytes_received"], raw_stats["protocol_bytes_received"])
            self.c.empty(8, struct.pack("<I", meta["token"]))
            with self.assertRaises(host.DeviceError):
                self.c.rpc(13, struct.pack("<III", meta["token"], 0, 4))

    def test_rle_request_boundaries_and_pixel_alignment(self):
        meta = host.words(self.c.rpc(6), host.SCREEN_FIELDS, "SCREEN_CAPTURE")
        token = meta["token"]
        for body in (b"", struct.pack("<III", token + 1, 0, 2),
                     struct.pack("<III", token, 0, 0), struct.pack("<III", token, 0, 16386),
                     struct.pack("<III", token, 1, 2), struct.pack("<III", token, 0, 1),
                     struct.pack("<III", token, 1200, 2)):
            with self.assertRaises(host.DeviceError): self.c.rpc(13, body)
        block = self.c.rpc(13, struct.pack("<III", token, 1198, 16384))
        self.assertEqual(struct.unpack_from("<4I", block), (token, 1198, 1200, 2))
        self.assertEqual(host.decode_screen_rle(block[16:], 2, 2), self.usb.screen_data[-2:])
        self.c.empty(8, struct.pack("<I", token))

    def test_compiled_module_upload_execute_and_output(self):
        directory = BUILD / "device-test-module"
        directory.mkdir(exist_ok=True)
        source = directory / "probe.c"
        source.write_text('''#include "module_abi.h"
int h1_module_entry(const h1_exec_api *a,h1_exec_call *c){
 if(a->version!=1 || c->arg_size!=2)return -9;
 for(unsigned i=0;i<600;++i)c->output[i]=(unsigned char)(i+c->args[0]);
 c->output_size=600;return -(int)c->opcode;
}
''', encoding="ascii")
        info = self.c.exec_info()
        metadata, data = host.build_module(source, info["base"], directory)
        self.c.upload(metadata, data)
        result, output = self.c.run_module(7, b"\x03\x04")
        self.assertEqual(result["return_value"], -7)
        self.assertEqual(result["flags"], 0)
        self.assertEqual(output, bytes((i + 3) & 255 for i in range(600)))
        self.assertEqual(result["output_size"], 600)
        self.c.reset_module()
        self.assertEqual(self.c.exec_info()["state"], 0)

    def test_stop_ack_then_restore(self):
        self.c.touch(40, 132, lease_ticks=1000)
        self.c.stop()
        self.assertEqual(self.usb.word(self.usb.base + 28), 1)
        self.usb.call("resident_shutdown", self.usb.base)
        self.assertEqual(self.usb.word(self.usb.base + 24), 3)
        self.assertEqual((self.usb.imr, self.usb.pdmask), (0, 0))
        self.assertEqual(self.usb.contact, (40, 132, 0))

    def test_touch_protocol_bounds_move_release_lease_and_status_compatibility(self):
        self.assertEqual(len(self.c.rpc(2)), 64)
        self.c.touch(479, 271, lease_ticks=4000)
        state = self.c.touch_status()
        self.assertEqual((state["x"], state["y"], state["pressed"]), (479, 271, 1))
        self.assertTrue(0 < state["lease_remaining"] <= 4000)
        self.c.touch(40, 132, lease_ticks=1000)
        for values in ((480, 0, 1, 80), (0, 272, 1, 80), (0, 0, 2, 80),
                       (0, 0, 1, 0), (0, 0, 1, 4001)):
            with self.assertRaises(host.DeviceError):
                self.c.rpc(11, struct.pack("<4I", *values))
        self.assertEqual(self.usb.contact, (40, 132, 1))
        self.c.release_touch()
        self.assertEqual(self.c.touch_status()["pressed"], 0)
        self.c.touch(50, 60, lease_ticks=80)
        self.usb.ticks += 80
        self.usb.pump()
        state = self.c.touch_status()
        self.assertEqual(state, {"x": 50, "y": 60, "pressed": 0, "lease_remaining": 0})

    def test_touch_dtr_keeps_lease_but_reset_deconfigure_cable_release(self):
        self.usb.control(0x21, 0x22, 1)
        self.c.touch(123, 45, lease_ticks=1000)
        self.usb.control(0x21, 0x22, 0)
        self.usb.control(0x21, 0x22, 1)
        self.assertEqual(self.c.touch_status()["pressed"], 1)
        self.usb.control(0, 9, 0)
        self.assertEqual(self.usb.contact, (123, 45, 0))
        self.usb.control(0, 9, 1)
        self.c.touch(200, 100, lease_ticks=1000)
        self.usb.bus_reset()
        self.assertEqual(self.usb.contact, (200, 100, 0))
        self.usb.control(0, 9, 1)
        self.c.touch(250, 125, lease_ticks=1000)
        self.usb.put(0xB0010300, struct.pack("<I", 1 << 6))
        self.usb.pump()
        self.usb.put(0xB0010300, bytes(4))
        self.usb.pump()
        self.assertEqual(self.usb.contact, (250, 125, 0))

    def test_touch_status_allowed_while_module_runs(self):
        self.usb.put(self.usb.symbols["exec_diag"], struct.pack("<I", 4))
        self.assertEqual(self.c.touch_status()["pressed"], 0)
        with self.assertRaises(host.DeviceError):
            self.c.touch(20, 30)
        self.usb.put(self.usb.symbols["exec_diag"], bytes(4))

    def test_deconfigure_reconfigure_clears_partial_frame_snapshot_keys(self):
        self.c.press("UP", 1000)
        token = struct.unpack_from("<I", self.c.rpc(6))[0]
        # Deliver only ten bytes of an abandoned request (not a complete header).
        self.usb.pending.extend(host.frame(3, 700, struct.pack("<II", 0x83C80000, 8))[:10])
        for _ in range(3): self.usb.pump()
        self.assertEqual(self.usb.word(self.usb.symbols["protocol"] + 1056), 10)
        self.usb.control(0, 9, 0)
        self.assertEqual(self.usb.word(self.usb.symbols["protocol"] + 1056), 0)
        self.assertEqual(self.usb.keys, (0, 0))
        self.usb.control(0, 9, 1)
        self.assertEqual(self.c.rpc(1), host.IDENTITY)
        with self.assertRaises(host.DeviceError):
            self.c.rpc(7, struct.pack("<III", token, 0, 1))
        self.assertEqual(self.c.status()["usb_configured"], 1)

    def test_dtr_recovers_partial_request_and_preserves_finite_keys(self):
        self.usb.control(0x21, 0x22, 1)
        self.c.press("UP", 1000)
        token = struct.unpack_from("<I", self.c.rpc(6))[0]
        self.usb.pending.extend(host.frame(3, 701, struct.pack("<II", 0x83C80000, 8))[:10])
        for _ in range(3): self.usb.pump()
        self.assertEqual(self.usb.word(self.usb.symbols["protocol"] + 1056), 10)
        self.usb.control(0x21, 0x22, 0)
        self.assertEqual(self.usb.word(self.usb.symbols["protocol"] + 1056), 0)
        self.assertEqual(self.usb.keys, (0, 16))
        self.usb.control(0x21, 0x22, 1)
        self.assertEqual(self.c.rpc(1), host.IDENTITY)
        with self.assertRaises(host.DeviceError):
            self.c.rpc(7, struct.pack("<III", token, 0, 1))
        self.usb.ticks += 1000
        self.usb.pump()
        self.assertEqual(self.c.status()["keys_high"], 0)

    def test_dtr_flushes_fifos_without_resetting_data_toggles(self):
        # The constructor PING exchanged one packet in each direction, so the
        # next packets must use DATA1. Reinitializing endpoints here previously
        # changed only the device to DATA0 and broke the second COM session.
        usb = self.usb
        self.assertEqual((usb.host_in_toggle[1], usb.host_out_toggle[1]), (1, 1))
        self.assertEqual((usb.device_in_toggle[1], usb.device_out_toggle[1]), (1, 1))
        usb.rx[1] = bytearray(b"stale OUT")
        usb.ep[1][0x18], usb.ep[1][0x16] = len(usb.rx[1]), 1
        usb.tx[1] = bytearray(b"stale IN")
        usb.ep[1][0x12] = 1
        usb.tx[2] = bytearray(b"stale notification")
        usb.ep[2][0x12] = 1
        usb.device_in_toggle[2] = usb.host_in_toggle[2] = 1
        usb.control(0x21, 0x22, 1)
        self.assertFalse(usb.rx[1] or usb.tx[1] or usb.tx[2] or usb.available)
        self.assertEqual((usb.ep[1][0x16], usb.ep[1][0x12], usb.ep[2][0x12]), (0, 0, 0))
        self.assertEqual(usb.device_in_toggle, usb.host_in_toggle)
        self.assertEqual(usb.device_out_toggle, usb.host_out_toggle)
        for _ in range(4):
            usb.control(0x21, 0x22, 0)
            usb.control(0x21, 0x22, 1)
            reopened = host.Client(transport=usb)
            self.assertEqual(reopened.identity, host.IDENTITY.decode())
        self.assertEqual(usb.duplicates, {"in": 0, "out": 0})

    def test_repeated_dtr_and_rts_changes_preserve_session(self):
        usb = self.usb
        usb.control(0x21, 0x22, 1)
        usb.pending.extend(host.frame(1, 712)[:10])
        for _ in range(3): usb.pump()
        for value in (1, 3, 3, 1):
            # Neither repeating DTR nor changing only RTS abandons an RPC or
            # flushes an already queued IN packet.
            usb.tx[1] = bytearray(b"retained")
            usb.ep[1][0x12] = 1
            usb.control(0x21, 0x22, value)
            self.assertEqual(usb.word(usb.symbols["protocol"] + 1056), 10)
            self.assertEqual(usb.available, b"retained")
            usb.available.clear()
        self.assertEqual(usb.duplicates, {"in": 0, "out": 0})
        usb.control(0x21, 0x22, 0)
        self.assertEqual(usb.word(usb.symbols["protocol"] + 1056), 0)
        self.assertEqual(self.c.rpc(1), host.IDENTITY)

    def test_standard_requests_and_bus_reset_reset_data_toggles(self):
        usb = self.usb
        self.assertEqual((usb.device_in_toggle[1], usb.device_out_toggle[1]), (1, 1))
        usb.control(2, 1, index=0x81)
        self.assertEqual((usb.host_in_toggle[1], usb.device_in_toggle[1]), (0, 0))
        self.assertEqual((usb.host_out_toggle[1], usb.device_out_toggle[1]), (1, 1))
        usb.control(2, 1, index=0x01)
        self.assertEqual((usb.host_out_toggle[1], usb.device_out_toggle[1]), (0, 0))
        self.assertEqual(self.c.rpc(1), host.IDENTITY)
        usb.control(0, 9, 1)
        self.assertEqual((usb.device_in_toggle[1], usb.device_out_toggle[1]), (0, 0))
        self.assertEqual((usb.host_in_toggle[1], usb.host_out_toggle[1]), (0, 0))
        self.assertEqual(self.c.rpc(1), host.IDENTITY)
        usb.bus_reset()
        self.assertEqual((usb.device_in_toggle[1], usb.device_out_toggle[1]), (0, 0))
        self.assertEqual((usb.host_in_toggle[1], usb.host_out_toggle[1]), (0, 0))
        usb.control(0, 9, 1)
        self.assertEqual(self.c.rpc(1), host.IDENTITY)
        self.assertEqual(usb.duplicates, {"in": 0, "out": 0})

    def test_device_only_toggle_reset_discards_packets(self):
        # Sanity-check the model against the hardware regression: a DATA1 host
        # OUT received by a reset DATA0 device is ACKed as a duplicate, so the
        # RPC parser sees nothing. Also exercise the symmetric IN case.
        usb = self.usb
        usb.device_out_toggle[1] = 0
        usb.pending.extend(host.frame(1, 710))
        usb.pump()
        self.assertEqual(usb.duplicates["out"], 1)
        self.assertEqual(usb.word(usb.symbols["protocol"] + 1056), 0)
        self.assertFalse(usb.available)
        usb.device_in_toggle[1] = 0
        usb.pending.extend(host.frame(1, 711))
        for _ in range(3): usb.pump()
        self.assertEqual(usb.duplicates["in"], 1)
        self.assertFalse(usb.available)
        usb.bus_reset()
        usb.control(0, 9, 1)
        self.assertEqual(self.c.rpc(1), host.IDENTITY)

    def test_dtr_discards_partial_reply_and_requests_module_cancel(self):
        self.usb.control(0x21, 0x22, 1)
        # A 64-byte frame has reached the host, but its terminating ZLP has not.
        self.usb.pending.extend(host.frame(3, 702, struct.pack("<II", 0x83C80000, 48)))
        for _ in range(20):
            self.usb.pump()
            if self.usb.held: break
        self.assertEqual(len(self.usb.held), 64)
        self.assertGreater(self.usb.word(self.usb.symbols["protocol"] + 1060), 0)
        self.usb.held.clear()  # Closing the simulated COM handle discards host data.
        self.usb.control(0x21, 0x22, 0)
        self.assertEqual(self.usb.word(self.usb.symbols["protocol"] + 1060), 0)
        self.assertFalse(self.usb.available)
        diag = self.usb.symbols["exec_diag"]
        self.usb.put(diag, struct.pack("<I", 4))  # Model a cooperatively polling module.
        self.usb.put(diag + 28, bytes(4))
        self.usb.control(0x21, 0x22, 1)
        self.assertTrue(self.usb.word(diag + 28) & 4)
        self.usb.put(diag, bytes(4))
        self.assertEqual(self.c.rpc(1), host.IDENTITY)

    def test_native_ownership_loss_releases_keys_and_stops_dispatch(self):
        for reason in ("irq24", "pd6", "highspeed", "endpoint_irq"):
            with self.subTest(reason=reason):
                usb = ResidentUSB()
                c = host.Client(transport=usb)
                c.press("UP", 1000)
                c.touch(90, 80, lease_ticks=1000)
                previous_commands = usb.word(usb.symbols["commands"])
                if reason == "irq24": usb.imr &= ~(1 << 24)
                elif reason == "pd6": usb.pdmask &= ~(1 << 6)
                elif reason == "highspeed": usb.ui[1] |= 0x20
                elif reason == "endpoint_irq": usb.ui[6] = 3
                usb.pending.extend(host.frame(4, 703, struct.pack("<I", 0x83C80000) + b"bad"))
                for _ in range(4): usb.pump()
                self.assertEqual(usb.word(usb.base + 28), 1)
                self.assertNotEqual(usb.word(usb.base + 32), 0)
                self.assertEqual(usb.keys, (0, 0))
                self.assertEqual(usb.contact, (90, 80, 0))
                self.assertEqual(usb.word(usb.symbols["commands"]), previous_commands)
                self.assertEqual(usb.get(0x83C80000, 3), bytes(3))


if __name__ == "__main__":
    unittest.main(verbosity=2)
