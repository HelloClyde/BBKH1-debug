"""Bind a reproducible release BDA to the existing exact-byte acceptance reports.

This does not run a physical H1 or a private firmware emulator in CI.
"""
from pathlib import Path
import hashlib,json,subprocess,sys

ROOT=Path(__file__).resolve().parents[1]
EXPECTED='4214dbc11266c59359dac38d3ad6386c3d727e0f534a6520e952c5872cbd8b3d'
SDK='067fe072477861dfc8949d7b1a55279fb92d2548'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def save(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def main():
    build=read(ROOT/'dist/H1Debug.build.json')
    assert sha(ROOT/'dist/H1Debug.bda')==build['sha256']==EXPECTED,'Release BDA differs from hardware-tested bytes'
    assert build['image_id']=='7195598f' and build['sdk_commit']==SDK
    assert subprocess.check_output(['git','-C',str(ROOT/'vendor/h1-sdk'),'rev-parse','HEAD'],text=True).strip()==SDK
    for path,digest in build['source_sha256'].items():
        assert sha(ROOT/path)==digest, 'Build input changed: '+path
    evidence=ROOT/'docs/evidence/h1-debug-deploy-hardware-20261017.json'
    native=ROOT/'docs/evidence/h1-debug-deploy-offline-verification.json'
    h,n=read(evidence),read(native)
    assert h['bda_sha256']==n['bda_sha256']==EXPECTED and h['image_id']==n['image_id']==build['image_id']
    assert h['hardware_verified'] and n['full_firmware']['verified']
    assert n['source_sha256']==build['source_sha256'],'Acceptance source map differs'
    sys.path.insert(0,str(ROOT/'vendor/h1-sdk'))
    from h1_bda.validate import validate_bda
    assert validate_bda(ROOT/'dist/H1Debug.bda')['ok']
    build.update(hardware_verified=True,full_firmware_verified=True,
                 verification_basis='Exact-byte match to existing 2026-10-17 acceptance; CI does not run hardware/private firmware',
                 hardware_evidence=evidence.relative_to(ROOT).as_posix(),full_firmware_evidence=native.relative_to(ROOT).as_posix())
    save(ROOT/'dist/H1Debug.build.json',build)
    save(ROOT/'dist/H1Debug.verification.json',{'bda_sha256':EXPECTED,'image_id':build['image_id'],
        'hardware_verified':True,'full_firmware_verified':True,'physical_hardware_tested_in_this_build':False,
        'basis':build['verification_basis'],
        'evidence':[{'path':p.relative_to(ROOT).as_posix(),'sha256':sha(p)} for p in (evidence,native)]})
    print('PASS: exact tested BDA, SDK, source hashes and acceptance binding')

if __name__=='__main__':main()
