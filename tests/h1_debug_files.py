"""Execute files.c as MIPS with an explicit native-filesystem fault model.

Native service addresses/signatures are checked against the user's private
V1.41 project.bin. Python models files, semaphore ownership, native handles,
rename-without-overwrite and failures before/after native mutations. This is
not proof of real FAT/FTL persistence or CDC coexistence on hardware.
"""
from pathlib import Path
import hashlib
import json
import struct
import subprocess
import sys
import time
import unittest
import zlib

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN, UC_HOOK_CODE
from unicorn.mips_const import (UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2,
    UC_MIPS_REG_A3, UC_MIPS_REG_V0, UC_MIPS_REG_RA, UC_MIPS_REG_SP,
    UC_MIPS_REG_PC, UC_MIPS_REG_CP0_STATUS)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build/h1-debug/files-test"
FIRMWARE = ROOT / "build/minigpt-native/project.bin"
FIRMWARE_SHA = "d05786e442f9aad62a8d0a0cb4f6d786bdc7c2fa353a7a2b152c9ed9f01b40ef"
TABLE = 0x802AA080
NATIVE = {0x8004D028: "open", 0x80056C9C: "close", 0x80056CEC: "read",
          0x80056EA0: "write", 0x8004D760: "seek", 0x80056F8C: "tell",
          0x8004DCB4: "remove", 0x8004E1E0: "rename", 0x80056524: "stat",
          0x8004E444: "mkdir", 0x8004E9D4: "rmdir", 0x8004EAE0: "findfirst",
          0x80057148: "findnext", 0x80057204: "findclose",
          0x8000D484: "lock", 0x8000D598: "unlock"}
FIELDS = ("version", "state", "token", "size", "received", "expected_crc",
          "actual_crc", "verify_offset", "error", "fs_errno", "flags",
          "old_size", "old_crc", "last_activity_tick", "step_count", "reserved")


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    stub = OUT / "files_stub.c"
    stub.write_text('#include "debug.h"\nrd_header rd_control={.runtime_tables={0,0x802aa080u}};\n'
                    'uint32_t fw_tick(void){return 0;}\n')
    elf = OUT / "files.elf"
    tc = ROOT / ".tools/toolchain/bin"
    args = [tc / "mipsel-none-elf-gcc.exe", "-EL", "-march=mips32", "-mabi=32",
            "-msoft-float", "-mno-abicalls", "-G0", "-fno-pic", "-O2",
            "-ffreestanding", "-fno-builtin", "-fno-stack-protector", "-Wall",
            "-Wextra", "-Werror", "-Wno-misleading-indentation", "-nostdlib",
            "-Wl,--build-id=none", "-Wl,-Ttext,0x82000000", "-Wl,-e,rd_files_init",
            "-I", ROOT / "h1-debug", "-o", elf, ROOT / "h1-debug/files.c", stub, "-lgcc"]
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return elf


