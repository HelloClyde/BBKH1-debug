"""Create explicit reproducible BDA, install, skill and complete-source assets."""
from pathlib import Path
import argparse,hashlib,json,re,shutil,subprocess,zipfile

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'dist/release'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def git(*args,cwd=ROOT):return subprocess.check_output(['git',*args],cwd=cwd)
def tracked(cwd):return [p.decode('utf-8') for p in git('ls-files','-z',cwd=cwd).split(b'\0') if p]

def zipfiles(path,items):
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name,source in sorted(items):
            info=zipfile.ZipInfo(name,(2026,10,17,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644<<16
            z.writestr(info,source.read_bytes())

def verify(tag=None,commit=None):
    manifest=json.loads((OUT/'release-manifest.json').read_text(encoding='utf-8'))
    if tag:assert manifest['tag']==tag
    if commit:assert manifest['commit']==commit
    for row in manifest['assets']:
        path=OUT/row['name'];assert path.stat().st_size==row['size'] and sha(path)==row['sha256'],path
    with zipfile.ZipFile(OUT/'H1Debug-install.zip') as z:
        assert hashlib.sha256(z.read('应用/程序/H1Debug.bda')).hexdigest()==manifest['bda_sha256']
    with zipfile.ZipFile(OUT/'h1-debug-skill.zip') as z:
        assert 'h1-debug/SKILL.md' in z.namelist() and 'h1-debug/references/deployment.md' in z.namelist()
    with zipfile.ZipFile(OUT/f"H1Debug-{manifest['tag']}-source.zip") as z:
        names=z.namelist();assert any(n.endswith('/vendor/h1-sdk/h1_bda/header.py') for n in names)
        assert not any(n.endswith(('.raw','project.bin')) or '/.tools/' in n or '/.git/' in n for n in names)
    print('PASS: release assets, BDA, skill and complete SDK source')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--tag');p.add_argument('--commit');p.add_argument('--verify',action='store_true')
    a=p.parse_args()
    if a.verify:return verify(a.tag,a.commit)
    version=(ROOT/'VERSION').read_text().strip();tag='v'+version
    assert re.fullmatch(r'v\d+\.\d+\.\d+',tag)
    if a.tag:assert a.tag==tag,'Tag differs from VERSION'
    commit=git('rev-parse','HEAD').decode().strip()
    if a.commit:assert a.commit==commit
    OUT.mkdir(parents=True,exist_ok=True)
    expected={'H1Debug.bda','H1Debug.build.json','H1Debug.verification.json','H1Debug-install.zip',
              'h1-debug-skill.zip',f'H1Debug-{tag}-source.zip','RELEASE-NOTES.md','release-manifest.json','SHA256SUMS.txt'}
    assert not any(p.name not in expected for p in OUT.iterdir()),'Unexpected release directory contents'
    for name in ('H1Debug.bda','H1Debug.build.json','H1Debug.verification.json'):shutil.copy2(ROOT/'dist'/name,OUT/name)
    zipfiles(OUT/'H1Debug-install.zip',[
      ('应用/程序/H1Debug.bda',ROOT/'dist/H1Debug.bda'),('INSTALL.md',ROOT/'INSTALL.md'),
      ('LICENSE',ROOT/'LICENSE'),('NOTICE.md',ROOT/'NOTICE.md'),
      ('licenses/SDK-LICENSE',ROOT/'vendor/h1-sdk/LICENSE'),('licenses/SDK-NOTICE',ROOT/'vendor/h1-sdk/NOTICE')])
    skill=ROOT/'skills/h1-debug'
    zipfiles(OUT/'h1-debug-skill.zip',[(f'h1-debug/{p.relative_to(skill).as_posix()}',p) for p in skill.rglob('*') if p.is_file()]+[('LICENSE',ROOT/'LICENSE')])
    prefix=f'BBKH1-debug-{tag}/';items=[]
    for name in tracked(ROOT):
        path=ROOT/name
        if path.is_file():items.append((prefix+name,path))
    sdk=ROOT/'vendor/h1-sdk'
    sdk_files=tracked(sdk)
    items += [(prefix+'vendor/h1-sdk/'+name,sdk/name) for name in sdk_files]
    bundle=ROOT/'build/h1-sdk.bundle'
    git('bundle','create',str(bundle),'HEAD',cwd=sdk)
    snapshot=ROOT/'build/h1-sdk.snapshot.json'
    snapshot.write_text(json.dumps({'commit':git('rev-parse','HEAD',cwd=sdk).decode().strip(),
        'bundle_sha256':sha(bundle),'files':{name:sha(sdk/name) for name in sdk_files}},indent=2)+'\n',encoding='utf-8')
    items += [(prefix+'vendor/h1-sdk.bundle',bundle),(prefix+'vendor/h1-sdk.snapshot.json',snapshot)]
    zipfiles(OUT/f'H1Debug-{tag}-source.zip',items)
    shutil.copy2(ROOT/'docs/release-v1.0.0.md',OUT/'RELEASE-NOTES.md')
    assets=[{'name':p.name,'size':p.stat().st_size,'sha256':sha(p)} for p in sorted(OUT.iterdir()) if p.name not in ('release-manifest.json','SHA256SUMS.txt')]
    manifest={'tag':tag,'commit':commit,'sdk_commit':git('rev-parse','HEAD',cwd=sdk).decode().strip(),
              'bda_sha256':sha(OUT/'H1Debug.bda'),'assets':assets}
    (OUT/'release-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    sums=[f'{sha(p)}  {p.name}\n' for p in sorted(OUT.iterdir()) if p.name!='SHA256SUMS.txt']
    (OUT/'SHA256SUMS.txt').write_text(''.join(sums),encoding='ascii')
    verify(tag,commit)

if __name__=='__main__':main()
