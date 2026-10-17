"""Execute the lossless screen encoder as MIPS, with independent byte checks."""
from pathlib import Path
import hashlib, json, random, struct, subprocess, sys, unittest
from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE

ROOT = Path(__file__).resolve().parents[1]
from h1_debug_io import VM
OUT = ROOT / 'build/h1-debug/screen-codec-test'

def decode(data, bpp):
    result = bytearray()
    cursor = 0
    while cursor < len(data):
        tag = data[cursor]
        cursor += 1
        if tag < 128:
            count = (tag + 1) * bpp
            assert cursor + count <= len(data)
            result.extend(data[cursor:cursor + count])
            cursor += count
        else:
            assert cursor + bpp <= len(data)
            result.extend(data[cursor:cursor + bpp] * ((tag & 127) + 2))
            cursor += bpp
    return bytes(result)

class CodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUT.mkdir(parents=True, exist_ok=True)
        cls.elf = OUT / 'codec.elf'
        args = [ROOT / '.tools/toolchain/bin/mipsel-none-elf-gcc.exe',
                '-EL', '-march=mips32', '-mabi=32', '-msoft-float', '-mno-abicalls',
                '-G0', '-fno-pic', '-O2', '-ffreestanding', '-fno-builtin',
                '-fno-stack-protector', '-Wall', '-Wextra', '-Werror', '-nostdlib',
                '-Wl,--build-id=none', '-Wl,-Ttext,0x82000000',
                '-Wl,-e,rd_screen_rle_encode', '-I', ROOT / 'h1-debug',
                '-o', cls.elf, ROOT / 'h1-debug/screen_codec.c', '-lgcc']
        subprocess.run(list(map(str, args)), check=True, capture_output=True)

    def setUp(self):
        self.vm = VM(self.elf)

    def encode(self, data, bpp, capacity=496, byte_count=None):
        vm = self.vm
        source, dest, consumed = 0x81000000, 0x81100000, 0x81200000
        vm.write(source - 16, b'\xAB' * 16 + data + b'\xCD' * 16)
        vm.write(dest - 16, b'\xEF' * (capacity + 32))
        vm.word(consumed, 0xDEADBEEF)
        reads, writes = [], []
        r = vm.u.hook_add(UC_HOOK_MEM_READ, lambda u, a, p, n, v, q: reads.append((p, n)),
                          begin=(source & 0x1fffffff) - 16,
                          end=(source & 0x1fffffff) + len(data) + 15)
        w = vm.u.hook_add(UC_HOOK_MEM_WRITE, lambda u, a, p, n, v, q: writes.append((p, n)),
                          begin=(dest & 0x1fffffff) - 16,
                          end=(dest & 0x1fffffff) + capacity + 15)
        try:
            count = vm.call('rd_screen_rle_encode', source,
                            len(data) if byte_count is None else byte_count,
                            bpp, dest, capacity, consumed)
        finally:
            vm.u.hook_del(r)
            vm.u.hook_del(w)
        self.assertLessEqual(count, capacity)
        self.assertTrue(all((source & 0x1fffffff) <= p and p + n <= (source & 0x1fffffff) + len(data) for p, n in reads))
        self.assertTrue(all((dest & 0x1fffffff) <= p and p + n <= (dest & 0x1fffffff) + capacity for p, n in writes))
        self.assertEqual(vm.read(dest - 16, 16), b'\xEF' * 16)
        self.assertEqual(vm.read(dest + capacity, 16), b'\xEF' * 16)
        self.assertEqual(vm.read(source, len(data)), data)
        return vm.read(dest, count), vm.word(consumed)

    def roundtrip(self, data, bpp, capacity=496):
        remaining, output, chunks = data, bytearray(), 0
        while remaining:
            encoded, consumed = self.encode(remaining[:16384], bpp, capacity)
            self.assertGreater(consumed, 0)
            self.assertEqual(consumed % bpp, 0)
            self.assertEqual(decode(encoded, bpp), remaining[:consumed])
            output.extend(decode(encoded, bpp))
            remaining = remaining[consumed:]
            chunks += 1
        self.assertEqual(bytes(output), data)
        return chunks

    def test_run_boundaries_and_unchanged_x_channel(self):
        for bpp in (2, 4):
            for run in (1, 2, 127, 128, 129, 130, 257):
                with self.subTest(bpp=bpp, run=run):
                    pixel = bytes(range(bpp))
                    data = pixel * run + bytes([255] * bpp) + pixel * run
                    self.roundtrip(data, bpp)
        # Equal RGB with different X is still different input, and must survive.
        self.roundtrip(b''.join(bytes((7, 8, 9, x)) for x in range(256)), 4)

    def test_random_and_alternating_are_lossless(self):
        random_source = random.Random(48272)
        for bpp in (2, 4):
            self.roundtrip(random_source.randbytes(4096), bpp)
            self.roundtrip((b'\x00' * bpp + b'\xFF' * bpp) * 400, bpp)

    def test_capacity_guards_and_prefix_progress(self):
        for bpp in (2, 4):
            data = b'\x12' * (bpp * 300) + bytes(range(64))
            for capacity in (0, 1, bpp, bpp + 1, 9, 17, 495, 496):
                with self.subTest(bpp=bpp, capacity=capacity):
                    encoded, consumed = self.encode(data, bpp, capacity)
                    if capacity < bpp + 1:
                        self.assertEqual((encoded, consumed), (b'', 0))
                    else:
                        self.assertGreater(consumed, 0)
                        self.assertEqual(decode(encoded, bpp), data[:consumed])

    def test_invalid_sizes_and_formats_do_not_access_pixels(self):
        for bpp, count in ((0, 4), (1, 4), (3, 4), (8, 8), (4, 3), (2, 3), (4, 0)):
            with self.subTest(bpp=bpp, count=count):
                self.assertEqual(self.encode(bytes(8), bpp, byte_count=count), (b'', 0))

    def test_full_native_resolution_flat_frame(self):
        data = b'\x00\x00\x00\xA7' * (480 * 272)
        chunks = self.roundtrip(data, 4)
        self.assertEqual(chunks, 32)

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CodecTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'passed': result.wasSuccessful(), 'tests': result.testsRun,
              'actual_compiled_MIPS': True, 'physical_hardware_tested': False,
              'source_sha256': hashlib.sha256((ROOT / 'h1-debug/screen_codec.c').read_bytes()).hexdigest(),
              'elf_sha256': hashlib.sha256((OUT / 'codec.elf').read_bytes()).hexdigest()}
    (OUT / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
    sys.exit(0 if result.wasSuccessful() else 1)
