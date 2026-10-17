"""Run the actual MIPS I/O code with modeled firmware queue and LCD memory.

The CPU executes the installed native-key jump and generated trampoline.
Queue posting and the physical scanner are explicit models; no hardware claim.
"""
from pathlib import Path
import hashlib
import json
import struct
import subprocess
import sys
from elftools.elf.elffile import ELFFile
from unicorn import Uc,UC_ARCH_MIPS,UC_MODE_MIPS32,UC_MODE_LITTLE_ENDIAN,UC_HOOK_CODE,UC_HOOK_MEM_READ
from unicorn.mips_const import (UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,
    UC_MIPS_REG_A3,UC_MIPS_REG_V0,UC_MIPS_REG_RA,UC_MIPS_REG_SP,UC_MIPS_REG_PC,
    UC_MIPS_REG_CP0_STATUS,UC_MIPS_REG_S0,UC_MIPS_REG_S1,UC_MIPS_REG_S2,
    UC_MIPS_REG_S3,UC_MIPS_REG_S4,UC_MIPS_REG_S5,UC_MIPS_REG_S6,UC_MIPS_REG_S7,
    UC_MIPS_REG_FP,UC_MIPS_REG_GP)

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'build/h1-debug/io-test'
sys.path.insert(0,str(ROOT/'tools'))
from verify_h1_debug_io_firmware import verify


def build():
    OUT.mkdir(parents=True,exist_ok=True)
    stub=OUT/'cache_stub.c'
    stub.write_text('void rd_cache_sync(void *a,void *b){(void)a;(void)b;}\n')
    tc=ROOT/'.tools/toolchain/bin'
    elf=OUT/'io.elf'
    args=[tc/'mipsel-none-elf-gcc.exe','-EL','-march=mips32','-mabi=32','-msoft-float',
          '-mno-abicalls','-G0','-fno-pic','-O2','-ffreestanding','-fno-builtin',
          '-fno-stack-protector','-Wall','-Wextra','-Werror','-nostdlib',
          '-Wl,--build-id=none','-Wl,-Ttext,0x82000000','-Wl,-e,rd_io_init',
          '-I',ROOT/'h1-debug','-o',elf,ROOT/'h1-debug/io.c',stub,'-lgcc']
    result=subprocess.run(list(map(str,args)),capture_output=True,text=True)
    if result.returncode:raise RuntimeError(result.stderr)
    return elf


