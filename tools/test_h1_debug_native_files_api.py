"""Verify native filesystem calls in an isolated, explicitly selected H1 image.

GDB invokes original firmware functions from the real debugger worker's task
context, with interrupts enabled. This is native integration evidence, not a
USB speed measurement or physical-device test. Only private NAND is modified.
"""
from pathlib import Path
import argparse, hashlib, json, struct, sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'vendor/h1-sdk/scripts'))
import navigate_emulator_game_slot as nav
from h1_debug_gdb import Remote, KERNEL_SHA


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--server',default='http://127.0.0.1:8857')
    p.add_argument('--gdb-port',type=int,default=8858)
    p.add_argument('--work-dir',type=Path,default=ROOT/'build/h1-debug-native-files-probe')
    a=p.parse_args();work=a.work_dir.resolve()
    status=nav.api(a.server,'/api/status')
    assert Path(status['nand']).resolve()==work/'h1-system.raw'
    assert Path(status['kernel']).resolve()==work/'project.bin'
    assert hashlib.sha256((work/'project.bin').read_bytes()).hexdigest()==KERNEL_SHA
    build=json.loads((work/'tested-build.json').read_text())
    r=Remote(a.gdb_port);saved=None;results=[]
    try:
        r.command('?')
        def word(addr):return struct.unpack('<I',r.memory(addr,4))[0]
        def write(addr,data):assert r.command(f'M{addr:x},{len(data):x}:'+data.hex())=='OK'
        tcb=word(0x80a6d708);assert tcb
        base=word(tcb+4)
        symbols={n:base+v-0x81000000 for n,v in build['symbols'].items()}
        boundary=symbols['transport_service'];r.run_to(boundary)
        assert word(0x8057adfc)==tcb and r.memory(tcb+0x35,1)==b'\x06'
        saved=bytes.fromhex(r.command('g'))
        regs=list(struct.unpack('<'+'I'*(len(saved)//4),saved))
        assert regs[32]&1 and not regs[32]&2
        scratch=symbols['screen']+4096
        fs=struct.unpack('<24I',r.memory(0x802aa080,96))
        def call(fn,*args):
            values=regs.copy();values[29]-=4096;values[31]=boundary;values[37]=fn
            for i,v in enumerate(args):values[4+i]=v
            assert r.command('G'+struct.pack('<'+'I'*len(values),*values).hex())=='OK'
            r.run_to(boundary)
            value=struct.unpack('<I',bytes.fromhex(r.command('g'))[8:12])[0]
            assert r.command('G'+saved.hex())=='OK'
            return value
        paths=[scratch+i*256 for i in range(3)]
        names=[r'A:\H1DNative.TMP',r'A:\H1DNative.DAT',r'A:\H1DNative.BAK']
        for address,name in zip(paths,names):write(address,name.encode('ascii')+b'\0')
        mode=scratch+768;data=scratch+1024;out=scratch+2048
        payload=b'H1 native background filesystem proof\0'+bytes(range(256))
        # Existing paths are never overwritten by this test.
        write(mode,b'rb\0')
        for path in paths:
            handle=call(fs[0],path,mode)
            if handle:
                call(fs[1],handle);raise RuntimeError('Native test path already exists')
        write(mode,b'wb\0');write(data,payload)
        handle=call(fs[0],paths[0],mode);assert handle
        assert call(fs[3],data,1,len(payload),handle)==len(payload)
        assert call(fs[1],handle)==0
        assert call(fs[10],paths[0],paths[1])==0
        write(mode,b'rb\0');handle=call(fs[0],paths[1],mode);assert handle
        assert call(fs[2],out,1,len(payload),handle)==len(payload)
        assert r.memory(out,len(payload))==payload
        assert call(fs[1],handle)==0
        # Native rename must refuse replacement; target bytes survive.
        write(mode,b'wb\0');handle=call(fs[0],paths[2],mode);assert handle
        write(data,b'old-backup');assert call(fs[3],data,1,10,handle)==10
        assert call(fs[1],handle)==0
        refused=call(fs[10],paths[2],paths[1]);assert refused==0xffffffff
        error=word(0x8057aefc)
        assert call(fs[9],paths[2])==0
        assert call(fs[10],paths[1],paths[2])==0
        write(mode,b'rb\0');handle=call(fs[0],paths[2],mode);assert handle
        assert call(fs[2],out,1,len(payload),handle)==len(payload)
        assert r.memory(out,len(payload))==payload
        assert call(fs[1],handle)==0
        assert call(fs[9],paths[2])==0
        results.append(dict(worker_priority=6,interrupts_enabled=True,bytes=len(payload),
                            create_write_close=True,rename=True,readback_exact=True,
                            overwrite_refused=True,overwrite_errno=error,remove=True))
    finally:
        if saved is not None:r.command('G'+saved.hex())
        r.close()
    report=dict(ok=True,physical_hardware_tested=False,usb_transport_tested=False,
                firmware_sha256=KERNEL_SHA,bda_sha256=build['sha256'],
                gdb_injected_firmware_calls=True,results=results)
    (work/'native-files-api-verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
