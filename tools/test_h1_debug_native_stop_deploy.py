"""Verify shell-launched BDA STOP and same-build debugger self-stop.

Requires an already RUNNING final debugger and a native utility desktop in
the selected private 8857/8858 emulator. H1UpdateTest.bda must already contain
the V2 update probe. The private Black/White filename contains H1Debug.

RPC arguments enter the real task-6 dispatcher through NativeClient/TaskCall.
After STOP, observations use read-only GDB memory reads and native frontend
input; TaskCall must never wait for the stopped worker. This is complete
firmware control-flow evidence, NOT physical hardware or USB-host evidence.
No emulator is started, reset, seeded, or terminated by this script.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import struct
import subprocess
import time
from urllib.parse import urlsplit

from elftools.elf.elffile import ELFFile
from test_h1_debug_native_deploy import NativeClient,TaskCall,ROOT,nav
from h1_debug_gdb import Remote,KERNEL_SHA

TASK_SLOT=0x80a6d708
ROOT_SLOT=0x80477658
DESKTOP_BUSY=0x80859a9c
LOADER_CALL=0x80077c64
NATIVE_WINDOW_PROC=0x80083c20
NATIVE_LOADER_CALL=0x0040f809
TARGET='A:\\应用\\程序\\H1UpdateTest.bda'
DEBUGGER='A:\\应用\\程序\\黑白子.bda'


def load_shell_symbols(elf_path,build):
    """Use ELF STT_FILE grouping: generic local 'state' names are duplicated."""
    selected={};all_symbols={};owner=None;image_id=None
    with elf_path.open('rb') as stream:
        elf=ELFFile(stream)
        for segment in elf.iter_segments():
            start=segment['p_vaddr'];data=segment.data()
            if segment['p_type']=='PT_LOAD' and start<=0x8100004c<start+len(data):
                image_id=struct.unpack_from('<I',data,0x8100004c-start)[0]
        for symbol in elf.get_section_by_name('.symtab').iter_symbols():
            if symbol['st_info']['type']=='STT_FILE':owner=symbol.name
            all_symbols[symbol.name]=symbol['st_value']
            if owner=='shell.c' and symbol['st_info']['bind']=='STB_LOCAL':
                selected[symbol.name]=symbol['st_value']
    assert image_id==int(build['image_id'],16),'Resident ELF belongs to another build'
    assert all_symbols['rd_shell_status']==build['symbols']['rd_shell_status']
    required=('job','kind','state','error','app_index','entries','return_code','callback_active','installed')
    assert all(name in selected for name in required),'ELF lacks shell-local state symbols'
    for name in required:
        assert 0x81000000<=selected[name]<0x81000000+build['resident_memory_bytes']
    return {name:selected[name] for name in required}


def verify_endpoint(server,gdb_port,work):
    parsed=urlsplit(server)
    # This helper deliberately confines execution to one private port pair.
    assert parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost')
    assert parsed.port==8857 and gdb_port==8858,'Only private ports 8857/8858 are allowed'
    assert not parsed.username and not parsed.password and parsed.path in ('','/')
    assert work.is_relative_to((ROOT/'build').resolve()),'Private image must be under this workspace/build'
    native=nav.api(server,'/api/status')
    assert native['running'],'Selected private emulator is not running'
    assert Path(native['nand']).resolve()==work/'h1-system.raw','Wrong selected NAND'
    assert Path(native['kernel']).resolve()==work/'project.bin','Wrong selected kernel'
    assert hashlib.sha256((work/'project.bin').read_bytes()).hexdigest()==KERNEL_SHA
    if os.name=='nt':
        command=(f'@(Get-NetTCPConnection -State Listen -LocalPort {gdb_port} '
                 '-ErrorAction Stop | Select-Object -ExpandProperty OwningProcess '
                 '-Unique) | ConvertTo-Json -Compress')
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                              capture_output=True,text=True,check=True)
        owners=json.loads(result.stdout);owners=owners if isinstance(owners,list) else [owners]
        assert owners==[native['pid']],('GDB listener is not the selected QEMU process',owners,native['pid'])
    return native


class ReadOnly:
    """Bounded stop/read/detach observations; never call code or write RAM."""
    def __init__(self,port,build,shell_symbols,probe):
        self.port=port;self.build=build;self.shell_symbols=shell_symbols;self.probe=probe
        self.tcb=None;self.base=None;self.allocation=None
    def snapshot(self,*,probe=False):
        remote=Remote(self.port)
        try:
            remote.command('?')
            def word(address):return struct.unpack('<I',remote.memory(address,4))[0]
            tcb=word(TASK_SLOT);assert 0x80004000<=tcb<0x83c00000
            base=word(tcb+4);assert 0x80004000<=base<0x83c00000
            header=struct.unpack('<20I',remote.memory(base,80))
            assert header[0]==0x31444248 and header[1]==1 and header[3]==base and header[5]==6
            assert header[19]==int(self.build['image_id'],16),'Running resident image_id mismatch'
            assert header[4]==self.build['resident_memory_bytes']
            if self.base is None:self.tcb,self.base,self.allocation=tcb,base,header[2]
            else:assert (tcb,base,header[2])==(self.tcb,self.base,self.allocation),'Resident was replaced instead of reused'
            local=lambda address:base+address-0x81000000
            root=word(ROOT_SLOT);assert 0x80004000<=root<0x83c00000
            gui=word(0x80a6d6f0+10*4)
            value=dict(tcb=tcb,base=base,allocation=header[2],header=list(header),
                       state=header[6],last_error=header[8],root=root,root_callback=word(root+0x88),
                       loader_call=word(LOADER_CALL),desktop_busy=word(DESKTOP_BUSY),
                       active_window=word(0x8057b020),focused_child=word(root+0xd8),
                       tick=word(0x8057b064),polls=word(local(self.build['symbols']['polls'])),
                       irq22_handler=word(0x8057b6d4),irq22_context=word(0x8057b6d8),
                       gui_switches=word(gui+0x3c),
                       shell={name:word(local(address)) for name,address in self.shell_symbols.items()})
            if probe:
                identity=struct.unpack('<4I',remote.memory(self.probe['identity_address'],16))
                assert identity[0]==self.probe['identity_magic'] and identity[1]==2,identity
                value['probe_identity']=list(identity)
            return value
        finally:
            remote.close() # D detaches and resumes; it does not terminate QEMU.


def wait_for(observe,predicate,label,timeout=12):
    deadline=time.monotonic()+timeout;last=None
    while time.monotonic()<deadline:
        last=observe()
        if predicate(last):return last
        time.sleep(.15)
    raise TimeoutError(f'{label}: last observation {last}')


def assert_restored(value):
    assert value['state']==3 and value['last_error']==0,value
    assert value['root_callback']==NATIVE_WINDOW_PROC and value['loader_call']==NATIVE_LOADER_CALL
    assert value['irq22_handler']==0x80165988 and value['irq22_context']==0
    assert value['shell']['installed']==0


def menu_ready(value):
    return not value['desktop_busy'] and value['active_window']==value['root'] and not value['focused_child']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',default='http://127.0.0.1:8857')
    parser.add_argument('--gdb-port',type=int,default=8858)
    parser.add_argument('--work-dir',type=Path,default=ROOT/'build/h1-debug-native-files-probe')
    parser.add_argument('--build',type=Path,default=ROOT/'dist/H1Debug.build.json')
    parser.add_argument('--bda',type=Path,default=ROOT/'dist/H1Debug.bda')
    parser.add_argument('--resident-elf',type=Path,default=ROOT/'build/h1-debug/resident.elf')
    parser.add_argument('--probes',type=Path,default=ROOT/'build/h1-debug-update-probes/probes.json')
    parser.add_argument('--debug-x',type=int,default=42)
    parser.add_argument('--debug-y',type=int,default=135)
    args=parser.parse_args();work=args.work_dir.resolve()
    assert 0<=args.debug_x<480 and 0<=args.debug_y<272
    build=json.loads(args.build.read_text(encoding='utf-8'))
    package=args.bda.read_bytes();digest=hashlib.sha256(package).hexdigest()
    assert build['sha256']==digest and build['size']==len(package)
    image=json.loads((work/'image-build.json').read_text(encoding='utf-8'))
    assert image['bda_sha256']==digest,'Selected private image is not the final BDA build'
    probes=json.loads(args.probes.read_text(encoding='utf-8'))
    probe=next(p for p in probes if p['version']==2)
    assert hashlib.sha256(Path(probe['path']).read_bytes()).hexdigest()==probe['sha256']
    shell_symbols=load_shell_symbols(args.resident_elf,build)
    native=verify_endpoint(args.server,args.gdb_port,work)
    output=work/('stop-deploy-'+str(time.time_ns()));output.mkdir()
    report=dict(ok=False,bda_sha256=digest,image_id=build['image_id'],firmware_sha256=KERNEL_SHA,
                selected_nand=str(work/'h1-system.raw'),qemu_pid=native['pid'],
                server=args.server,gdb_port=args.gdb_port,physical_hardware_tested=False,
                usb_transport_tested=False,emulator_started_or_stopped=False,
                evidence='GDB-injected RPCs in real resident task 6; read-only GDB after STOP; native GUI/loader and frontend input',
                timings_are_benchmarks=False,probe=probe,steps=[])
    observer=ReadOnly(args.gdb_port,build,shell_symbols,probe);client=None
    def record(stage,**values):
        report['steps'].append(dict(stage=stage,**values));save()
    def save():
        (output/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    def capture(name):
        target=output/(name+'.png');nav.capture(args.server,target);return str(target)
    def restart(label):
        before=observer.snapshot();assert before['state']==3 and menu_ready(before)
        picture=capture(label+'-menu-before-tap')
        nav.tap(args.server,args.debug_x,args.debug_y)
        started=wait_for(observer.snapshot,lambda v:v['state']==1,label+' did not restart')
        assert started['last_error']==0 and started['shell']['installed']==1
        prompt=capture(label+'-started-prompt')
        nav.press_key(args.server,25)
        ready=wait_for(observer.snapshot,lambda v:v['state']==1 and menu_ready(v),label+' did not return to menu')
        new_client=NativeClient(args.gdb_port,build)
        assert new_client.desktop_status()['menu_idle']==1
        record(label,before=before,started=started,ready=ready,menu_picture=picture,prompt_picture=prompt)
        return new_client
    try:
        initial=observer.snapshot();assert initial['state']==1 and menu_ready(initial)
        client=NativeClient(args.gdb_port,build)
        assert client.desktop_status()['menu_idle']==1
        # A same-image native RUN must not accidentally execute a different
        # BDA still installed under the private Black/White test filename.
        downloaded=client.file_get(DEBUGGER,output/'debugger-readback.bda')
        assert downloaded['sha256']==digest,'Private debugger file differs from the loaded resident'
        assert client.file_stat(TARGET)['size']==probe['bytes']
        record('initial',snapshot=initial,debugger_readback=downloaded,picture=capture('initial-menu'))
        launched=client.desktop_run(TARGET)
        first=wait_for(lambda:observer.snapshot(probe=True),lambda v:v['shell']['state']==3,'Probe did not enter RUNNING')
        time.sleep(.8) # Natural execution interval; no GDB is connected here.
        advanced=observer.snapshot(probe=True)
        assert advanced['probe_identity'][2]!=first['probe_identity'][2],'Probe animation did not advance'
        record('probe-running',receipt=launched,first=first,advanced=advanced,picture=capture('probe-running'))
        assert client.rpc(10)==b'';client.close();client=None
        stopped=wait_for(lambda:observer.snapshot(probe=True),lambda v:v['state']==3,'STOP failed')
        assert_restored(stopped)
        assert stopped['shell']['state']==3 and stopped['shell']['callback_active']==1
        time.sleep(.8) # Again allow the real foreground to run without GDB.
        still_running=observer.snapshot(probe=True);assert_restored(still_running)
        assert still_running['polls']==stopped['polls'],'Stopped worker still polls'
        assert still_running['probe_identity'][2]!=stopped['probe_identity'][2],'STOP halted foreground animation'
        record('stopped-foreground-continues',stopped=stopped,continued=still_running,picture=capture('stopped-probe-still-running'))
        nav.press_key(args.server,24)
        returned=wait_for(observer.snapshot,lambda v:menu_ready(v) and v['shell']['state']==4,'Probe did not return through retained bridge')
        assert_restored(returned);assert returned['shell']['callback_active']==0
        record('probe-returned-after-stop',snapshot=returned,picture=capture('probe-returned-menu'))
        client=restart('restart-after-probe-stop')
        before=client.desktop_status();assert before['menu_idle']==1
        # Do not use desktop_run()'s subsequent TaskCall polls: this payload
        # deliberately stops that very worker, potentially before the next RPC.
        queued=client.rpc(51,DEBUGGER.encode('gbk'));assert len(queued)==4
        job_id=struct.unpack('<I',queued)[0];assert job_id
        client.close();client=None
        self_stopped=wait_for(observer.snapshot,lambda v:v['state']==3,'Debugger self-stop failed',timeout=8)
        assert_restored(self_stopped)
        assert self_stopped['shell']['job']==job_id and self_stopped['shell']['kind']==2
        assert self_stopped['shell']['state']==3 and self_stopped['shell']['callback_active']==1
        assert self_stopped['shell']['entries']!=before['entry_count']
        time.sleep(.35)
        prompt=capture('remote-debugger-stopped-success-prompt')
        record('remote-debugger-self-stop',job_id=job_id,before=before,stopped=self_stopped,
               success_prompt_picture=prompt,prompt_expected='调试已关闭，USB和输入接口已恢复。',
               prompt_pixels_require_visual_inspection=True)
        nav.press_key(args.server,25)
        closed=wait_for(observer.snapshot,lambda v:menu_ready(v) and v['shell']['state']==4,'Self-stop prompt did not return through retained callback')
        assert_restored(closed);assert closed['shell']['callback_active']==0
        assert closed['shell']['return_code']==0
        record('self-stop-returned',snapshot=closed,picture=capture('self-stop-returned-menu'))
        client=restart('final-restart')
        before_poll=observer.snapshot();time.sleep(.5);final=observer.snapshot()
        assert final['polls']>before_poll['polls'] and final['state']==1 and menu_ready(final)
        report.update(ok=True,final_state='RUNNING, same resident allocation/task, native menu',final_snapshot=final)
        record('final-ready',picture=capture('final-running-menu'))
    except BaseException as exc:
        report['error']=f'{type(exc).__name__}: {exc}'
        try:report['failure_snapshot']=observer.snapshot();report['failure_picture']=capture('failure')
        except Exception as observation_error:report['failure_observation_error']=str(observation_error)
        save();raise
    finally:
        if client is not None:client.close()
    print(json.dumps({'ok':True,'report':str(output/'verification.json'),
                      'final_state':report['final_state']},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