class VM:
    def __init__(self,elf):
        self.u=Uc(UC_ARCH_MIPS,UC_MODE_MIPS32|UC_MODE_LITTLE_ENDIAN)
        self.u.mem_map(0,64*1024*1024)
        self.u.mem_map(0x13050000,4096)
        self.u.reg_write(UC_MIPS_REG_CP0_STATUS,0x10000000)
        with elf.open('rb') as handle:
            parsed=ELFFile(handle)
            for segment in parsed.iter_segments():
                if segment['p_type']=='PT_LOAD':
                    self.write(segment['p_vaddr'],segment.data())
            self.symbols={s.name:s['st_value'] for s in parsed.get_section_by_name('.symtab').iter_symbols()}
        firmware=(ROOT/'build/minigpt-native/project.bin').read_bytes()
        # Load exact binding evidence and original scanner prologue. The
        # actual scanner continuation and queue routine are modeled below.
        for a in (0x80008234,0x80008238,0x8000cddc,0x8000cde0,0x80005b48,
                  0x80005b4c,0x80005824,0x80005828,0x8000582c,
                  0x80040ebc,0x80040ec0,0x80040ec4,0x80040ec8,
                  0x80040ecc,0x80040ed0,0x80040ed4,0x80040ed8,0x8000c8b0,0x8000c8b4,
                  0x8000cc5c,0x8000cc60,0x8000d208,0x8000d20c):
            self.write(a,firmware[a-0x80004000:a-0x80004000+4])
        self.word(0x8057ae10,0x80580000)
        self.write(0x80580000,b'\x02'+bytes(7)+struct.pack('<I',0x80580100))
        self.word(0x80580114,50);self.word(0x80580118,0)
        self.word(0x80580104,0x80580200);self.word(0x80580108,0x805802c8)
        self.word(0x8058010c,0x80580200);self.word(0x80580110,0x80580200)
        self.word(0x8057adfc,0x80590000)
        self.physical_xy=(77,88)
        self.accepted=[];self.fail_after=None;self.physical=0
        self.u.hook_add(UC_HOOK_CODE,self.intercept,begin=0x8000cddc,end=0x8000cddc)
        self.u.hook_add(UC_HOOK_CODE,self.intercept,begin=0x8000823c,end=0x8000823c)
        for a in (0x80040edc,0x8000c8b8,0x8000cc64,0x8000d210):
            self.u.hook_add(UC_HOOK_CODE,self.intercept,begin=a,end=a)

    def write(self,address,data):self.u.mem_write(address&0x1fffffff,data)
    def read(self,address,size):return bytes(self.u.mem_read(address&0x1fffffff,size))
    def word(self,address,value=None):
        if value is None:return struct.unpack('<I',self.read(address,4))[0]
        self.write(address,struct.pack('<I',value))
    def intercept(self,u,address,size,user):
        if address==0x8000823c:
            result=int(self.physical==u.reg_read(UC_MIPS_REG_A0))
        elif address==0x80040edc:
            self.write(u.reg_read(UC_MIPS_REG_A0),struct.pack('<H',self.physical_xy[0]))
            self.write(u.reg_read(UC_MIPS_REG_A1),struct.pack('<H',self.physical_xy[1]))
            sp=u.reg_read(UC_MIPS_REG_SP)
            u.reg_write(UC_MIPS_REG_S6,self.word(sp+0x48));u.reg_write(UC_MIPS_REG_S7,self.word(sp+0x4c))
            u.reg_write(UC_MIPS_REG_SP,sp+0x58)
            result=0
        elif address in (0x8000c8b8,0x8000cc64):
            assert u.reg_read(UC_MIPS_REG_A0)==0x80580000
            used=self.word(0x80580118)
            if used:
                out=self.word(0x80580110);result=self.word(out)
                self.word(0x80580110,0x80580200 if out+4==0x805802c8 else out+4)
                self.word(0x80580118,used-1)
            else:result=0
            reg=UC_MIPS_REG_A1 if address==0x8000c8b8 else UC_MIPS_REG_A2
            self.write(u.reg_read(reg),bytes([0 if result else 31]))
            if address==0x8000c8b8:
                sp=u.reg_read(UC_MIPS_REG_SP)
                u.reg_write(UC_MIPS_REG_S1,self.word(sp+0x14));u.reg_write(UC_MIPS_REG_SP,sp+0x20)
        elif address==0x8000d210:
            assert u.reg_read(UC_MIPS_REG_A0)==0x80580000
            self.drain();result=0;sp=u.reg_read(UC_MIPS_REG_SP)
            u.reg_write(UC_MIPS_REG_S0,self.word(sp+0x10));u.reg_write(UC_MIPS_REG_SP,sp+0x18)
        else:
            assert u.reg_read(UC_MIPS_REG_A0)==0x80580000
            pointer=u.reg_read(UC_MIPS_REG_A1)
            used=self.word(0x80580118)
            if used==50 or self.fail_after==0:result=30
            else:
                value,code=struct.unpack('<2I',self.read(pointer,8))
                assert (1<=value<=42 and code in (9,10)) or code in (8,11,12)
                self.accepted.append((pointer,value,code))
                end=self.word(0x8058010c);self.word(end,pointer)
                self.word(0x8058010c,0x80580200 if end+4==0x805802c8 else end+4)
                self.word(0x80580118,used+1);result=0
                if self.fail_after is not None:self.fail_after-=1
        u.reg_write(UC_MIPS_REG_V0,result)
        u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
    def call(self,name,*args):
        for reg,value in zip((UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,UC_MIPS_REG_A3),args):
            self.u.reg_write(reg,value)
        self.u.reg_write(UC_MIPS_REG_SP,0x83fef000)
        if len(args)>4:self.write(0x83fef010,struct.pack('<'+'I'*(len(args)-4),*args[4:]))
        self.u.reg_write(UC_MIPS_REG_RA,0x80002000)
        address=self.symbols[name] if isinstance(name,str) else name
        self.u.emu_start(address,0x80002000,count=5_000_000)
        assert self.u.reg_read(UC_MIPS_REG_PC)==0x80002000,hex(self.u.reg_read(UC_MIPS_REG_PC))
        return self.u.reg_read(UC_MIPS_REG_V0)
    def held(self):
        self.call('rd_keys_get',0x81000000,0x81000004)
        return struct.unpack('<2I',self.read(0x81000000,8))
    def drain(self):
        self.word(0x80580118,0)
        self.word(0x8058010c,0x80580200);self.word(0x80580110,0x80580200)
    def consume(self,pend=False):
        if pend:return self.call(0x8000cc5c,0x80580000,0,0x81000010)
        return self.call(0x8000c8b0,0x80580000,0x81000010)
    def position(self):
        self.call(0x80040ebc,0x81000010,0x81000012)
        return struct.unpack('<2H',self.read(0x81000010,4))
    def touch(self,now):
        self.call('rd_touch_get',0x81000010,now)
        return struct.unpack('<4I',self.read(0x81000010,16))
    def screen(self,bits=18):
        for a,value in ((0x803f2404,480),(0x803f2408,272),(0x803f240c,bits),
                        (0xb3050030,13),(0xb3050040,0x0057bbc0)):
            self.word(a,value)
        total=480*272*(2 if bits==16 else 4)
        self.write(0x8057bbc0,struct.pack('<4I',0x0057bbc0,0x01f00000,0xbeafbeaf,0xc0000000|total//4))
        raw=(bytes(range(251))*((total+250)//251))[:total]
        self.write(0x81f00000,raw)
        self.write(0x810ffff0,b'\xa5'*16)
        self.write(0x81100000+522240,b'\x5a'*16)
        return raw


def main():
    static=verify(ROOT/'build/minigpt-native/project.bin');elf=build();cases=[]
    vm=VM(elf)
    assert vm.call('rd_io_init')==0
    original=(0x3c02802a,0x8c424aa4)
    patched=struct.unpack('<2I',vm.read(0x80008234,8))
    assert patched[0]>>26==2 and patched[1]==0
    # Independent keys D+K, release each separately, immutable queued records.
    d,k=1<<9,1<<31
    assert vm.call('rd_keys_set',d|k,0,80,100)==0
    assert vm.held()==(d|k,0)
    assert vm.call(0x80008234,32)==1 and vm.call(0x80008234,37)==1
    assert vm.call(0x80008234,16)==0
    vm.physical=16;assert vm.call(0x80008234,16)==1
    assert vm.call('rd_keys_set',k,0,80,101)==0 and vm.call(0x80008234,32)==0
    assert vm.call(0x80008234,37)==1
    assert vm.call('rd_keys_set',0,0,0,102)==0 and vm.held()==(0,0)
    assert [(v,c) for p,v,c in vm.accepted]==[(10,9),(32,9),(10,10),(32,10)]
    for pointer,value,code in vm.accepted:
        assert struct.unpack('<2I',vm.read(pointer,8))==(value,code)
    cases.append('D+K independent held/release, physical OR, immutable event pointers')
    # Confirm and Enter map to one native scancode; release of one keeps other.
    vm.drain();assert vm.call('rd_keys_set',1<<24,1<<6,8,200)==0
    assert vm.call(0x80008234,28)==1
    assert vm.call('rd_keys_set',0,1<<6,8,201)==0
    assert vm.call(0x80008234,28)==1
    vm.call('rd_keys_poll',209);assert vm.held()==(0,0)
    assert vm.call(0x80008234,28)==0
    cases.append('native scancode aliases ORed and lease expiry releases')
    # Full queue accepts no new presses, and explicit release is immediate
    # for held queries while queued-up transitions are retried after drain.
    vm.drain();assert vm.call('rd_keys_set',d|k,0,80,300)==0
    vm.word(0x80580118,50);before=len(vm.accepted)
    assert vm.call('rd_keys_set',d|k|1,0,80,301)==2
    assert vm.held()==(d|k,0) and len(vm.accepted)==before
    assert vm.call('rd_keys_set',0,0,0,302)==2 and vm.held()==(0,0)
    vm.drain();vm.call('rd_keys_poll',303)
    assert [(v,c) for p,v,c in vm.accepted[-2:]]==[(10,10),(32,10)]
    cases.append('full queue rejects new state, immediate held release, deferred event retry')
    # Unexpected mid-batch OSQPost failure records exactly the accepted key;
    # expiration crosses uint32 wrap and retries its release safely.
    vm.drain();vm.fail_after=1
    assert vm.call('rd_keys_set',d|k,0,8,0xfffffffc)==2
    assert vm.held()==(d,0)
    vm.call('rd_keys_poll',3);assert vm.held()==(d,0)
    vm.call('rd_keys_poll',4);assert vm.held()==(0,0)
    vm.fail_after=None;vm.drain();vm.call('rd_keys_poll',5)
    assert vm.accepted[-1][1:]==(10,10)
    cases.append('partial post reconciliation and wraparound lease expiration')
    for args in ((0,0x400,1,0),(1,0,0,0),(1,0,4001,0)):
        assert vm.call('rd_keys_set',*args)==1
    vm.drain();assert vm.call('rd_keys_set',1,0,4000,10)==0
    vm.word(0x80580118,50)
    assert vm.call('rd_io_shutdown')==2 and vm.held()==(0,0)
    assert struct.unpack('<2I',vm.read(0x80008234,8))==patched
    vm.drain();assert vm.call('rd_io_shutdown')==0
    assert struct.unpack('<2I',vm.read(0x80008234,8))==original
    assert vm.call('rd_io_shutdown')==0 and vm.call('rd_io_init')==0
    vm.word(0x80008234,0x08001234)
    assert vm.call('rd_io_shutdown')==3 and vm.word(0x80008234)==0x08001234
    cases.append('input bounds, pending stop, exact restore, restart, foreign hook refusal')
    vm=VM(elf);assert vm.call('rd_io_init')==0
    prefix=struct.pack('<6I',0x27bdffa8,0xafbf0050,0xafb7004c,0xafb60048,0x00a0b821,0x0080b021)
    assert vm.read(0x80040ebc,24)==prefix
    callee_saved=(UC_MIPS_REG_S0,UC_MIPS_REG_S1,UC_MIPS_REG_S2,UC_MIPS_REG_S3,
                  UC_MIPS_REG_S4,UC_MIPS_REG_S5,UC_MIPS_REG_S6,UC_MIPS_REG_S7,
                  UC_MIPS_REG_FP,UC_MIPS_REG_GP)
    saved_values={reg:0x71100000+16*i for i,reg in enumerate(callee_saved)}
    def checked_position(expected):
        for reg,value in saved_values.items():vm.u.reg_write(reg,value)
        assert vm.position()==expected
        assert vm.u.reg_read(UC_MIPS_REG_SP)==0x83fef000
        assert vm.u.reg_read(UC_MIPS_REG_RA)==0x80002000
        for reg,value in saved_values.items():assert vm.u.reg_read(reg)==value,(reg,value)
    checked_position(vm.physical_xy)
    # Queue all three transitions before the GUI runs. Each dequeue must bind
    # its own coordinates; slot reuse must never change a returned event proxy.
    assert vm.call('rd_touch_set',10,20,1,80,100)==0
    assert vm.call('rd_touch_set',100,120,1,80,101)==0
    assert vm.call('rd_touch_set',0,0,0,0,102)==0
    assert vm.touch(102)==(100,120,0,0)
    proxies=[]
    for point,event in [((10,20),11),((100,120),12),((100,120),8)]:
        pointer=vm.consume(pend=event==12);proxies.append((pointer,event))
        assert struct.unpack('<2I',vm.read(pointer,8))==(0,event)
        checked_position(point)
    # Reusing a dequeued slot, including changing its event kind, leaves all
    # previously returned proxy records immutable.
    for i in range(160):
        assert vm.call('rd_touch_set',i%480,i%272,1,80,103+i)==0
        vm.consume(pend=bool(i&1));assert vm.position()==(i%480,i%272)
        for pointer,event in proxies:assert struct.unpack('<2I',vm.read(pointer,8))==(0,event)
    assert vm.word(vm.symbols['touch_outstanding'])==0
    assert vm.call('rd_touch_set',0,0,0,0,300)==0
    vm.consume();assert vm.position()==(159,159)
    assert vm.call('rd_io_shutdown')==0
    assert vm.position()==vm.physical_xy
    cases.append('queued DOWN/MOVE/UP coordinates, 160 safe slot reuses, original getter signatures and complete o32 bridge preservation')
    vm=VM(elf);assert vm.call('rd_io_init')==0
    for args in ((480,0,1,1,0),(0,272,1,1,0),(0,0,2,1,0),(0,0,1,0,0),(0,0,1,4001,0)):
        assert vm.call('rd_touch_set',*args)==1
    assert vm.call('rd_touch_set',479,271,1,8,0xfffffffc)==0
    assert vm.touch(0)==(479,271,1,4)
    before=len(vm.accepted)
    assert vm.call('rd_touch_set',479,271,1,8,0xfffffffc)==0 and len(vm.accepted)==before
    vm.consume();vm.word(0x80580118,50)
    assert vm.call('rd_touch_set',2,3,1,20,0)==2
    assert vm.touch(0)==(479,271,1,4)
    vm.call('rd_touch_poll',3);assert vm.touch(3)[2]==1
    vm.call('rd_touch_poll',4);assert vm.touch(4)==(479,271,0,0)
    assert vm.call('rd_io_shutdown')==2
    vm.drain();vm.call('rd_touch_poll',5)
    assert vm.consume() and vm.call('rd_io_shutdown')==2
    # A different task reading the UP's coordinates cannot acknowledge the
    # foreground consumer. The original consumer can stop after reading them.
    vm.word(0x8057adfc,0x80590100);assert vm.position()==(479,271)
    assert vm.call('rd_io_shutdown')==2
    vm.word(0x8057adfc,0x80590000);assert vm.position()==(479,271)
    assert vm.call('rd_io_shutdown')==0
    cases.append('touch bounds, same-point renewal, full queue expiry retry, wraparound and consumer-specific UP acknowledgement')
    vm=VM(elf);assert vm.call('rd_io_init')==0
    assert vm.call('rd_touch_set',22,33,1,80,1)==0
    vm.consume();assert vm.position()==(22,33)
    # Physical key events preserve the active virtual pen. Physical touch
    # records keep their native pointer/value and select original coordinates.
    assert vm.call('rd_keys_set',1,0,80,2)==0
    key=vm.consume();assert struct.unpack('<2I',vm.read(key,8))==(1,9)
    assert vm.position()==(22,33)
    physical=0x80581000;vm.write(physical,struct.pack('<2I',0,11))
    assert vm.call(0x8000cddc,0x80580000,physical)==0
    assert vm.consume()==physical and vm.position()==vm.physical_xy
    assert vm.call('rd_touch_set',44,55,1,80,3)==0
    vm.consume();assert vm.position()==(44,55)
    assert vm.call('rd_touch_set',66,77,1,80,4)==0
    assert vm.call('rd_touch_set',0,0,0,0,5)==0
    assert vm.word(vm.symbols['touch_outstanding'])==2
    assert vm.call(0x8000d208,0x80580000)==0
    assert vm.word(vm.symbols['touch_outstanding'])==0 and vm.touch(5)[2]==0
    vm.call('rd_touch_poll',6)
    up=vm.consume();assert struct.unpack('<2I',vm.read(up,8))==(0,8)
    # An app that ignores coordinates still acknowledges processing its UP
    # by entering its next input read; no timestamp guesses are needed.
    assert vm.consume()==0
    assert vm.call('rd_keys_set',0,0,0,0)==0
    vm.consume();assert vm.call('rd_io_shutdown')==0
    cases.append('mixed physical/key/virtual input, native queue flush reclamation and deferred final UP')
    vm=VM(elf);assert vm.call('rd_io_init')==0
    assert vm.call('rd_touch_set',4,5,1,80,0)==0
    for i in range(49):assert vm.call('rd_touch_set',i+5,6,1,80,i+1)==0
    assert vm.call('rd_touch_set',100,100,1,80,51)==2
    assert vm.word(vm.symbols['touch_outstanding'])==50
    snapshots=[(p,vm.read(p,8)) for p,_,_ in vm.accepted]
    assert vm.call('rd_touch_set',0,0,0,0,52)==2
    for p,data in snapshots:assert vm.read(p,8)==data
    for _ in range(50):vm.consume()
    vm.call('rd_touch_poll',53);vm.consume();vm.position()
    foreign=0x8000c8b0;original=vm.word(foreign);vm.word(foreign,0x08001234)
    assert vm.call('rd_io_shutdown')==3 and vm.word(foreign)==0x08001234
    vm.word(foreign,original);assert vm.call('rd_io_shutdown')==0
    assert vm.call('rd_io_init')==0 and vm.call('rd_io_shutdown')==0
    cases.append('50-event pressure preserves queued point records, drains safely, foreign hook refusal and restart')
    vm=VM(elf)
    for bits,fmt in ((18,2),(16,1),(32,2)):
        raw=vm.screen(bits)
        assert vm.call('rd_screen_capture',0x81100000,522240,0x81000000)==0
        metadata=struct.unpack('<6I',vm.read(0x81000000,24))
        assert metadata==(480,272,480*(2 if bits==16 else 4),fmt,len(raw),0xa1f00000)
        assert vm.read(0x81100000,len(raw))==raw
        assert vm.read(0x810ffff0,16)==b'\xa5'*16
        assert vm.read(0x81100000+522240,16)==b'\x5a'*16
    cases.append('exact RGB565 and 18/32-bit LCD DMA snapshots with guards')
    invalid=[(0x803f2404,481),(0xb3050030,0),(0x803f240c,15),
             (0x8057bbc0,0x0057bbd0),(0x8057bbc4,0x03ffffff),
             (0x8057bbcc,0xd001fe00)]
    for address,value in invalid:
        vm.screen();vm.word(address,value)
        assert vm.call('rd_screen_capture',0x81100000,522240,0x81000000)==2
    vm.screen();assert vm.call('rd_screen_capture',0x81100000,522239,0x81000000)==1
    vm.word(0x80005b48,0)
    assert vm.call('rd_io_init')==1
    assert vm.call('rd_screen_capture',0x81100000,522240,0x81000000)==1
    cases.append('invalid descriptor, source, geometry, pixel format and firmware refusal')
    vm=VM(elf);vm.screen()
    def swap_frame(u,access,address,size,value,user):
        vm.word(0x8057bbc4,0x01e00000)
    hook=vm.u.hook_add(UC_HOOK_MEM_READ,swap_frame,
        begin=0x01f00000+522240-4,end=0x01f00000+522240-1)
    assert vm.call('rd_screen_capture',0x81100000,522240,0x81000000)==3
    vm.u.hook_del(hook)
    cases.append('display buffer swap during snapshot rejected')
    report=dict(ok=True,actual_compiled_MIPS=True,hardware_tested=False,
                cache_operation_hardware_tested=False,physical_scanner_modeled=True,
                firmware_queue_modeled=True,cases=cases,
                ELF_sha256=hashlib.sha256(elf.read_bytes()).hexdigest(),static=static)
    (OUT/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
