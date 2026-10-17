"""Compare the launcher relocation format with independent GNU links.

Exercises low-half sign carry and jumps at two nontrivial heap addresses.
This tests emitted machine bytes, not merely a second copy of ELF parsing.
"""
from pathlib import Path
import json
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build/h1-debug'


def main():
    description = json.loads((BUILD / 'relocations.json').read_text())
    original = (BUILD / 'resident.bin').read_bytes()
    tc = ROOT / '.tools/toolchain/bin'
    # The build directory can retain objects whose indices changed when a
    # source file was added. Link exactly the inputs recorded by this build,
    # not every stale object matching a filename glob.
    objects = [Path(line[5:].strip()) for line in (BUILD / 'resident.map').read_text().splitlines()
               if line.startswith('LOAD ') and line.rstrip().endswith('.o')]
    assert objects and all(p.resolve().parent == BUILD.resolve() and p.name.startswith('resident-') for p in objects)
    results = []
    for base in (0x82007fe0, 0x8283ffe0):
        work = BUILD / f'relocate-{base:08x}'; work.mkdir(exist_ok=True)
        script = (ROOT / 'h1-debug/resident.ld').read_text().replace('0x81000000', hex(base)).replace('0x81200000', hex(base+0x200000))
        linker = work / 'resident.ld'; linker.write_text(script)
        elf, raw = work / 'resident.elf', work / 'resident.bin'
        subprocess.run([str(tc/'mipsel-none-elf-gcc.exe'), '-EL', '-march=mips32', '-mabi=32', '-msoft-float', '-mno-abicalls', '-G0', '-fno-pic',
                        '-nostdlib', '-Wl,--build-id=none', '-Wl,--gc-sections', f'-Wl,-T,{linker}', '-o', str(elf), *map(str,objects), '-lgcc'], check=True)
        subprocess.run([str(tc/'mipsel-none-elf-objcopy.exe'), '-O', 'binary', str(elf), str(raw)], check=True)
        actual = bytearray(original)
        for offset, kind, value in description['records']:
            assert description['base'] <= value <= description['base']+description['memory_size']
            relocated = (value+base-description['base']) & 0xffffffff
            word, = struct.unpack_from('<I', actual, offset)
            if kind == 1: word = relocated
            elif kind == 2: word = (word & 0xfc000000) | ((relocated >> 2) & 0x3ffffff)
            elif kind == 3: word = (word & 0xffff0000) | (((relocated+0x8000) >> 16) & 0xffff)
            elif kind == 4: word = (word & 0xffff0000) | (relocated & 0xffff)
            else: raise AssertionError(kind)
            struct.pack_into('<I', actual, offset, word)
        expected = raw.read_bytes()
        differences = [(i, actual[i], expected[i]) for i in range(min(len(actual),len(expected))) if actual[i] != expected[i]]
        assert len(actual) == len(expected) and not differences, differences[:20]
        results.append(dict(base=hex(base), bytes=len(actual), exact_gnu_link_match=True))
    (BUILD / 'relocation-validation.json').write_text(json.dumps(dict(passed=True, cases=results), indent=2)+'\n')
    print('PASS: relocated resident image exactly matches 2 independent GNU links (HI16 carry, LO16, J26, pointers)')


if __name__ == '__main__': main()
