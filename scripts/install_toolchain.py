"""Install the pinned Windows GNU MIPS toolchain used by H1Debug acceptance."""
from pathlib import Path
import argparse, hashlib, shutil, subprocess, urllib.request, zipfile

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://static.grumpycoder.net/pixel/mips/g++-mipsel-none-elf-15.2.0.zip'
SHA256 = '8ba866e25c9826ee04ab4310365d264e3e73769e3738bb58ae38fd6740b7ee8d'

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dest',type=Path,default=ROOT/'.tools/toolchain')
    a=p.parse_args();dest=a.dest.resolve();dest.mkdir(parents=True,exist_ok=True)
    archive=dest/'g++-mipsel-none-elf-15.2.0.zip'
    if not archive.exists():
        print('Downloading pinned GCC 15.2.0',flush=True)
        req=urllib.request.Request(URL,headers={'User-Agent':'BBKH1-debug/1.0'})
        partial=archive.with_suffix('.part')
        with urllib.request.urlopen(req,timeout=120) as source,partial.open('wb') as target:
            shutil.copyfileobj(source,target)
        partial.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=SHA256:
        raise RuntimeError('Toolchain archive SHA256 mismatch: '+str(archive))
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            target=(dest/entry.filename).resolve()
            if target!=dest and dest not in target.parents:raise RuntimeError('Unsafe archive path')
        z.extractall(dest)
    version=subprocess.check_output([str(dest/'bin/mipsel-none-elf-gcc.exe'),'--version'],text=True).splitlines()[0]
    if not version.endswith('15.2.0'):raise RuntimeError(version)
    for name in ('gcc','nm','objcopy'):
        if not (dest/f'bin/mipsel-none-elf-{name}.exe').is_file():raise RuntimeError('Missing '+name)
    print(version)

if __name__=='__main__':main()
