"""Test ordinary-BDA deployment against a private complete H1 firmware.

RPC payloads reach the actual relocated dispatcher through GDB in task 6.
The filesystem, GUI safe point, loader and application all run natively.
There is no emulated USB host here: this is not USB or hardware evidence.
"""
from pathlib import Path
import argparse,hashlib,json,struct,sys,time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'vendor/h1-sdk/scripts'))
import navigate_emulator_game_slot as nav
import h1_debug as host
from h1_debug_gdb import Remote,KERNEL_SHA


class TaskCall:
    def __init__(self,port,build):
        self.r=Remote(port);self.saved=None
        try:
            self.r.command('?')
            self.tcb=self.word(0x80a6d708);assert self.tcb
            self.base=self.word(self.tcb+4);assert self.word(self.base+24)==1,'Debugger must be RUNNING'
            assert self.word(self.base+76)==int(build['image_id'],16),'Resident image mismatch'
            self.symbols={n:self.base+v-0x81000000 for n,v in build['symbols'].items()}
            self.boundary=self.symbols['transport_service'];self.r.run_to(self.boundary)
            assert self.word(0x8057adfc)==self.tcb and self.r.memory(self.tcb+0x35,1)==b'\x06'
            self.saved=bytes.fromhex(self.r.command('g'))
            self.regs=list(struct.unpack('<'+'I'*(len(self.saved)//4),self.saved))
            assert self.regs[32]&1 and not self.regs[32]&2
            self.buffer=self.symbols['screen']+4096
        except BaseException:
            self.close();raise
    def word(self,address):return struct.unpack('<I',self.r.memory(address,4))[0]
    def write(self,address,data):
        for offset in range(0,len(data),1024):
            block=data[offset:offset+1024]
            assert self.r.command(f'M{address+offset:x},{len(block):x}:'+block.hex())=='OK'
    def call(self,name,*args):
        values=self.regs.copy();values[29]-=4096;values[31]=self.boundary
        values[37]=self.symbols[name] if isinstance(name,str) else name
        for i,v in enumerate(args[:4]):values[4+i]=v
        if len(args)>4:self.write(values[29]+16,struct.pack('<'+'I'*(len(args)-4),*args[4:]))
        assert self.r.command('G'+struct.pack('<'+'I'*len(values),*values).hex())=='OK'
        self.r.run_to(self.boundary)
        result=struct.unpack('<I',bytes.fromhex(self.r.command('g'))[8:12])[0]
        assert self.r.command('G'+self.saved.hex())=='OK'
        return result
    def close(self):
        if self.saved is not None:self.r.command('G'+self.saved.hex())
        self.r.close()
    def __enter__(self):return self
    def __exit__(self,*_):self.close()


class NativeClient(host.Client):
    def __init__(self,port,build):
        self.native_port=port;self.build=build;self._caps=None;self._broken=False
        assert self.rpc(1)==host.IDENTITY
        self.identity=host.IDENTITY.decode('ascii');self.port=f'private-gdb-{port}'
    def close(self):self._broken=True
    def rpc(self,op,body=b''):
        with TaskCall(self.native_port,self.build) as task:
            source=task.buffer;output=source+1024;status=source+2048
            task.write(source,bytes(body));task.write(status,bytes(4))
            count=task.call('dispatch',op,source,len(body),output,status)
            error=task.word(status);assert count<=512
            data=task.r.memory(output,count) if count else b''
            if error:raise host.DeviceError(op,error,data)
            return data


def seed_debugger(port,old_build,source):
    """Install only into the explicitly verified private emulator's test slot."""
    package=source.read_bytes()
    with TaskCall(port,old_build) as task:
        fs=struct.unpack('<24I',task.r.memory(0x802aa080,96));scratch=task.buffer
        path=scratch;mode=scratch+256;data=scratch+512
        task.write(path,'A:\\应用\\程序\\黑白子.bda'.encode('gbk')+b'\0')
        task.write(mode,b'rb\0');handle=task.call(fs[0],path,mode);assert handle
        try:
            old_size=task.call(fs[4],handle,0,2);assert old_size==old_build['size']
            assert task.call(fs[4],handle,0,0)==0
            digest=hashlib.sha256()
            for offset in range(0,old_size,4096):
                count=min(4096,old_size-offset);assert task.call(fs[2],data,1,count,handle)==count
                digest.update(task.r.memory(data,count))
            assert digest.hexdigest()==old_build['sha256']
        finally:assert task.call(fs[1],handle)==0
        task.write(mode,b'wb\0');handle=task.call(fs[0],path,mode);assert handle
        try:
            for offset in range(0,len(package),4096):
                block=package[offset:offset+4096];task.write(data,block)
                assert task.call(fs[3],data,1,len(block),handle)==len(block)
        finally:assert task.call(fs[1],handle)==0
        task.write(mode,b'rb\0');handle=task.call(fs[0],path,mode);assert handle
        try:
            actual=bytearray()
            for offset in range(0,len(package),4096):
                count=min(4096,len(package)-offset)
                assert task.call(fs[2],data,1,count,handle)==count
                actual.extend(task.r.memory(data,count))
            assert actual==package
        finally:assert task.call(fs[1],handle)==0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--server',default='http://127.0.0.1:8857')
    p.add_argument('--gdb-port',type=int,default=8858)
    p.add_argument('--work-dir',type=Path,default=ROOT/'build/h1-debug-native-files-probe')
    p.add_argument('--seed',action='store_true')
    a=p.parse_args();work=a.work_dir.resolve()
    native=nav.api(a.server,'/api/status')
    assert Path(native['nand']).resolve()==work/'h1-system.raw'
    assert Path(native['kernel']).resolve()==work/'project.bin'
    assert hashlib.sha256((work/'project.bin').read_bytes()).hexdigest()==KERNEL_SHA
    build=json.loads((ROOT/'dist/H1Debug.build.json').read_text())
    if a.seed:
        old_build=json.loads((work/'tested-build.json').read_text())
        seed_debugger(a.gdb_port,old_build,ROOT/'dist/H1Debug.bda')
        (work/'tested-build.json').write_text(json.dumps(build,indent=2)+'\n',encoding='utf-8')
        image=json.loads((work/'image-build.json').read_text())
        image['bda_sha256']=build['sha256'];image['updated_via']='original native FS calls in private worker'
        (work/'image-build.json').write_text(json.dumps(image,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print('PASS private debugger package copied and read back; reboot private emulator next');return
    probes=json.loads((ROOT/'build/h1-debug-update-probes/probes.json').read_text())
    client=NativeClient(a.gdb_port,build)
    target='A:\\应用\\程序\\H1UpdateTest.bda'
    assert not client.file_stat(target)['exists'],'Use a fresh private test image'
    report=dict(bda_sha256=build['sha256'],firmware_sha256=KERNEL_SHA,
                physical_hardware_tested=False,usb_transport_tested=False,
                evidence='Actual resident dispatcher in worker task 6; native FS, GUI and loader; GDB-injected RPC arguments',versions=[])
    for probe in probes:
        version=probe['version'];print(f'deploy v{version}',flush=True)
        receipt=client.deploy(probe['path'],target,receipt_path=work/f'deploy-v{version}.json')
        time.sleep(.4)
        with TaskCall(a.gdb_port,build) as task:
            identity=struct.unpack('<4I',task.r.memory(probe['identity_address'],16))
            assert identity[0]==probe['identity_magic'] and identity[1]==version,identity
        time.sleep(.4)
        with TaskCall(a.gdb_port,build) as task:
            advanced=struct.unpack('<4I',task.r.memory(probe['identity_address'],16))
            assert advanced[2]!=identity[2],'Foreground animation stalled'
        nav.capture(a.server,work/f'running-v{version}.png')
        # File reading remains usable while the ordinary BDA is running.
        downloaded=work/f'download-v{version}.bda'
        get=client.file_get(target,downloaded)
        assert get['sha256']==probe['sha256']
        try:client.desktop_refresh(wait_seconds=1)
        except RuntimeError:pass
        else:raise AssertionError('Refresh must reject an active foreground app')
        client.press('ESCAPE',ticks=8)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            status=client.desktop_status()
            if status['menu_idle']:break
            time.sleep(.05)
        else:raise TimeoutError('Probe failed to return to desktop')
        nav.capture(a.server,work/f'menu-v{version}.png')
        report['versions'].append(dict(version=version,receipt=receipt,identity=identity,
                                       advanced=advanced,download_sha256=get['sha256'],exit_status=status))
    report['ok']=True
    (work/'deploy-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