class FileVM:
    def __init__(self, elf):
        self.u = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        self.u.mem_map(0, 64 * 1024 * 1024)
        self.u.reg_write(UC_MIPS_REG_CP0_STATUS, 0x10000000)
        firmware = FIRMWARE.read_bytes()
        assert hashlib.sha256(firmware).hexdigest() == FIRMWARE_SHA
        self.put(0x80004000, firmware)
        with elf.open("rb") as stream:
            parsed = ELFFile(stream)
            for segment in parsed.iter_segments():
                if segment["p_type"] == "PT_LOAD":
                    self.put(segment["p_vaddr"], segment.data())
            self.symbols = {s.name: s["st_value"] for s in parsed.get_section_by_name(".symtab").iter_symbols()}
        self.ticks = 10
        self.entries = {}
        self.directories = {b"A:\\", b"B:\\"}
        self.handles = {}
        self.next_handle = 0x81800000
        self.next_cluster = 4
        self.locked = False
        self.operations = []
        self.fault = None
        self.calls = {}
        self.read_sizes = []
        self.cursors = {}
        self.put(0x8071D378, struct.pack("<I", 0x8058F000))
        self.put(0x80CCCB90, bytes(400))
        self.u.hook_add(UC_HOOK_CODE, self.code)
        assert self.call("rd_files_init") == 0

    def put(self, address, data):
        self.u.mem_write(address & 0x1FFFFFFF, bytes(data))

    def get(self, address, count):
        return bytes(self.u.mem_read(address & 0x1FFFFFFF, count))

    def word(self, address, value=None):
        if value is None:
            return struct.unpack("<I", self.get(address, 4))[0]
        self.put(address, struct.pack("<I", value & 0xFFFFFFFF))

    def string(self, address):
        result = bytearray()
        while True:
            c = self.get(address, 1)[0]
            if not c:
                return bytes(result)
            result.append(c)
            address += 1
            assert len(result) < 1024

    @staticmethod
    def key(path):
        # ASCII FAT case folding must not change a GBK trail byte.
        result = bytearray(path)
        i = 0
        while i < len(result):
            if 0x81 <= result[i] <= 0xFE:
                i += 2
            else:
                if 97 <= result[i] <= 122:
                    result[i] -= 32
                i += 1
        return bytes(result)

    def add(self, path, data):
        if isinstance(path, str):
            path = path.encode("gbk")
        path = self.key(path)
        self.entries[path] = dict(data=bytearray(data), cluster=self.next_cluster)
        self.next_cluster += 1
        return path

    def file(self, path):
        if isinstance(path, str):
            path = path.encode("gbk")
        return bytes(self.entries[self.key(path)]["data"])

    def new_handle(self, path):
        record = self.entries[path]
        pointer = self.next_handle
        self.next_handle += 0x100
        self.put(pointer, bytes(0x58))
        self.word(pointer + 0x18, record["cluster"])
        self.put(pointer + 0x48, struct.pack("<H", path[0] - 65))
        for slot in range(100):
            if not self.word(0x80CCCB90 + slot * 4):
                self.word(0x80CCCB90 + slot * 4, pointer)
                self.handles[pointer] = dict(path=path, offset=0, slot=slot, record=record)
                return pointer
        raise AssertionError("handle exhaustion")

    def close_handle(self, pointer):
        record = self.handles.pop(pointer)
        self.word(0x80CCCB90 + 4 * record["slot"], 0)

    def code(self, uc, address, size, user):
        if address == self.symbols["fw_tick"]:
            result = self.ticks
        elif address not in NATIVE:
            return
        else:
            op = NATIVE[address]
            a, b, c, d = (uc.reg_read(r) for r in
                         (UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3))
            result = self.native(op, a, b, c, d)
        uc.reg_write(UC_MIPS_REG_V0, result & 0xFFFFFFFF)
        uc.reg_write(UC_MIPS_REG_PC, uc.reg_read(UC_MIPS_REG_RA))

    def native(self, op, a, b, c, d):
        if op == "lock":
            assert not self.locked, "nested native filesystem semaphore acquisition"
            self.locked = True
            self.put(c, b"\0")
            return 0
        if op == "unlock":
            assert self.locked
            self.locked = False
            return 0
        if op == "stat":
            assert self.locked, "FS+54 is not internally locked"
        else:
            assert not self.locked, "stdio wrappers already take the native lock"
        path = None
        if op in ("stat", "open", "remove", "rename", "mkdir", "rmdir", "findfirst"):
            path = self.key(self.string(a))
        self.calls[op] = self.calls.get(op, 0) + 1
        self.operations.append((op, path))
        behavior = self.fault(self, op, path) if self.fault else None
        self.word(0x8057AEFC, 0)
        if behavior == "fail" and op != "close":
            self.word(0x8057AEFC, 0xD0)
            return 0 if op in ("open", "read", "write") else -1
        if op == "stat":
            if path in self.directories:
                self.put(b, struct.pack("<6I", 0x40000000, 0, 0, 0, 0, 0))
            elif path in self.entries:
                self.put(b, struct.pack("<6I", 0, 0, len(self.entries[path]["data"]), 0, 0, 0))
            else:
                self.word(0x8057AEFC, 0x12)
                return -1
            return 0
        if op == "mkdir":
            if path in self.directories or path in self.entries:
                self.word(0x8057AEFC, 0x30)
                return -1
            self.directories.add(path)
            return 0
        if op == "rmdir":
            if path not in self.directories:
                self.word(0x8057AEFC, 0x12)
                return -1
            if any(p.startswith(path + b"\\") for p in self.directories | self.entries.keys()):
                self.word(0x8057AEFC, 0x31)
                return -1
            self.directories.remove(path)
            return 0
        if op in ("findfirst", "findnext"):
            if op == "findfirst":
                assert path.endswith(b"\\*.*")
                prefix = path[:-3]
                entries = self.directories if b == 0x16 else self.entries
                assert b in (0x16, 0x06)
                names = []
                for item in sorted(entries):
                    if item.startswith(prefix) and item != prefix:
                        name = item[len(prefix):]
                        # A GBK trail 0x5c is not a path separator.
                        i = 0
                        while i < len(name):
                            if 0x81 <= name[i] <= 0xFE:
                                i += 2
                            elif name[i] == 0x5C:
                                break
                            else:
                                i += 1
                        if i == len(name):
                            names.append((name, 0 if b == 0x16 else len(self.entries[item]["data"]), 0x10 if b == 0x16 else 0x20))
                if not names:
                    self.word(0x8057AEFC, 0x12)
                    self.word(c, 0)
                    return -1
                a = c
                self.cursors[a] = names
                self.word(a, 0x81900000 + len(self.cursors) * 0x1000)
            names = self.cursors[a]
            if not names:
                self.word(0x8057AEFC, 0x12)
                return -1
            name, size, attrs = names.pop(0)
            self.word(a + 4, size)
            self.word(a + 8, attrs)
            self.put(a + 0x12, name + b"\0")
            return 0
        if op == "findclose":
            self.cursors.pop(a)
            self.word(a, 0)
            return 0
        if op == "open":
            mode = self.string(b)
            if mode == b"wb":
                self.add(path, b"")
                if behavior == "after":
                    self.word(0x8057AEFC, 0xD0)
                    return 0
            elif path not in self.entries:
                self.word(0x8057AEFC, 0x12)
                return 0
            return self.new_handle(path)
        if op == "close":
            self.close_handle(a)
            if behavior == "fail":
                self.word(0x8057AEFC, 0xD0)
                return -1
            return 0
        if op == "seek":
            record = self.handles[a]
            offset = b if b < 0x80000000 else b - 0x100000000
            offset += 0 if c == 0 else record["offset"] if c == 1 else len(record["record"]["data"])
            if offset < 0:
                return -1
            record["offset"] = offset
            return offset
        if op == "tell":
            return self.handles[a]["offset"]
        if op in ("read", "write"):
            assert b == 1
            record = self.handles[d]
            data, offset = record["record"]["data"], record["offset"]
            count = c if behavior != "short" else c // 2
            if op == "read":
                self.read_sizes.append(c)
                count = min(count, len(data) - offset)
                self.put(a, data[offset:offset + count])
            else:
                data[offset:offset + count] = self.get(a, count)
            record["offset"] += count
            return count
        if op == "remove":
            if path not in self.entries:
                self.word(0x8057AEFC, 0x12)
                return -1
            if any(h["record"] is self.entries[path] for h in self.handles.values()):
                self.word(0x8057AEFC, 0x33)
                return -1
            del self.entries[path]
            return 0
        if op == "rename":
            destination = self.key(self.string(b))
            if path in self.directories:
                if destination in self.directories or destination in self.entries:
                    self.word(0x8057AEFC, 0x51)
                    return -1
                self.directories = {destination + p[len(path):] if p == path or p.startswith(path + b"\\") else p
                                    for p in self.directories}
                self.entries = {destination + p[len(path):] if p.startswith(path + b"\\") else p: value
                                for p, value in self.entries.items()}
                return 0
            if path not in self.entries or destination in self.entries:
                self.word(0x8057AEFC, 0x51)
                return -1
            self.entries[destination] = self.entries.pop(path)
            if behavior == "corrupt":
                self.entries[destination]["data"][0] ^= 1
            if behavior == "after":
                self.word(0x8057AEFC, 0xD0)
                return -1
            return 0
        raise AssertionError(op)

    def call(self, name, *args):
        ret, stack = 0x83BFF000, 0x83AFF000
        self.u.reg_write(UC_MIPS_REG_SP, stack)
        self.u.reg_write(UC_MIPS_REG_RA, ret)
        for register, value in zip((UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3), args):
            self.u.reg_write(register, value)
        if len(args) > 4:
            self.put(stack + 16, struct.pack("<" + "I" * (len(args) - 4), *args[4:]))
        self.u.emu_start(self.symbols[name], ret, count=8_000_000)
        assert self.u.reg_read(UC_MIPS_REG_PC) == ret, name
        assert not self.locked
        return self.u.reg_read(UC_MIPS_REG_V0)

    def command(self, op, body=b"", expect=0):
        self.put(0x83A00000, body + b"\0")
        self.put(0x83A01000, bytes(512))
        self.word(0x83A02000, 0xFFFFFFFF)
        count = self.call("rd_files_command", op, 0x83A00000, len(body), 0x83A01000, 0x83A02000)
        status = self.word(0x83A02000)
        assert status == expect, (op, status, expect, self.status() if op != 40 else None)
        return self.get(0x83A01000, count)

    def status(self):
        return dict(zip(FIELDS, struct.unpack("<16I", self.command(40))))

    def paths(self):
        token = self.status()["token"]
        return [self.command(49, struct.pack("<II", token, kind)) for kind in range(3)]

    def begin(self, path, data):
        if isinstance(path, str):
            path = path.encode("gbk")
        return struct.unpack("<2I", self.command(41, struct.pack("<II", len(data), zlib.crc32(data)) + path))[0]

    def upload(self, path, data):
        token = self.begin(path, data)
        for offset in range(0, len(data), 504):
            chunk = data[offset:offset + 504]
            result = self.command(42, struct.pack("<II", token, offset) + chunk)
            assert struct.unpack("<I", result)[0] == offset + len(chunk)
        return token

    def finish(self, token):
        self.command(43, struct.pack("<I", token))
        return self.drain()

    def drain(self):
        for _ in range(200):
            if not self.call("rd_files_busy"):
                assert not self.handles, self.handles
                assert not self.cursors, self.cursors
                return self.status()
            self.ticks += 1
            self.call("rd_files_poll", self.ticks)
        raise AssertionError(self.status())


class FileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.elf = build()

    def setUp(self):
        self.vm = FileVM(self.elf)
        self.path = b"A:\\Example.dat"

    def test_upload_replaces_and_verifies_new_file_then_cleans_backup(self):
        self.vm.add(self.path, b"old contents")
        data = bytes(range(256)) * 37
        result = self.vm.finish(self.vm.upload(self.path, data))
        self.assertEqual((result["state"], result["actual_crc"]), (5, zlib.crc32(data)))
        self.assertEqual(result["old_crc"], zlib.crc32(b"old contents"))
        self.assertEqual(self.vm.file(self.path), data)
        self.assertEqual(list(self.vm.entries), [self.vm.key(self.path)])

    def test_zero_length_new_upload_and_download(self):
        self.assertEqual(self.vm.finish(self.vm.begin(self.path, b""))["state"], 5)
        token, size = struct.unpack("<II", self.vm.command(45, self.path))
        self.assertEqual(size, 0)
        self.assertEqual(self.vm.command(46, struct.pack("<III", token, 0, 1)), b"")
        self.vm.command(47, struct.pack("<I", token))

    def test_get_stat_ranges_and_no_whole_file_crc_claim(self):
        data = b"0123456789" * 99
        self.vm.add(self.path, data)
        self.assertEqual(struct.unpack("<3I", self.vm.command(48, self.path)), (1, len(data), 0))
        token, size = struct.unpack("<II", self.vm.command(45, self.path))
        self.assertEqual(size, len(data))
        self.assertEqual(self.vm.command(46, struct.pack("<III", token, 600, 512)), data[600:])
        self.vm.command(46, struct.pack("<III", token, 991, 1), expect=1)
        self.assertEqual(self.vm.status()["actual_crc"], 0)
        self.vm.command(47, struct.pack("<I", token))
        self.assertFalse(self.vm.handles)

    def test_offset_token_and_incomplete_commit_rejected_without_mutation(self):
        token = self.vm.begin(self.path, b"abcd")
        self.vm.command(42, struct.pack("<II", token + 1, 0) + b"ab", expect=7)
        self.vm.command(42, struct.pack("<II", token, 0) + b"ab")
        self.vm.command(42, struct.pack("<II", token, 0) + b"ab", expect=6)
        self.vm.command(43, struct.pack("<I", token), expect=6)
        self.assertEqual(self.vm.status()["received"], 2)
        self.vm.command(44, struct.pack("<I", token))
        self.assertEqual(self.vm.drain()["state"], 7)

    def test_crc_mismatch_before_commit_keeps_old_target(self):
        self.vm.add(self.path, b"old")
        token = self.vm.begin(self.path, b"good")
        self.vm.command(42, struct.pack("<II", token, 0) + b"evil")
        self.vm.command(43, struct.pack("<I", token), expect=5)
        self.assertEqual(self.vm.drain()["state"], 8)
        self.assertEqual(self.vm.file(self.path), b"old")

    def test_temporary_readback_detects_corruption_before_target_rename(self):
        self.vm.add(self.path, b"old")
        token = self.vm.upload(self.path, b"new")
        self.vm.entries[self.vm.key(self.vm.paths()[1])]["data"][0] ^= 1
        result = self.vm.finish(token)
        self.assertEqual((result["state"], result["error"]), (8, 5))
        self.assertEqual(self.vm.file(self.path), b"old")
        self.assertNotIn("rename", self.vm.calls)

    def test_short_write_and_failed_close_cleanup_preserve_old_target(self):
        for operation, behavior in (("write", "short"), ("close", "fail")):
            with self.subTest(operation=operation):
                self.setUp()
                self.vm.add(self.path, b"old")
                token = self.vm.begin(self.path, b"new")
                fired = []
                def fault(vm, op, path):
                    if op == operation and not fired:
                        fired.append(True)
                        return behavior
                self.vm.fault = fault
                if operation == "write":
                    self.vm.command(42, struct.pack("<II", token, 0) + b"new", expect=4)
                    result = self.vm.drain()
                else:
                    self.vm.command(42, struct.pack("<II", token, 0) + b"new")
                    result = self.vm.finish(token)
                self.assertEqual(result["state"], 8)
                self.assertEqual(self.vm.file(self.path), b"old")

    def test_failed_install_rolls_back_and_reads_old_crc(self):
        self.vm.add(self.path, b"old")
        token = self.vm.upload(self.path, b"new")
        self.vm.fault = lambda vm, op, path: "fail" if op == "rename" and path.endswith(b".TMP") else None
        result = self.vm.finish(token)
        self.assertEqual(result["state"], 8)
        self.assertEqual(result["old_crc"], zlib.crc32(b"old"))
        self.assertEqual(self.vm.file(self.path), b"old")
        self.assertEqual(list(self.vm.entries), [self.vm.key(self.path)])

    def test_failed_backup_rename_preserves_original_target(self):
        self.vm.add(self.path, b"old")
        token = self.vm.upload(self.path, b"new")
        self.vm.fault = lambda vm, op, path: "fail" if op == "rename" and path == vm.key(self.path) else None
        result = self.vm.finish(token)
        self.assertEqual(result["state"], 8)
        self.assertEqual(self.vm.file(self.path), b"old")
        self.assertEqual(list(self.vm.entries), [self.vm.key(self.path)])

    def test_failed_open_after_directory_entry_creation_cleans_owned_temporary(self):
        self.vm.add(self.path, b"old")
        self.vm.fault = lambda vm, op, path: "after" if op == "open" else None
        self.vm.command(41, struct.pack("<II", 3, zlib.crc32(b"new")) + self.path, expect=4)
        self.assertEqual(self.vm.drain()["state"], 8)
        self.assertEqual(list(self.vm.entries), [self.vm.key(self.path)])
        self.assertEqual(self.vm.file(self.path), b"old")

    def test_short_read_verification_never_replaces_target(self):
        self.vm.add(self.path, b"old")
        token = self.vm.upload(self.path, b"new")
        self.vm.fault = lambda vm, op, path: "short" if op == "read" else None
        self.assertEqual(self.vm.finish(token)["state"], 8)
        self.assertEqual(self.vm.file(self.path), b"old")

    def test_verification_reads_at_most_one_4096_byte_chunk_per_worker_step(self):
        self.vm.add(self.path, b"old" * 9000)
        token = self.vm.upload(self.path, b"new" * 10000)
        self.vm.command(43, struct.pack("<I", token))
        for _ in range(100):
            if not self.vm.call("rd_files_busy"):
                break
            before = self.vm.calls.get("read", 0)
            self.vm.call("rd_files_poll", self.vm.ticks)
            self.assertLessEqual(self.vm.calls.get("read", 0) - before, 1)
        self.assertEqual(self.vm.status()["state"], 5)
        self.assertEqual(max(self.vm.read_sizes), 4096)

    def test_final_target_corruption_rolls_back(self):
        self.vm.add(self.path, b"old")
        token = self.vm.upload(self.path, b"new")
        self.vm.fault = lambda vm, op, path: "corrupt" if op == "rename" and path.endswith(b".TMP") else None
        result = self.vm.finish(token)
        self.assertEqual((result["state"], result["error"]), (8, 5))
        self.assertEqual(self.vm.file(self.path), b"old")

    def test_failed_rollback_retains_backup_and_blocks_new_begin(self):
        self.vm.add(self.path, b"old")
        token = self.vm.upload(self.path, b"new")
        self.vm.fault = lambda vm, op, path: "fail" if op == "rename" and path.endswith((b".TMP", b".BAK")) else None
        result = self.vm.finish(token)
        self.assertEqual(result["state"], 9)
        self.assertEqual(self.vm.file(self.vm.paths()[2]), b"old")
        self.vm.command(41, struct.pack("<II", 0, 0) + b"A:\\other.dat", expect=9)
        self.assertEqual(self.vm.status()["token"], token)

    def test_ambiguous_after_mutation_rename_retains_old_backup(self):
        self.vm.add(self.path, b"old")
        token = self.vm.upload(self.path, b"new")
        self.vm.fault = lambda vm, op, path: "after" if op == "rename" and path.endswith(b".TMP") else None
        result = self.vm.finish(token)
        self.assertEqual(result["state"], 9)
        self.assertEqual(self.vm.file(self.vm.paths()[2]), b"old")
        self.assertEqual(self.vm.file(self.path), b"new")

    def test_backup_cleanup_failure_reports_done_with_backup(self):
        self.vm.add(self.path, b"old")
        token = self.vm.upload(self.path, b"new")
        self.vm.fault = lambda vm, op, path: "fail" if op == "remove" and path.endswith(b".BAK") else None
        result = self.vm.finish(token)
        self.assertEqual(result["state"], 5)
        self.assertTrue(result["flags"] & 4)
        self.assertEqual(result["fs_errno"], 0xD0)
        self.assertEqual(self.vm.file(self.path), b"new")

    def test_transport_cancel_at_each_commit_step_preserves_old_file(self):
        for cancel_after in range(1, 15):
            with self.subTest(cancel_after=cancel_after):
                self.setUp()
                self.vm.add(self.path, b"old")
                token = self.vm.upload(self.path, b"new")
                self.vm.command(43, struct.pack("<I", token))
                for _ in range(cancel_after):
                    self.vm.call("rd_files_poll", self.vm.ticks)
                if self.vm.status()["state"] == 5:
                    continue
                self.vm.call("rd_files_cancel", 12, self.vm.ticks)
                result = self.vm.drain()
                self.assertEqual(result["state"], 7)
                self.assertEqual(self.vm.file(self.path), b"old")
                self.assertEqual(result["token"], token)

    def test_idle_timeout_and_tick_wrap_close_upload_or_get(self):
        for reading in (False, True):
            with self.subTest(reading=reading):
                self.setUp()
                self.vm.ticks = 0xFFFFFFF0
                if reading:
                    self.vm.add(self.path, b"old")
                    self.vm.command(45, self.path)
                else:
                    self.vm.begin(self.path, b"new")
                self.vm.call("rd_files_poll", (0xFFFFFFF0 + 4000) & 0xFFFFFFFF)
                result = self.vm.drain()
                self.assertEqual((result["state"], result["error"]), (7, 13))

    def test_open_foreground_handle_blocks_replace_and_preserves_it(self):
        key = self.vm.add(self.path, b"old")
        foreign = self.vm.new_handle(key)
        token = self.vm.upload(self.path, b"new")
        self.vm.command(43, struct.pack("<I", token))
        for _ in range(30):
            self.vm.call("rd_files_poll", self.vm.ticks)
            if not self.vm.call("rd_files_busy"):
                break
        self.assertEqual((self.vm.status()["state"], self.vm.status()["error"]), (8, 8))
        self.assertEqual(self.vm.file(self.path), b"old")
        self.assertIn(foreign, self.vm.handles)
        self.vm.close_handle(foreign)

    def test_gbk_trail_backslash_is_not_path_separator(self):
        path = b"B:\\" + b"\x81\x5c" + b".bin"
        token = self.vm.upload(path, b"ordinary data")
        self.assertTrue(self.vm.paths()[1].startswith(b"B:\\H1D"))
        self.assertEqual(self.vm.finish(token)["state"], 5)
        self.assertEqual(self.vm.file(path), b"ordinary data")

    def test_invalid_paths_are_rejected_without_filesystem_mutation(self):
        for path in (b"C:\\x", b"A:relative", b"A:\\..\\x", b"A:\\x.\\y",
                     b"A:\\x ", b"A:\\x\0y", b"A:\\x?", b"A:\\x\\", b"A:\\\\x",
                     b"A:\\x\x81", b"A:\\x\x81\x7f", b"A:\\" + b"x" * 238):
            with self.subTest(path=path):
                self.vm.command(41, struct.pack("<II", 0, 0) + path, expect=1)
        self.assertFalse(self.vm.operations)

    def test_temporary_name_collision_does_not_overwrite_existing_data(self):
        self.vm.add(b"A:\\H1D00000001.TMP", b"unrelated")
        token = self.vm.upload(self.path, b"new")
        self.assertEqual(token, 2)
        self.assertEqual(self.vm.finish(token)["state"], 5)
        self.assertEqual(self.vm.file(b"A:\\H1D00000001.TMP"), b"unrelated")

    def test_changed_firmware_binding_disables_mutations(self):
        self.vm.word(TABLE + 0x28, 0x8004E1E4)
        self.assertEqual(self.vm.call("rd_files_init"), 3)
        self.vm.command(41, struct.pack("<II", 0, 0) + self.path, expect=3)
        self.assertEqual(self.vm.status()["state"], 0)

    def test_management_mkdir_list_file_rename_delete_and_rmdir(self):
        directory = "A:\\管理测试".encode("gbk")
        child = directory + "\\子目录".encode("gbk")
        file = directory + "\\数据.bin".encode("gbk")
        self.vm.command(65, directory)
        self.vm.command(65, child)
        self.vm.add(file, b"original file")
        token, = struct.unpack("<I", self.vm.command(60, directory + b"\\"))
        self.assertEqual(self.vm.status()["state"], 11)
        found = []
        for _ in range(3):
            reply = self.vm.command(61, struct.pack("<I", token))
            done, size, attributes, count = struct.unpack_from("<4I", reply)
            self.assertEqual(len(reply), 16 + count)
            if not done:
                found.append((reply[16:], size, attributes))
        self.assertEqual(found, [("子目录".encode("gbk"), 0, 16), ("数据.BIN".encode("gbk"), 13, 32)])
        self.assertFalse(self.vm.cursors)
        self.vm.command(62, struct.pack("<I", token))
        renamed = directory + b"\\renamed.bin"
        self.vm.command(64, struct.pack("<I", len(file)) + file + renamed)
        self.assertEqual(self.vm.file(renamed), b"original file")
        self.vm.command(63, renamed)
        error = self.vm.command(66, directory, expect=17)
        self.assertEqual(struct.unpack("<I", error)[0], 49)
        self.vm.command(66, child)
        self.vm.command(66, directory)
        self.assertEqual(self.vm.directories, {b"A:\\", b"B:\\"})

    def test_list_empty_root_and_cursor_close(self):
        token, = struct.unpack("<I", self.vm.command(60, b"A:\\"))
        self.assertEqual(struct.unpack("<4I", self.vm.command(61, struct.pack("<I", token))), (1, 0, 0, 0))
        self.vm.command(62, struct.pack("<I", token))
        self.assertFalse(self.vm.cursors)
        self.assertEqual(struct.unpack("<3I", self.vm.command(48, b"B:\\")), (1, 0, 1))
        self.vm.add(self.path, b"data")
        token, = struct.unpack("<I", self.vm.command(60, b"A:/"))
        self.vm.command(61, struct.pack("<I", token))
        self.assertTrue(self.vm.cursors)
        self.vm.command(62, struct.pack("<I", token + 1), expect=7)
        self.assertTrue(self.vm.cursors)
        self.vm.command(62, struct.pack("<I", token))
        self.assertFalse(self.vm.cursors)

    def test_management_rejects_root_wrong_types_existing_destination_and_cross_volume(self):
        self.vm.command(65, b"A:\\dir")
        self.vm.add(self.path, b"old")
        for op in (63, 65, 66):
            self.vm.command(op, b"A:\\", expect=1)
        self.vm.command(63, b"A:\\dir", expect=16)
        self.vm.command(66, self.path, expect=15)
        self.vm.command(60, self.path, expect=15)
        self.vm.command(65, self.path, expect=14)
        self.vm.command(65, b"A:\\dir", expect=14)
        self.vm.command(64, struct.pack("<I", len(self.path)) + self.path + b"B:\\new", expect=18)
        self.vm.command(64, struct.pack("<I", len(self.path)) + self.path + b"A:\\dir", expect=14)
        self.vm.command(63, b"A:\\missing", expect=10)
        self.assertEqual(self.vm.file(self.path), b"old")

    def test_management_directory_rename_rejects_own_descendant(self):
        self.vm.command(65, b"A:\\dir")
        self.vm.command(65, b"A:\\dir\\child")
        self.vm.add(b"A:\\dir\\data", b"payload")
        for dst in (b"A:\\DIR\\child\\new", b"A:\\dir"):
            self.vm.command(64, struct.pack("<I", 6) + b"A:\\dir" + dst, expect=1)
        self.vm.command(64, struct.pack("<I", 6) + b"A:\\dir" + b"A:\\renamed")
        self.assertEqual(self.vm.file(b"A:\\renamed\\data"), b"payload")
        self.assertIn(b"A:\\RENAMED\\CHILD", self.vm.directories)

    def test_management_open_foreground_file_rejects_delete_and_rename(self):
        key = self.vm.add(self.path, b"old")
        foreign = self.vm.new_handle(key)
        self.vm.command(63, self.path, expect=8)
        self.vm.command(64, struct.pack("<I", len(self.path)) + self.path + b"A:\\new", expect=8)
        self.assertEqual(self.vm.file(self.path), b"old")
        self.assertEqual(list(self.vm.handles), [foreign])
        self.vm.close_handle(foreign)

    def test_list_busy_cancel_timeout_and_close_failure_retry(self):
        self.vm.add(self.path, b"old")
        token, = struct.unpack("<I", self.vm.command(60, b"A:\\"))
        self.vm.command(61, struct.pack("<I", token))
        self.assertTrue(self.vm.cursors)
        for op in (60, 63, 65, 66):
            self.vm.command(op, b"A:\\other", expect=2)
        self.vm.command(41, struct.pack("<II", 0, 0) + b"A:\\upload", expect=2)
        self.vm.call("rd_files_cancel", 12, self.vm.ticks)
        self.vm.fault = lambda vm, op, path: "fail" if op == "findclose" else None
        self.vm.call("rd_files_poll", self.vm.ticks)
        self.assertTrue(self.vm.call("rd_files_busy"))
        self.assertTrue(self.vm.cursors)
        self.vm.fault = None
        self.assertEqual(self.vm.drain()["state"], 7)
        token, = struct.unpack("<I", self.vm.command(60, b"A:\\"))
        self.vm.command(61, struct.pack("<I", token))
        self.vm.call("rd_files_poll", self.vm.ticks + 4000)
        result = self.vm.drain()
        self.assertEqual((result["state"], result["error"]), (7, 13))

    def test_list_gbk_trailing_backslash_directory_and_name(self):
        directory = b"A:\\folder\x81\x5c"
        self.vm.command(65, directory)
        self.vm.add(directory + b"\\file\x81\x5c", b"data")
        token, = struct.unpack("<I", self.vm.command(60, directory))
        reply = self.vm.command(61, struct.pack("<I", token))
        self.assertEqual(reply[16:], b"FILE\x81\x5c")
        self.vm.command(62, struct.pack("<I", token))
        self.vm.command(63, directory + b"\\file\x81\x5c")
        self.vm.command(66, directory)

    def test_management_preserves_recovery_transaction(self):
        self.vm.add(self.path, b"old")
        token = self.vm.upload(self.path, b"new")
        self.vm.fault = lambda vm, op, path: "fail" if op == "rename" and path.endswith((b".TMP", b".BAK")) else None
        self.assertEqual(self.vm.finish(token)["state"], 9)
        paths = self.vm.paths()
        for op in (60, 63, 64, 65, 66):
            self.vm.command(op, b"A:\\other", expect=9)
        self.assertEqual(self.vm.paths(), paths)

    def test_list_read_error_closes_cursor_and_does_not_report_eof(self):
        self.vm.add(self.path, b"old")
        token, = struct.unpack("<I", self.vm.command(60, b"A:\\"))
        self.vm.command(61, struct.pack("<I", token))
        self.vm.fault = lambda vm, op, path: "fail" if op == "findnext" else None
        payload = self.vm.command(61, struct.pack("<I", token), expect=4)
        self.assertEqual(struct.unpack("<I", payload)[0], 0xD0)
        self.assertEqual(self.vm.drain()["state"], 8)


if __name__ == "__main__":
    started = time.perf_counter()
    program = unittest.main(exit=False)
    report = dict(kind="compiled-MIPS-native-filesystem-model", passed=program.result.wasSuccessful(),
                  tests=program.result.testsRun, failures=len(program.result.failures),
                  errors=len(program.result.errors), elapsed_seconds=time.perf_counter()-started,
                  firmware_sha256=FIRMWARE_SHA,
                  source_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                 for name in ("h1-debug/files.c", "h1-debug/files.h", "tests/h1_debug_files.py")},
                  elf_sha256=hashlib.sha256((OUT / "files.elf").read_bytes()).hexdigest())
    (OUT / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    sys.exit(0 if report["passed"] else 1)
