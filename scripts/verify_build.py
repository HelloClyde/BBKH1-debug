"""Bind a reproducible release BDA to the existing exact-byte acceptance reports.

This does not run a physical H1 or a private firmware emulator in CI.
"""
from pathlib import Path
import hashlib,json,subprocess,sys

ROOT=Path(__file__).resolve().parents[1]
EXPECTED='bf2934e44ae4d3bdc191acb10f61ec7f3c8d5b866bb4d4f2042dd6479dc4d412'
SDK='067fe072477861dfc8949d7b1a55279fb92d2548'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def save(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def main():
    build=read(ROOT/'dist/H1Debug.build.json')
    assert sha(ROOT/'dist/H1Debug.bda')==build['sha256']==EXPECTED,'Release BDA differs from hardware-tested bytes'
    assert build['image_id']=='71a0983b' and build['sdk_commit']==SDK
    assert subprocess.check_output(['git','-C',str(ROOT/'vendor/h1-sdk'),'rev-parse','HEAD'],text=True).strip()==SDK
    for path,digest in build['source_sha256'].items():
        assert sha(ROOT/path)==digest, 'Build input changed: '+path
    evidence=ROOT/'docs/evidence/h1-debug-clock-usb-hardware-20261019.json'
    h=read(evidence)
    assert h['bda_sha256']==EXPECTED and h['image_id']==build['image_id']
    assert h['hardware_verified'] and h['full_firmware_verified'] is False
    assert h['source_sha256']==build['source_sha256'],'Acceptance source map differs'
    sys.path.insert(0,str(ROOT/'vendor/h1-sdk'))
    from h1_bda.validate import validate_bda
    assert validate_bda(ROOT/'dist/H1Debug.bda')['ok']
    build.pop('full_firmware_evidence',None)
    build.update(hardware_verified=True,full_firmware_verified=False,
                 verification_basis='Exact-byte match to targeted 2026-10-19 physical acceptance; CI does not run hardware/private firmware',
                 hardware_evidence=evidence.relative_to(ROOT).as_posix(),hardware_scope=h['scope'])
    save(ROOT/'dist/H1Debug.build.json',build)
    save(ROOT/'dist/H1Debug.verification.json',{'bda_sha256':EXPECTED,'image_id':build['image_id'],
        'hardware_verified':True,'full_firmware_verified':False,'physical_hardware_tested_in_this_build':False,
        'scope':h['scope'],
        'basis':build['verification_basis'],
        'evidence':[{'path':evidence.relative_to(ROOT).as_posix(),'sha256':sha(evidence)}]})
    print('PASS: exact tested BDA, SDK, source hashes and acceptance binding')

if __name__=='__main__':main()
