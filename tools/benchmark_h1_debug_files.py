"""Measure verified file transfer on a confirmed idle H1; clean up only this run's file."""
from pathlib import Path
import argparse,hashlib,json,secrets,struct,time
from h1_debug import Client

class MeasuredClient(Client):
    def __init__(self,*args,**kw):self.calls=[];super().__init__(*args,**kw)
    def rpc(self,op,body=b''):
        start=time.monotonic()
        try:return super().rpc(op,body)
        finally:self.calls.append(dict(op=op,seconds=time.monotonic()-start,bytes=len(body)))

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port',required=True);ap.add_argument('--output',required=True,type=Path)
    ap.add_argument('--size',type=int,default=32768);ap.add_argument('--repeat',type=int,default=2)
    a=ap.parse_args()
    if not 1024<=a.size<=2*1024*1024 or not 1<=a.repeat<=3:ap.error('size 1KiB..2MiB, repeat 1..3')
    a.output.mkdir(parents=True,exist_ok=True)
    report=dict(physical_h1=True,passed=False,port=a.port,size=a.size,rounds=[],cleanup=False)
    target='A:\\H1DS'+secrets.token_hex(4).upper()+'.BIN';report['target']=target
    source=a.output/'payload.bin';raw=bytes(range(256))*(a.size//256)+bytes(range(a.size%256));source.write_bytes(raw)
    expected=hashlib.sha256(raw).hexdigest()
    def save():(a.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    save()
    try:
        with MeasuredClient(a.port,timeout=6) as c:
            report['identity']=c.identity;report['caps']=c.capabilities()
            before=c.status();report['status_before']=before
            report['image_id']='%08x'%struct.unpack_from('<I',c.read_memory(before['resident_base'],80),76)[0]
            serial=c.serial
            if hasattr(serial,'serial'):
                report['uart_baud']=serial.serial.baudrate
                report['esp_firmware']=getattr(serial,'info',{}).get('firmware')
            desktop=c.desktop_status();report['desktop']=desktop
            if not desktop['menu_idle']:raise RuntimeError('Exit foreground application to idle desktop first')
            if c.file_stat(target)['exists']:raise RuntimeError('Benchmark path already exists; refusing overwrite')
            save()
            for i in range(a.repeat):
                start=len(c.calls);put=c.file_put(source,target,receipt_path=a.output/f'put-{i}.json')
                calls=c.calls[start:];data_time=sum(x['seconds'] for x in calls if x['op']==42)
                r=dict(kind='new' if not i else 'replace',put_seconds=put['elapsed_seconds'],put_Bps=a.size/put['elapsed_seconds'],
                       write_rpc_seconds=data_time,write_rpc_Bps=a.size/data_time,write_rpc_count=sum(x['op']==42 for x in calls))
                report['rounds'].append(r);save();print('Verified upload',i,json.dumps(r),flush=True)
                got=c.file_get(target,a.output/f'readback-{i}.bin')
                if got['sha256']!=expected:raise RuntimeError('Readback SHA256 mismatch; retained benchmark file for diagnosis')
                r.update(get_seconds=got['elapsed_seconds'],get_Bps=a.size/got['elapsed_seconds'],sha256_verified=True);save()
                print('Readback SHA256 matched',i,round(r['get_Bps'],1),'B/s',flush=True)
            c.file_delete(target)
            if c.file_stat(target)['exists']:raise RuntimeError('Benchmark cleanup not confirmed')
            report['cleanup']=True;report['status_after']=c.status();report['rpc']=c.calls
            report['device_tick_delta']=(report['status_after']['tick']-before['tick'])&0xffffffff
            report['device_tick_hz']=report['caps'].get('tick_hz') or None
            if report['status_after']['bad_frames']!=before['bad_frames'] or report['status_after']['usb_fault']:
                raise RuntimeError('Device reported framing or USB fault')
            report['passed']=True;print('PASS verified transfers and owned-file cleanup; H1Debug remains running',flush=True)
    except BaseException as e:
        report['error']=str(e);save();raise
    finally:save()
if __name__=='__main__':main()
