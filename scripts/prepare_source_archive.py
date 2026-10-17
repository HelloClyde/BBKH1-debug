"""Restore SDK Git metadata from the bundled history in a release source ZIP.

This is only needed for release ZIPs. A normal recursive git clone already has
the pinned SDK checkout. Existing SDK Git repositories are never reset.
"""
from pathlib import Path
import hashlib,json,subprocess
ROOT=Path(__file__).resolve().parents[1]
SDK=ROOT/'vendor/h1-sdk'

def main():
    if (SDK/'.git').exists():
        head=subprocess.run(['git','-C',str(SDK),'rev-parse','HEAD'],capture_output=True,text=True)
        if head.returncode or head.stdout.strip()!='067fe072477861dfc8949d7b1a55279fb92d2548':
            raise RuntimeError('Existing SDK Git metadata is incomplete or uses another revision; extract the corrected source ZIP into a fresh directory')
        print('Pinned SDK Git metadata already exists; nothing changed.');return
    manifest=json.loads((ROOT/'vendor/h1-sdk.snapshot.json').read_text(encoding='utf-8'))
    for name,digest in manifest['files'].items():
        assert hashlib.sha256((SDK/name).read_bytes()).hexdigest()==digest,'SDK snapshot changed: '+name
    bundle=ROOT/'vendor/h1-sdk.bundle'
    assert hashlib.sha256(bundle.read_bytes()).hexdigest()==manifest['bundle_sha256']
    def run(*args):subprocess.run(['git','-C',str(SDK),*map(str,args)],check=True)
    run('init')
    run('fetch',bundle,'HEAD')
    fetched=subprocess.check_output(['git','-C',str(SDK),'rev-parse','FETCH_HEAD'],text=True).strip()
    assert fetched==manifest['commit'],'SDK bundle commit mismatch'
    # The checked files already match the snapshot; only establish HEAD/index.
    run('reset','--mixed',fetched)
    print('Restored pinned SDK Git metadata without downloading or replacing source files.')

if __name__=='__main__':main()
