"""Build a relocatable heap-resident H1 debugger and its BDA toggle launcher."""
from pathlib import Path
import argparse
import hashlib
import json
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / 'vendor/h1-sdk'
SOURCE = ROOT / 'h1-debug'
ICON = SOURCE / 'assets/icon.png'
BUILD = ROOT / 'build/h1-debug'
LINK_BASE = 0x81000000
SDK_COMMIT = '067fe072477861dfc8949d7b1a55279fb92d2548'


def run(args):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    if result.stderr:
        print(result.stderr, end='')
    return result.stdout


def relocation_records(elf_path, raw):
    """Convert GNU --emit-relocs records, rejecting unhandled MIPS relocations.

    HI16 uses the corresponding signed LO16, including carry, not merely a
    guessed difference between two links. PC-relative branches don't relocate.
    Absolute firmware/MMIO constants have SHN_ABS or no ELF relocation.
    """
    from elftools.elf.elffile import ELFFile
    from elftools.elf.relocation import RelocationSection
    records = []
    with elf_path.open('rb') as stream:
        elf = ELFFile(stream)
        symbols = elf.get_section_by_name('.symtab')
        values = {s.name: s['st_value'] for s in symbols.iter_symbols()}
        for section in elf.iter_sections():
            if not isinstance(section, RelocationSection):
                continue
            target = elf.get_section(section['sh_info'])
            if not target['sh_flags'] & 2:
                continue
            symtab = elf.get_section(section['sh_link'])
            pending = {}
            for rel in section.iter_relocations():
                kind, index = rel['r_info_type'], rel['r_info_sym']
                symbol = symtab.get_symbol(index)
                shndx = symbol['st_shndx']
                if shndx == 'SHN_ABS':
                    continue
                if not isinstance(shndx, int) or not elf.get_section(shndx)['sh_flags'] & 2:
                    raise ValueError(f'Unresolved/nonallocated relocation: {symbol.name} {shndx}')
                offset = rel['r_offset'] - LINK_BASE
                if offset < 0 or offset + 4 > len(raw) or offset % 4:
                    raise ValueError('Relocation outside resident image')
                word = struct.unpack_from('<I', raw, offset)[0]
                if kind == 2:  # R_MIPS_32
                    records.append((offset, 1, word))
                elif kind == 4:  # R_MIPS_26
                    records.append((offset, 2, ((word & 0x3ffffff) << 2) | ((rel['r_offset'] + 4) & 0xf0000000)))
                elif kind == 5:  # R_MIPS_HI16
                    pending.setdefault(index, []).append((offset, word & 0xffff))
                elif kind == 6:  # R_MIPS_LO16
                    lo = word & 0xffff
                    signed = lo if lo < 0x8000 else lo - 0x10000
                    for high_offset, high in pending.pop(index, []):
                        records.append((high_offset, 3, ((high << 16) + signed) & 0xffffffff))
                    # Only low 16 bits matter for this instruction. Encode an
                    # in-image representative so loader bounds stay strict.
                    records.append((offset, 4, LINK_BASE + lo))
                elif kind in (0, 10):  # NONE / PC16 (both ends move together)
                    continue
                else:
                    raise ValueError(f'Unsupported MIPS relocation {kind} at {offset:#x}')
            if pending:
                raise ValueError('Unpaired HI16 relocations')
    if len({x[0] for x in records}) != len(records):
        raise ValueError('Duplicate resident relocation')
    return sorted(records), values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain', type=Path, default=ROOT / '.tools/toolchain/bin')
    args = parser.parse_args()
    if run(['git', '-C', SDK, 'rev-parse', 'HEAD']).strip() != SDK_COMMIT:
        raise RuntimeError('SDK revision mismatch')
    BUILD.mkdir(parents=True, exist_ok=True)
    tc = args.toolchain.resolve()
    gcc, objcopy = tc / 'mipsel-none-elf-gcc.exe', tc / 'mipsel-none-elf-objcopy.exe'
    resident_sources = [SOURCE / n for n in ('resident.c', 'firmware.c', 'io.c', 'usb.c', 'screen_codec.c', 'files.c', 'shell.c')]
    resident_sources += [ROOT / 'experiments/h1-usb-debug/protocol.c',
                         ROOT / 'experiments/h1-usb-runner/native_exec.c',
                         ROOT / 'experiments/h1-usb-runner/trampoline.S']
    dependencies = [ROOT / 'experiments/h1-usb-debug/protocol.h', ROOT / 'experiments/h1-usb-runner/module_abi.h',
                    ROOT / 'experiments/h1-usb-runner/native_exec.h', ROOT / 'src/runtime/entry.S',
                    ROOT / 'src/runtime/startup.c', ROOT / 'src/runtime/h1.ld'] + list((SDK / 'sdk/include').glob('*.h'))
    inputs = sorted(set(resident_sources + dependencies + list(SOURCE.glob('*.h')) + list(SOURCE.glob('*.ld')) + [SOURCE / 'launcher.c', Path(__file__), ICON]))
    source_hashes = {str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    image_id = int(hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest()[:8], 16)
    flags = ['-EL', '-march=mips32', '-mabi=32', '-msoft-float', '-mno-abicalls', '-G0', '-fno-pic',
             '-O2', '-ffreestanding', '-fno-builtin', '-fno-stack-protector', '-ffunction-sections',
             '-fdata-sections', '-fno-strict-aliasing', '-g0', '-Wall', '-Wextra', '-Werror',
             '-Wno-misleading-indentation', f'-ffile-prefix-map={ROOT}=h1-debug', f'-DRD_BUILD_ID=0x{image_id:08x}u',
             '-I', SOURCE, '-I', BUILD, '-I', SDK / 'sdk/include']

    def compile_link(sources, name, script, reloc=False):
        objects = []
        for i, src in enumerate(sources):
            obj = BUILD / f'{name}-{i}-{src.stem}.o'
            run([gcc, *flags, '-c', src, '-o', obj]); objects.append(obj)
        elf = BUILD / (name + '.elf')
        raw = BUILD / (name + '.bin')
        run([gcc, *flags, '-nostdlib', '-Wl,--build-id=none', '-Wl,--gc-sections',
             *(['-Wl,--emit-relocs'] if reloc else []), f'-Wl,-T,{script}',
             f'-Wl,-Map,{BUILD / (name + ".map")}', '-o', elf, *objects, '-lgcc'])
        run([objcopy, '-O', 'binary', elf, raw])
        return elf, raw.read_bytes()

    elf, resident = compile_link(resident_sources, 'resident', SOURCE / 'resident.ld', True)
    records, symbols = relocation_records(elf, resident)
    memory_size = symbols['__bss_end'] - LINK_BASE
    header = ['/* Generated by tools/build_h1_debug.py; do not edit. */', '#include <stdint.h>',
              f'#define RD_IMAGE_LINK_BASE 0x{LINK_BASE:08x}u', f'#define RD_IMAGE_SIZE {len(resident)}u',
              f'#define RD_MEMORY_SIZE {memory_size}u', f'#define RD_IMAGE_ID 0x{image_id:08x}u',
              f'#define RD_RELOC_COUNT {len(records)}u', 'static const uint8_t rd_image[] = {']
    header += [','.join(f'0x{x:02x}' for x in resident[i:i+24]) + ',' for i in range(0, len(resident), 24)]
    header += ['};', 'struct rd_reloc { uint32_t offset,type,value; };', 'static const struct rd_reloc rd_relocations[] = {']
    header += ['{0x%x,%d,0x%08x},' % r for r in records] + ['};']
    (BUILD / 'resident_image.h').write_text('\n'.join(header) + '\n', encoding='ascii')
    _, payload = compile_link([ROOT / 'src/runtime/entry.S', ROOT / 'src/runtime/startup.c',
                               SOURCE / 'launcher.c', SOURCE / 'firmware.c'], 'H1Debug', ROOT / 'src/runtime/h1.ld')
    sys.path.insert(0, str(SDK))
    from h1_bda.header import HeaderFields, encode_header
    from h1_bda.resources import PAYLOAD_OFFSET, RESOURCE_OFFSET, RESOURCE_SIZES, build_icon_resources
    from h1_bda.validate import validate_bda
    size = PAYLOAD_OFFSET + len(payload)
    padding = (-size) & 3
    fields = HeaderFields(category=0x47, file_size_minus_4=size+padding-4, payload_offset=PAYLOAD_OFFSET,
                          resource_offset=RESOURCE_OFFSET, resource_sizes=RESOURCE_SIZES)
    resources = build_icon_resources(ICON)
    data = encode_header(fields, title='H1调试', build_time='2026-10-16 00:00:00') + resources + payload + bytes(padding)
    dest = ROOT / 'dist/H1Debug.bda'; dest.parent.mkdir(exist_ok=True); dest.write_bytes(data)
    validation = validate_bda(dest)
    if not validation['ok']:
        raise RuntimeError(validation)
    digest = hashlib.sha256(data).hexdigest()
    report = dict(version='resident-debug-usb-clock-recovery-r1', image_id=f'{image_id:08x}', sha256=digest, size=len(data),
                  resident_image_bytes=len(resident), resident_memory_bytes=memory_size, relocations=len(records),
                  sdk_commit=SDK_COMMIT, firmware='H1/Y100 V1.41', hardware_verified=False,
                  full_firmware_verified=False, forced_preemption=False, raw_tick_hz=None,
                  stopped_memory_policy='Retained allocation and dormant task reused until reboot; queued key pointers must stay valid.',
                  source_sha256=source_hashes, symbols=symbols,
                  icon_source=str(ICON.relative_to(ROOT)).replace('\\', '/'),
                  icon_sha256=source_hashes[str(ICON.relative_to(ROOT)).replace('\\', '/')],
                  icon_resources_sha256=hashlib.sha256(resources).hexdigest())
    dest.with_suffix('.build.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    # Never leave a previous binary's successful acceptance report beside a
    # newly built BDA. Verification is rebound to this exact digest after tests.
    verification = dict(bda_sha256=digest, hardware_verified=False,
                        full_firmware_verified=False, status='built; verification pending')
    dest.with_suffix('.verification.json').write_text(json.dumps(verification, indent=2)+'\n', encoding='utf-8')
    dest.with_suffix('.bda.sha256').write_text(f'{digest}  {dest.name}\n', encoding='ascii')
    (BUILD / 'relocations.json').write_text(json.dumps(dict(base=LINK_BASE, memory_size=memory_size, records=records, symbols=symbols), indent=2)+'\n')
    print(f'PASS {dest}: {len(data)} bytes; resident RAM {memory_size}; {len(records)} relocations\nSHA256 {digest}')


if __name__ == '__main__':
    main()
