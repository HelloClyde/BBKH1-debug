"""Exercise the released virtual-pen API in an isolated full H1 firmware.

Run after test_h1_debug_native.py, with its stopped resident and utility menu.
GDB calls rd_touch_set on the resident's own task at a fw_delay boundary;
the exact release code posts to the real firmware queue. This is not USB or
physical-hardware verification, and the tested pen events are not frontend ADC.
The frontend is used only to initially enable the service and exit Time.
"""
from pathlib import Path
import argparse,hashlib,json,re,struct,sys,time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'vendor/h1-sdk/scripts'))
import navigate_emulator_game_slot as nav
from h1_debug_gdb import Remote


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--server',default='http://127.0.0.1:8847')
    p.add_argument('--gdb-port',type=int,default=8848)
    p.add_argument('--work-dir',type=Path,default=ROOT/'build/h1-debug-native-touch-r1')
    a=p.parse_args();work=a.work_dir.resolve();u=a.server
    api=lambda endpoint,data=None:nav.api(u,endpoint,data)
    runtime=api('/api/status')
    assert Path(runtime['nand']).resolve()==work/'h1-system.raw'
    assert Path(runtime['kernel']).resolve()==work/'project.bin'
    build=json.loads((ROOT/'dist/H1Debug.build.json').read_text())
    image=json.loads((work/'image-build.json').read_text())
    digest=hashlib.sha256((ROOT/'dist/H1Debug.bda').read_bytes()).hexdigest()
    assert image['bda_sha256']==build['sha256']==digest
    syms=build['symbols'];journal=[]
    def words(address,count=1):
        content=api('/api/debug/memory?address=0x%x&count=%d'%(address&0x1fffffff,count))['memory']
        result=[int(x,16) for x in re.findall(r'0x([0-9a-fA-F]{8})',content)]
        assert len(result)==count
        return result
    tcb=words(0x80a6d708)[0];assert tcb
    base=words(tcb+4)[0]
    def local(name):return base+syms[name]-0x81000000
    def state():return words(base+24)[0]
    def status():
        names=('polls','touch_pressed','touch_outstanding','touch_view_valid',
               'touch_view_event','touch_view_read','touch_view_owner','touch_pending_up','touch_installed')
        return {'state':state(),**{name:words(local(name))[0] for name in names}}
    def wait(predicate,timeout=12):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            result=predicate()
            if result:return result
            time.sleep(.1)
        raise TimeoutError('Virtual pen native condition did not occur: '+str(status()))
    def capture(name):nav.capture(u,work/(name+'.png'))
    def save():
        (work/'virtual-touch-trace.json').write_text(json.dumps(journal,indent=2)+'\n')
    def call_touch(x,y,pressed):
        # No guest code is patched. Calls only start from our known task's
        # normal delay entry, with its own stack and all original registers
        # restored before the normal delay resumes.
        remote=Remote(a.gdb_port);restored=False;original=None
        try:
            remote.command('?')
            boundary=local('fw_delay')
            remote.run_to(boundary)
            current=struct.unpack('<I',remote.memory(0x8057adfc,4))[0]
            assert current==tcb,('Not resident task',hex(current),hex(tcb))
            original=remote.command('g');regs=list(struct.unpack('<'+'I'*(len(original)//8),bytes.fromhex(original)))
            assert regs[37]==boundary and regs[32]&1 and not regs[32]&2
            stack=regs[29]-256
            header=struct.unpack('<20I',remote.memory(base,80))
            assert stack-4096>header[13] and regs[29]<=header[12]
            tick=struct.unpack('<I',remote.memory(0x8057b064,4))[0]
            assert remote.command(f'M{stack+16:x},4:'+struct.pack('<I',tick).hex())=='OK'
            edited=regs[:];edited[4:8]=[x,y,pressed,400];edited[29]=stack
            edited[31]=boundary;edited[37]=local('rd_touch_set')
            assert remote.command('G'+struct.pack('<'+'I'*len(edited),*edited).hex())=='OK'
            remote.breakpoint(boundary,True)
            remote.command('c')
            completed=bytes.fromhex(remote.command('g'))
            result=struct.unpack_from('<I',completed,2*4)[0]
            assert struct.unpack_from('<I',completed,37*4)[0]==boundary
            assert struct.unpack('<I',remote.memory(0x8057adfc,4))[0]==tcb
            assert remote.command('G'+original)=='OK';restored=True
            remote.breakpoint(boundary,False)
            journal.append({'stage':'virtual_pen_api','x':x,'y':y,'pressed':pressed,
                            'result':result,'raw_tick':tick,'task_control_block':hex(tcb),
                            'execution':'release rd_touch_set on resident task; GDB register call only'})
            assert result==0,result
        finally:
            if original and not restored:
                # A failed test is private-emulator-only. Restore the stopped
                # task context when possible, and preserve failure evidence.
                try:remote.command('G'+original)
                except Exception:pass
            remote.close();save()
        time.sleep(.25)
        journal[-1]['after']=status();save()
    assert state()==3,'Run the standard lifecycle test first'
    nav.tap(u,42,135);wait(lambda:state()==1);nav.press_key(u,25);time.sleep(.6)
    capture('virtual-touch-before');before=words(0x83c00020,64);initial=status()
    call_touch(105,52,1);call_touch(108,54,1);call_touch(0,0,0)
    wait(lambda:words(0x83c00020,64)!=before)
    time.sleep(.5);capture('virtual-touch-time')
    other=status();polls=other['polls'];wait(lambda:status()['polls']>polls+32)
    assert state()==1 and words(tcb+4)[0]==base
    # Exit Time with the previously verified native keyboard input, then
    # specifically use virtual DOWN/MOVE/UP to launch H1Debug and stop it.
    nav.press_key(u,24);time.sleep(.8);capture('virtual-touch-returned-menu')
    call_touch(42,135,1);call_touch(44,134,1);call_touch(0,0,0)
    wait(lambda:state()==3);capture('virtual-touch-self-stop')
    final=status();assert final['touch_outstanding']==final['touch_installed']==0
    firmware=(work/'project.bin').read_bytes()
    hooks=(0x80008234,0x80040ed4,0x8000c8b0,0x8000cc5c,0x8000d208)
    for address in hooks:
        expected=struct.unpack_from('<2I',firmware,address-0x80004000)
        assert tuple(words(address,2))==expected,hex(address)
    assert words(0x8057b6d4,2)==[0x80165988,0]
    nav.press_key(u,25);time.sleep(.4);capture('virtual-touch-final-menu')
    report={'ok':True,'bda_sha256':digest,'firmware_sha256':image['kernel_sha256'],
            'method':'GDB invokes released rd_touch_set on real resident task, native OSQPost/dequeue/GUI dispatch',
            'usb_transport_tested':False,'physical_hardware_tested':False,
            'frontend_adc_used_for_tested_pen_events':False,
            'virtual_down_move_up_open_time_bda':True,
            'virtual_down_move_up_launch_debug_and_stop':True,
            'all_five_io_hooks_and_irq22_restored':True,
            'resident_base':hex(base),'task_control_block':hex(tcb),
            'initial':initial,'other_bda':other,'final':final,'calls':journal}
    (work/'virtual-touch-verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
