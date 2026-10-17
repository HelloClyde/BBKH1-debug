"""Build V1/V2 normal BDAs for end-to-end debugger file/deploy acceptance.

Only writes a local output directory. Does not install or access a device.
"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def build(directory):
    sys.path.insert(0, str(ROOT / 'vendor/h1-sdk'))
    from h1_bda.header import HeaderFields, encode_header
    from h1_bda.resources import PAYLOAD_OFFSET, RESOURCE_OFFSET, RESOURCE_SIZES, build_icon_resources
    from h1_bda.validate import validate_bda
    directory.mkdir(parents=True, exist_ok=True)
    tc = ROOT / '.tools/toolchain/bin'
    flags = ['-EL', '-march=mips32', '-mabi=32', '-msoft-float', '-mno-abicalls',
             '-G0', '-fno-pic', '-O2', '-ffreestanding', '-fno-builtin',
             '-fno-stack-protector', '-ffunction-sections', '-fdata-sections',
             '-Wall', '-Wextra', '-Werror', '-I', ROOT / 'vendor/h1-sdk/sdk/include']
    sources = [ROOT / 'src/runtime/entry.S', ROOT / 'src/runtime/startup.c',
               ROOT / 'h1-debug/examples/update_probe.c']
    resources = build_icon_resources(ROOT / 'h1-debug/assets/icon.png')

    def run(args):
        result = subprocess.run(list(map(str,args)), capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return result.stdout

    reports = []
    for version in (1,2):
        work = directory / f'v{version}'
        work.mkdir(exist_ok=True)
        objects = []
        for index,source in enumerate(sources):
            obj = work / f'{index}-{source.stem}.o'
            run([tc/'mipsel-none-elf-gcc.exe', *flags, f'-DUPDATE_PROBE_VERSION={version}',
                 '-c', source, '-o', obj])
            objects.append(obj)
        elf, raw = work/'probe.elf', work/'probe.bin'
        run([tc/'mipsel-none-elf-gcc.exe', *flags, '-nostdlib', '-Wl,--build-id=none',
             '-Wl,--gc-sections', f'-Wl,-T,{ROOT / "src/runtime/h1.ld"}',
             f'-Wl,-Map,{work/"probe.map"}', '-o', elf, *objects, '-lgcc'])
        run([tc/'mipsel-none-elf-objcopy.exe', '-O','binary',elf,raw])
        symbols = {fields[2]:int(fields[0],16) for line in run([tc/'mipsel-none-elf-nm.exe',elf]).splitlines()
                   if len(fields:=line.split())==3}
        data = raw.read_bytes()
        padding = (-(PAYLOAD_OFFSET+len(data))) & 3
        fields = HeaderFields(category=0x47, file_size_minus_4=PAYLOAD_OFFSET+len(data)+padding-4,
                              payload_offset=PAYLOAD_OFFSET, resource_offset=RESOURCE_OFFSET,
                              resource_sizes=RESOURCE_SIZES)
        package = encode_header(fields,title=f'USB测试{version}',build_time='2026-10-16 00:00:00') + resources + data + bytes(padding)
        path = directory / f'H1UpdateProbe-v{version}.bda'
        path.write_bytes(package)
        assert validate_bda(path)['ok']
        reports.append({'version':version, 'path':str(path.resolve()), 'bytes':len(package),
                        'sha256':hashlib.sha256(package).hexdigest(),
                        'identity_address':symbols['update_probe_identity'],
                        'identity_magic':0x31504455, 'UI':'large white version digit; yellow moving bar; Escape/Back exits'})
    (directory/'probes.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return reports


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=ROOT/'build/h1-debug-update-probes')
    args=parser.parse_args()
    print(json.dumps(build(args.directory),ensure_ascii=False,indent=2))
