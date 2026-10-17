"""Extract the real source asset, restore its SDK offline, and rebuild the BDA."""
from pathlib import Path
import hashlib,os,subprocess,sys,tempfile,zipfile

ROOT=Path(__file__).resolve().parents[1]

def main():
    version=(ROOT/'VERSION').read_text().strip()
    archive=ROOT/f'dist/release/H1Debug-v{version}-source.zip'
    parent=ROOT/'build';parent.mkdir(exist_ok=True)
    # Preserve this build evidence directory, including failures.
    directory=Path(tempfile.mkdtemp(prefix='source-package-',dir=parent))
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            target=(directory/name).resolve()
            assert directory.resolve() in target.parents,'Invalid source archive path'
        z.extractall(directory)
    checkout=directory/f'BBKH1-debug-v{version}'
    env=os.environ.copy();env['GIT_ALLOW_PROTOCOL']='file'
    def run(*args):
        subprocess.run([sys.executable,*map(str,args)],cwd=checkout,env=env,check=True)
    run('scripts/prepare_source_archive.py')
    run('tools/build_h1_debug.py','--toolchain',ROOT/'.tools/toolchain/bin')
    run('scripts/verify_build.py')
    def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
    assert sha(checkout/'dist/H1Debug.bda')==sha(ROOT/'dist/H1Debug.bda')
    print('PASS: actual source ZIP restores SDK without network and reproduces the tested BDA')

if __name__=='__main__':main()
