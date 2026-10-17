"""Exercise resident start, another BDA, stop/restart in an isolated H1 firmware.

Requires a prepared stopped/bootable private NAND with H1Debug in the
Black/White game slot, served on distinct ports 8847/8848; see tests/README.md.
Only this explicitly selected emulator is controlled; no physical USB access.
"""
from pathlib import Path
import argparse,hashlib,json,re,subprocess,sys,time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'vendor/h1-sdk/scripts'))
import navigate_emulator_game_slot as nav

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--server',default='http://127.0.0.1:8847')
    p.add_argument('--work-dir',type=Path,default=ROOT/'build/h1-debug-native')
    p.add_argument('--already-menu',action='store_true')
    a=p.parse_args();work=a.work_dir.resolve();u=a.server
    def api(path,data=None):return nav.api(u,path,data)
    status=api('/api/status')
    assert Path(status['nand']).resolve()==work/'h1-system.raw','Wrong emulator NAND'
    assert Path(status['kernel']).resolve()==work/'project.bin','Wrong emulator kernel'
    image=json.loads((work/'image-build.json').read_text(encoding='utf-8'))
    digest=hashlib.sha256((ROOT/'dist/H1Debug.bda').read_bytes()).hexdigest()
    assert image['bda_sha256']==digest,'Private image must contain current BDA'
    api('/api/charger',{'connected':False})
    if not a.already_menu:
        subprocess.run([sys.executable,ROOT/'vendor/h1-sdk/scripts/navigate_emulator_game_slot.py',
            '--server',u,'--page-swipes','1'],check=True)
        # The SDK gesture reaches the final Other page (Thunder Plane).
        # One right gesture returns to utilities: Time and the replaced
        # Black/White slot. Category/page state is otherwise easy to mistake.
        nav.swipe_right(u);time.sleep(1)
        nav.capture(u,work/'menu-before.png')
    def words(address,count):
        text=api('/api/debug/memory?address=0x%x&count=%d'%(address&0x1fffffff,count))['memory']
        values=[int(x,16) for x in re.findall(r'0x([0-9a-fA-F]{8})',text)]
        assert len(values)==count,(address,count,text)
        return values
    def capture(name):nav.capture(u,work/(name+'.png'))
    def wait(predicate,timeout=12):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            result=predicate()
            if result:return result
            time.sleep(.1)
        raise TimeoutError('Native lifecycle condition did not occur')
    def key(code=25):nav.press_key(u,code);time.sleep(.4)
    def launch_debug():nav.tap(u,42,135);time.sleep(.8)
    syms={p[2]:int(p[0],16) for line in subprocess.check_output([
        ROOT/'.tools/toolchain/bin/mipsel-none-elf-nm.exe',ROOT/'build/h1-debug/resident.elf'],text=True).splitlines()
        if len(p:=line.split())==3}
    assert words(0x80a6d708,1)[0]==0,'Resident must not already be installed'
    launch_debug();tcb=wait(lambda:words(0x80a6d708,1)[0]);base=words(tcb+4,1)[0]
    def header():return words(base,20)
    def local(name):return base+syms[name]-0x81000000
    def count():return words(local('polls'),1)[0]
    wait(lambda:header()[6]==1);capture('started');key()
    first=count();wait(lambda:count()>first+32)
    launcher_words=words(0x83c00020,16)
    heap_metadata=words(0x8057aff0,5)
    started_header=header();capture('background-menu')
    # Time is an original BDA in this private dump. Observe the actual loader
    # replacing 0x83c00020, and require the heap worker to keep progressing.
    nav.tap(u,105,52);time.sleep(1)
    wait(lambda:words(0x83c00020,16)!=launcher_words)
    other_bda_words=words(0x83c00020,16);before=count()
    wait(lambda:count()>before+32);capture('other-bda-running')
    other_header=header();assert other_header[:6]==started_header[:6]
    key(24);time.sleep(.5);capture('after-other-bda')
    launch_debug();wait(lambda:header()[6]==3);capture('stopped')
    stop_count=count();time.sleep(.5);assert count()==stop_count
    assert words(0x8057b6d4,2)==[0x80165988,0],'Tick callback was not restored'
    assert words(0x80a6d708,1)[0]==tcb and words(tcb+4,1)[0]==base
    key();launch_debug();wait(lambda:header()[6]==1)
    before=count();wait(lambda:count()>before+32);capture('restarted')
    assert words(0x80a6d708,1)[0]==tcb and words(tcb+4,1)[0]==base
    key();launch_debug();wait(lambda:header()[6]==3);key();capture('final-stopped-menu')
    report={'ok':True,'bda_sha256':digest,'firmware_sha256':image['kernel_sha256'],
        'test':'Complete H1 V1.41 firmware in isolated QEMU; native menu and real RTOS task switching',
        'physical_hardware_tested':False,'usb_transport_hardware_tested':False,
        'task_control_block':hex(tcb),'resident_base':hex(base),'resident_bytes':header()[4],
        'start_then_bda_return':True,'second_bda_overwrites_loading_window':True,
        'resident_runs_during_second_bda':True,'stop_restores_irq22':True,
        'restart_reuses_allocation_and_task':True,'final_state':'STOPPED retained allocation',
        'launcher_first_instructions':launcher_words,'other_bda_first_instructions':other_bda_words,
        'initial_header':started_header,'second_bda_header':other_header,'final_header':header(),
        'initial_heap_metadata':heap_metadata,'final_heap_metadata':words(0x8057aff0,5),
        'final_poll_count':count()}
    (work/'lifecycle-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
