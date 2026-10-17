"""Verify ten serial reopens and abandoned file/list cleanup on a physical H1."""
from pathlib import Path
import argparse,json,struct,time,zlib
import h1_debug as host
import test_h1_debug_hardware as hw

def cancelled(client):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        s=client.file_status()
        if s['state']==7:
            assert s['error']==12,s;return s
        if s['state'] in (8,9):raise AssertionError(s)
        time.sleep(.05)
    raise TimeoutError('DTR did not finish file cleanup')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,required=True)
    a=p.parse_args();d=a.directory;d.mkdir(parents=True,exist_ok=True)
    target='A:\\H1DTR'+time.strftime('%H%M%S')+'.bin'
    result={'completed':False,'rounds':[],'target':target}
    token=None;paths=None
    for i in range(10):
        with hw.connect(None,8,d/f'wire-{i:02d}.jsonl') as c:
            row={'round':i+1,'before':c.status()};result['rounds'].append(row)
            assert c.capabilities()['features']&0xe00==0xe00
            if i==0:
                assert not c.file_stat(target)['exists']
                cursor=struct.unpack('<I',c.rpc(60,'A:\\应用\\程序'.encode('gbk')))[0]
                first=c.rpc(61,struct.pack('<I',cursor));assert len(first)>16
                row['abandoned_list_token']=cursor
            elif i==1:
                row['list_cancelled']=cancelled(c)
                row['new_listing_count']=c.file_list('A:\\应用\\程序')['count']
                data=bytes(range(100))
                token,_=struct.unpack('<2I',c.rpc(41,struct.pack('<2I',len(data),zlib.crc32(data))+target.encode('gbk')))
                assert struct.unpack('<I',c.rpc(42,struct.pack('<2I',token,0)+data[:50]))[0]==50
                paths=c.file_paths(token);row['abandoned_upload']={'token':token,'paths':paths}
            elif i==2:
                row['upload_cancelled']=cancelled(c)
                assert row['upload_cancelled']['token']==token
                assert not c.file_stat(target)['exists']
                assert not c.file_stat(paths['temporary'])['exists']
                row['temporary_removed']=True
            else:
                row['desktop']=c.desktop_status();assert row['desktop']['menu_idle']
                row['touch']=c.touch_status()
            row['after']=c.status()
            assert not row['after']['usb_fault'] and not row['after']['last_error']
        hw.save_json(d/'reopen-verification.json',result);time.sleep(.1)
    assert len({r['before']['usb_resets'] for r in result['rounds']})==1
    assert len({r['after']['bad_frames'] for r in result['rounds']})==1
    result.update(completed=True,usb_reset_count_unchanged=True)
    hw.save_json(d/'reopen-verification.json',result)
    print(json.dumps({'ok':True,'rounds':len(result['rounds']),'temporary_removed':True}))

if __name__=='__main__':main()
