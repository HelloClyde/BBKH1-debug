"""Exercise file management and V1 -> V2 full-BDA deployment in one USB session.

Creates only uniquely named test files, verifies their content, then removes
them through the new management API. Leaves the debugger running for explicit
close acceptance. Uses existing owned-allocation memory and EXEC probes.
"""
from pathlib import Path
import argparse,hashlib,json,struct,time
import h1_debug as host
import test_h1_debug_hardware as hw

ROOT=Path(__file__).resolve().parents[1]

def wait_menu(client,seconds=15):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        state=client.desktop_status()
        if state['menu_idle']:return state
        time.sleep(.05)
    raise TimeoutError('Foreground did not return to the desktop')

def marker(client,probe):
    values=struct.unpack('<4I',client.read_memory(probe['identity_address'],16))
    assert values[:2]==(probe['identity_magic'],probe['version']),values
    return list(values)

def file_management(client,directory,device_directory):
    directory.mkdir(parents=True,exist_ok=True)
    assert not client.file_stat(device_directory)['exists']
    result={'directory':device_directory,'actions':[],'completed':False}
    def act(name,value):
        result['actions'].append({'name':name,'result':value})
        hw.save_json(directory/'management.json',result)
    hw.save_json(directory/'management.json',result)
    act('mkdir',client.file_mkdir(device_directory))
    child=device_directory+'\\子目录';renamed=device_directory+'\\改名目录'
    act('mkdir_child',client.file_mkdir(child))
    content=bytes((i*73+19)&255 for i in range(65537))
    source=directory/'payload.bin';source.write_bytes(content)
    target=child+'\\测试数据.bin'
    act('put',client.file_put(source,target,receipt_path=directory/'put.json'))
    listing=client.file_list(child)
    assert any(e['name']=='测试数据.bin' and e['size']==len(content) for e in listing['entries'])
    act('list',listing)
    try:client.file_rmdir(child)
    except host.DeviceError as error:
        assert error.status==17,(error.status,error.payload)
        act('nonempty_rmdir_refused',{'status':error.status})
    else:raise AssertionError('Nonempty directory was not refused')
    act('rename_directory',client.file_rename(child,renamed))
    old=renamed+'\\测试数据.bin';new=renamed+'\\新数据.bin'
    act('rename_file',client.file_rename(old,new))
    downloaded=directory/'readback.bin';act('get',client.file_get(new,downloaded))
    assert downloaded.read_bytes()==content
    empty=directory/'empty.bin';empty.write_bytes(b'')
    empty_target=renamed+'\\空文件.bin'
    act('put_empty',client.file_put(empty,empty_target,receipt_path=directory/'empty-put.json'))
    act('get_empty',client.file_get(empty_target,directory/'empty-readback.bin'))
    assert (directory/'empty-readback.bin').read_bytes()==b''
    act('delete_file',client.file_delete(new));assert not client.file_stat(new)['exists']
    act('delete_empty',client.file_delete(empty_target))
    act('empty_list',client.file_list(renamed));assert not result['actions'][-1]['result']['entries']
    act('rmdir_child',client.file_rmdir(renamed))
    act('rmdir_parent',client.file_rmdir(device_directory))
    assert not client.file_stat(device_directory)['exists']
    result['completed']=True;hw.save_json(directory/'management.json',result);return result

def acceptance(client,directory,*,capture=None,background=True,physical=True):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    capture=capture or (lambda name:client.screenshot(directory/(name+'.png')))
    build=json.loads((ROOT/'dist/H1Debug.build.json').read_text(encoding='utf-8'))
    probes=json.loads((ROOT/'build/h1-debug-update-probes/probes.json').read_text(encoding='utf-8'))
    run_id=time.strftime('%m%d%H%M%S')
    target='A:\\应用\\程序\\H1T'+run_id+'.bda'
    device_directory='A:\\H1T'+run_id
    report={'bda_sha256':build['sha256'],'physical_hardware_tested':physical,
            'one_client_session':True,'target':target,'versions':[],'completed':False}
    save=lambda:hw.save_json(directory/'acceptance.json',report)
    report['initial']=hw.probe(client,directory);save()
    assert report['initial']['caps']['features']&0xe00==0xe00
    assert client.desktop_status()['menu_idle'],'Start from the ordinary desktop'
    assert not client.file_stat(target)['exists']
    report['before_screen']=capture('before-menu');save()
    for probe in probes:
        version=probe['version'];print(f'Uploading and starting complete BDA V{version}',flush=True)
        row={'version':version,'sha256':probe['sha256']};report['versions'].append(row);save()
        row['deploy']=client.deploy(probe['path'],target,receipt_path=directory/f'deploy-v{version}.json')
        row['before']=marker(client,probe)
        time.sleep(.35);row['advanced']=marker(client,probe)
        assert row['advanced'][2]!=row['before'][2]
        row['screen']=capture(f'running-v{version}')
        row['download']=client.file_get(target,directory/f'installed-v{version}.bda')
        assert row['download']['sha256']==probe['sha256']
        if version==1:
            before=marker(client,probe)
            report['file_management']=file_management(client,directory/'management',device_directory)
            after=marker(client,probe);assert after[2]!=before[2]
            report['foreground_during_management']={'before':before,'after':after}
        if version==2 and background:
            for stage,function in [('compute',hw.compute),('memory',hw.memory)]:
                print('Background '+stage+' while V2 remains running',flush=True)
                work=directory/stage;work.mkdir(exist_ok=True)
                report[stage]=function(client,work,directory/'memory-ownership.json');save()
            row['after_background']=marker(client,probe)
            assert row['after_background'][2]!=row['advanced'][2]
        try:client.desktop_refresh(wait_seconds=1)
        except RuntimeError:row['active_refresh_refused']=True
        else:raise AssertionError('Refresh must refuse to replace a running frontend')
        row['exit_key']=client.press('ESCAPE',ticks=8)
        row['returned']=wait_menu(client)
        time.sleep(.5)  # menu_idle precedes the queued paint completing.
        row['menu_screen']=capture(f'menu-v{version}');save()
    report['delete_bda']=client.file_delete(target)
    assert not client.file_stat(target)['exists']
    report['final_refresh']=client.desktop_refresh()
    time.sleep(.5)
    report['final_screen']=capture('final-menu')
    report['final_status']=client.status()
    for key in ('usb_resets','bad_frames'):
        assert report['final_status'][key]==report['initial']['status'][key],key
    assert report['final_status']['usb_fault']==0 and report['final_status']['last_error']==0
    report.update(completed=True,test_files_removed=True,usb_reset_count_unchanged=True)
    save();return report

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--port');p.add_argument('--timeout',type=float,default=8)
    a=p.parse_args();a.directory.mkdir(parents=True,exist_ok=True)
    with hw.connect(a.port,a.timeout,a.directory/'wire.jsonl') as client:
        result=acceptance(client,a.directory)
    print(json.dumps({'ok':result['completed'],'report':str((a.directory/'acceptance.json').resolve()),
                      'versions':[v['version'] for v in result['versions']],
                      'test_files_removed':result['test_files_removed']},indent=2))

if __name__=='__main__':main()
