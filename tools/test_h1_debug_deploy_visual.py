"""Capture settled desktop metadata and real V1/V2 execution over one USB session."""
from pathlib import Path
import argparse,json,time
import h1_debug as host
import test_h1_debug_hardware as hw
from test_h1_debug_deploy_hardware import ROOT,wait_menu,marker

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,required=True)
    a=p.parse_args();d=a.directory;d.mkdir(parents=True,exist_ok=True)
    probes=json.loads((ROOT/'build/h1-debug-update-probes/probes.json').read_text(encoding='utf-8'))
    target='A:\\应用\\程序\\H1Visual'+time.strftime('%H%M%S')+'.bda'
    report={'target':target,'completed':False,'versions':[]}
    save=lambda:hw.save_json(d/'visual-verification.json',report)
    with hw.connect(None,8,d/'wire.jsonl') as c:
        report['initial']=hw.probe(c,d);assert c.desktop_status()['menu_idle']
        assert not c.file_stat(target)['exists'];save()
        for probe in probes:
            v=probe['version'];print(f'Desktop title and launch V{v}',flush=True)
            row={'version':v};report['versions'].append(row);save()
            row['upload_refresh']=c.deploy(probe['path'],target,run=False,receipt_path=d/f'deploy-v{v}.json')
            time.sleep(.7)
            row['desktop_screen']=c.screenshot(d/f'desktop-v{v}.png')
            row['run']=c.desktop_run(target)
            time.sleep(.3);row['identity']=marker(c,probe)
            row['running_screen']=c.screenshot(d/f'running-v{v}.png')
            c.press('ESCAPE',ticks=8);row['returned']=wait_menu(c);time.sleep(.4);save()
        report['delete']=c.file_delete(target);assert not c.file_stat(target)['exists']
        report['refresh_after_delete']=c.desktop_refresh();time.sleep(.7)
        report['deleted_screen']=c.screenshot(d/'desktop-after-delete.png')
        report['final']=c.status()
        assert report['final']['usb_resets']==report['initial']['status']['usb_resets']
        report['completed']=True;save()
    print(json.dumps({'ok':True,'report':str((d/'visual-verification.json').resolve())}))

if __name__=='__main__':main()
