"""Execute the compiled MIPS transport scheduler with delayed USB transactions.

This uses the real resident, protocol and USB code. Only firmware services,
USB MMIO, host ACK timing and clocks are modeled; simulated time is not a
hardware throughput estimate. A private test build never updates the BDA or
its release metadata. Run with --existing-build to test build/h1-debug instead.
"""
from pathlib import Path
import json
import struct
import subprocess
import sys
import unittest

from unicorn.mips_const import UC_MIPS_REG_A0, UC_MIPS_REG_PC, UC_MIPS_REG_RA

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build/h1-debug/transport-test"
sys.path.insert(0, str(ROOT / "tools"))
import build_h1_debug as builder
import h1_debug_device as device
import h1_debug as host


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    tc = ROOT / ".tools/toolchain/bin"
    flags = ["-EL", "-march=mips32", "-mabi=32", "-msoft-float", "-mno-abicalls",
             "-G0", "-fno-pic", "-O2", "-ffreestanding", "-fno-builtin",
             "-fno-stack-protector", "-ffunction-sections", "-fdata-sections",
             "-fno-strict-aliasing", "-g0", "-Wall", "-Wextra", "-Werror",
             "-Wno-misleading-indentation", "-DRD_BUILD_ID=0x54535431u",
             "-I", ROOT / "h1-debug", "-I", ROOT / "vendor/h1-sdk/sdk/include"]
    sources = [ROOT / "h1-debug" / name for name in
               ("resident.c", "firmware.c", "io.c", "usb.c", "files.c", "shell.c")]
    codec = ROOT / "h1-debug/screen_codec.c"
    if codec.exists():
        sources.append(codec)
    sources += [ROOT / "experiments/h1-usb-debug/protocol.c",
                ROOT / "experiments/h1-usb-runner/native_exec.c",
                ROOT / "experiments/h1-usb-runner/trampoline.S"]
    objects = []
    def run(args):
        result = subprocess.run(list(map(str, args)), capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
    for index, source in enumerate(sources):
        obj = OUT / f"{index}-{source.stem}.o"
        run([tc / "mipsel-none-elf-gcc.exe", *flags, "-c", source, "-o", obj])
        objects.append(obj)
    elf, raw = OUT / "resident.elf", OUT / "resident.bin"
    run([tc / "mipsel-none-elf-gcc.exe", *flags, "-nostdlib", "-Wl,--build-id=none",
         "-Wl,--gc-sections", "-Wl,--emit-relocs",
         f"-Wl,-T,{ROOT / 'h1-debug/resident.ld'}", "-o", elf, *objects, "-lgcc"])
    run([tc / "mipsel-none-elf-objcopy.exe", "-O", "binary", elf, raw])
    records, symbols = builder.relocation_records(elf, raw.read_bytes())
    (OUT / "relocations.json").write_text(json.dumps(dict(
        base=builder.LINK_BASE, memory_size=symbols["__bss_end"] - builder.LINK_BASE,
        records=records, symbols=symbols)))


class TimedUSB(device.ResidentUSB):
    """Advance independent SOF/raw clocks and ACK only after a bus delay."""
    def __init__(self):
        self.timed = False
        super().__init__()
        self.time_us = 0
        self.poll_us = 10
        self.frame_origin = 0
        self.frame_frozen = False
        self.tick_frozen = False
        self.tick_origin = self.ticks
        self.ack_delay_us = 100
        self.ack_at = None
        self.poll_entries = 0
        self.after_poll = None
        self.on_reply = None
        self.delayed_out = []
        self.replies = []
        self.sleeps = []
        self.stop_at_sleep = False
        self.release_keys = 0
        self.release_touch = 0
        self.poll_entry = self.symbols.get("transport_poll.part.0", self.symbols.get("transport_poll"))
        # The old model has no physical cable set. A connected session is
        # needed here to test an actual falling cable edge.
        self.put(0xB0010300, struct.pack("<I", 1 << 6))
        self.put(self.symbols["cable_seen"], struct.pack("<I", 1))
        self.timed = True

    def call(self, name, *args):
        # The single-poll helper may or may not be split by GCC.
        if name == "transport_poll.part.0" and name not in self.symbols:
            name = "transport_poll"
        return super().call(name, *args)

    def stat(self, name):
        return self.word(self.symbols[name])

    def queue(self, data, delay_us=0):
        self.delayed_out.append((self.time_us + delay_us, bytearray(data)))

    def _advance(self):
        self.poll_entries += 1
        self.time_us += self.poll_us
        if not self.tick_frozen:
            self.ticks = (self.tick_origin + self.time_us // 25000) & 0xFFFFFFFF
        if self.ack_at is not None and self.time_us >= self.ack_at:
            block = bytes(self.tx[1])
            self.tx[1].clear()
            self.ep[1][0x12] = 0
            self.ack_at = None
            self.packet_sizes.append(len(block))
            assert self.host_in_toggle[1] == self.device_in_toggle[1]
            self.device_in_toggle[1] ^= 1
            self.host_in_toggle[1] ^= 1
            self.held.extend(block)
            if len(block) < 64:
                reply = bytes(self.held)
                self.replies.append(reply)
                self.held.clear()
                if self.on_reply:
                    self.on_reply(reply)
        if self.delayed_out and not self.ep[1][0x16] & 1:
            when, remaining = self.delayed_out[0]
            if when <= self.time_us:
                block = remaining[:64]
                del remaining[:64]
                if not remaining:
                    self.delayed_out.pop(0)
                assert self.host_out_toggle[1] == self.device_out_toggle[1]
                self.host_out_toggle[1] ^= 1
                self.device_out_toggle[1] ^= 1
                self.rx[1] = block
                self.ep[1][0x18], self.ep[1][0x16] = len(block), 1
        if self.after_poll:
            self.after_poll(self.poll_entries)

    def code(self, uc, address, size, user):
        if self.timed:
            if address == self.poll_entry:
                self._advance()
            name = self.stubs.get(address)
            if name == "rd_keys_set" and not uc.reg_read(UC_MIPS_REG_A0):
                self.release_keys += 1
            if name == "rd_touch_set":
                # Root's disconnect/reset release always passes x=0.
                if not uc.reg_read(UC_MIPS_REG_A0):
                    self.release_touch += 1
            if name == "fw_delay":
                self.sleeps.append((uc.reg_read(UC_MIPS_REG_A0), self.poll_entries, self.time_us))
                if self.stop_at_sleep:
                    uc.emu_stop()
                    return
        super().code(uc, address, size, user)

    def usb_read(self, uc, access, address, size, value, user):
        if self.timed and address == 0x1304000C:
            assert size == 2
            frame = self.frame_origin + (0 if self.frame_frozen else self.time_us // 1000)
            self.put(address, struct.pack("<H", frame & 0x7FF))
        else:
            super().usb_read(uc, access, address, size, value, user)

    def usb_write(self, uc, access, address, size, value, user):
        super().usb_write(uc, access, address, size, value, user)
        if self.timed and address == 0x13040012 and self.index == 1 and value & 1:
            assert self.ack_at is None
            self.ack_at = self.time_us + self.ack_delay_us

    def service(self):
        before = self.poll_entries
        self.call("transport_service")
        return self.poll_entries - before

    def run_to_worker_sleep(self):
        from unicorn.mips_const import UC_MIPS_REG_SP
        self.stop_at_sleep = True
        self.u.reg_write(UC_MIPS_REG_SP, 0x83AFF000)
        self.u.reg_write(UC_MIPS_REG_RA, 0x83BFF000)
        self.u.reg_write(UC_MIPS_REG_A0, self.base)
        self.u.emu_start(self.symbols["resident_worker"], 0x83BFF000, count=10_000_000)
        assert self.sleeps
        self.stop_at_sleep = False


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.usb = TimedUSB()

    def request_memory(self, count=500):
        data = bytes(i % 251 for i in range(count))
        self.usb.put(0x83C80000, data)
        self.usb.queue(host.frame(3, 1, struct.pack("<II", 0x83C80000, count)))
        return data

    def test_idle_returns_without_opening_a_poll_window(self):
        self.assertEqual(self.usb.service(), 1)
        self.assertEqual(self.usb.stat("idle_returns"), 1)
        self.assertEqual(self.usb.stat("active_windows"), 0)

    def test_delayed_ack_progresses_after_the_old_32_poll_burst(self):
        self.request_memory()
        self.usb.ack_delay_us = 600
        count = self.usb.service()
        self.assertGreater(count, 32)
        self.assertGreaterEqual(len(self.usb.packet_sizes), 3)
        self.assertEqual(self.usb.stat("frame_limit_exits"), 1)
        self.assertEqual(self.usb.time_us, 2000)
        self.assertEqual(self.usb.stat("iteration_limit_exits"), 0)

    def test_full_reply_crc_and_zlp_with_delayed_ack(self):
        data = self.request_memory(48)
        self.usb.service()
        self.assertEqual(self.usb.packet_sizes, [64, 0])
        self.assertEqual(self.usb.replies, [host.frame(0x83, 1, data)])
        self.assertEqual(self.usb.duplicates, {"in": 0, "out": 0})

    def test_continuous_serial_requests_do_not_extend_the_window(self):
        self.usb.queue(host.frame(1, 1))
        self.usb.on_reply = lambda reply: self.usb.queue(host.frame(1, len(self.usb.replies) + 1), 50)
        count = self.usb.service()
        self.assertGreater(len(self.usb.replies), 5)
        self.assertEqual(count, 200)
        self.assertEqual(self.usb.time_us, 2000)
        self.assertEqual(self.usb.stat("active_windows"), 1)
        self.assertEqual(self.usb.stat("window_polls_max"), count)

    def test_frame_wrap_2047_to_zero_keeps_the_same_budget(self):
        self.usb.frame_origin = 2047
        self.request_memory()
        self.usb.service()
        self.assertEqual(self.usb.stat("frame_limit_exits"), 1)
        self.assertEqual(self.usb.time_us, 2000)

    def test_sof_stopped_exits_on_native_tick_change(self):
        self.usb.frame_frozen = True
        self.request_memory()
        self.usb.service()
        self.assertEqual(self.usb.time_us, 25000)
        self.assertEqual(self.usb.stat("tick_limit_exits"), 1)
        self.assertEqual(self.usb.stat("iteration_limit_exits"), 0)

    def test_both_clocks_frozen_have_a_fixed_count_exit(self):
        self.usb.frame_frozen = self.usb.tick_frozen = True
        self.request_memory()
        count = self.usb.service()
        self.assertEqual(count, 4096)
        self.assertEqual(self.usb.stat("iteration_limit_exits"), 1)
        self.assertEqual(self.usb.stat("window_polls_max"), count)

    def test_unread_host_fifo_does_not_activate_every_later_slice(self):
        self.request_memory()
        self.usb.ack_delay_us = 1_000_000
        self.usb.service()
        before = self.usb.stat("active_windows")
        self.assertEqual(self.usb.service(), 1)
        self.assertEqual(self.usb.stat("active_windows"), before)

    def test_reset_exits_window_and_releases_inputs(self):
        self.request_memory()
        def reset(n):
            if n == 40:
                self.usb.events = 4
                self.usb.ui[6], self.usb.ui[8] = 7, 2
        self.usb.after_poll = reset
        self.assertEqual(self.usb.service(), 40)
        self.assertGreater(self.usb.release_keys, 0)
        self.assertGreater(self.usb.release_touch, 0)
        self.assertEqual(self.usb.word(self.usb.symbols["protocol"] + 1060), 0)
        self.assertEqual((self.usb.ui[6], self.usb.ui[8], self.usb.ui[0xB]), (0, 0, 0))

    def test_disconnect_exits_window_and_releases_inputs(self):
        self.request_memory()
        self.usb.after_poll = lambda n: self.usb.put(0xB0010300, bytes(4)) if n == 40 else None
        self.assertEqual(self.usb.service(), 40)
        self.assertGreater(self.usb.release_keys, 0)
        self.assertGreater(self.usb.release_touch, 0)

    def test_stop_request_exits_window_before_its_deadline(self):
        self.request_memory()
        self.usb.after_poll = lambda n: self.usb.put(self.usb.base + 28, struct.pack("<I", 1)) if n == 40 else None
        self.assertEqual(self.usb.service(), 40)
        self.assertLess(self.usb.time_us, 2000)

    def test_worker_yields_after_one_window_and_does_not_change_irq_ownership(self):
        self.request_memory()
        before = self.usb.imr, self.usb.pdmask, self.usb.ui[6], self.usb.ui[8], self.usb.ui[0xB]
        self.usb.run_to_worker_sleep()
        self.assertEqual(self.usb.sleeps[0], (1, 200, 2000))
        self.assertEqual(before, (self.usb.imr, self.usb.pdmask, self.usb.ui[6], self.usb.ui[8], self.usb.ui[0xB]))

    def test_module_poll_yields_after_one_window(self):
        self.request_memory()
        self.usb.call("module_poll")
        self.assertEqual(self.usb.sleeps, [(1, 200, 2000)])


if __name__ == "__main__":
    if "--existing-build" in sys.argv:
        sys.argv.remove("--existing-build")
    else:
        build()
        device.BUILD = OUT
    unittest.main()
