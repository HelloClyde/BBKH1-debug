"""Execute compiled MIPS shell hooks with explicitly modeled GUI/filesystem.

The firmware bytes used as binding evidence and loader call instruction are
real. Queue/timer delivery, filesystem and the native app wrapper are modeled;
this test does not claim full-firmware or physical-device execution.
"""
from pathlib import Path
import hashlib,json,struct,subprocess
from elftools.elf.elffile import ELFFile
from unicorn import Uc,UC_ARCH_MIPS,UC_MODE_MIPS32,UC_MODE_LITTLE_ENDIAN,UC_HOOK_CODE
from unicorn.mips_const import *

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'build/h1-debug/shell-test'
FIRMWARE=ROOT/'build/minigpt-native/project.bin'
FIRMWARE_SHA='d05786e442f9aad62a8d0a0cb4f6d786bdc7c2fa353a7a2b152c9ed9f01b40ef'
WINDOW=0x81080000
BUFFER=0x81000000
PATH='A:\\应用\\程序\\Test.bda'.encode('gbk')

def build():
    OUT.mkdir(parents=True,exist_ok=True)
    stub=OUT/'stub.c'
    stub.write_text('unsigned test_tick=100; unsigned fw_tick(void){return test_tick;}\n'
                    'void rd_cache_sync(void*a,void*b){(void)a;(void)b;}\n')
    elf=OUT/'shell.elf'
    args=[ROOT/'.tools/toolchain/bin/mipsel-none-elf-gcc.exe','-EL','-march=mips32',
          '-mabi=32','-msoft-float','-mno-abicalls','-G0','-fno-pic','-O2',
          '-ffreestanding','-fno-builtin','-fno-stack-protector','-fno-strict-aliasing',
          '-Wall','-Wextra','-Werror','-Wno-misleading-indentation','-nostdlib',
          '-Wl,--build-id=none','-Wl,-Ttext,0x82000000','-Wl,-e,rd_shell_init',
          '-I',ROOT/'h1-debug','-o',elf,ROOT/'h1-debug/shell.c',stub,'-lgcc']
    subprocess.run(list(map(str,args)),check=True,capture_output=True)
    return elf

class VM:
    def __init__(self,elf):
        self.u=Uc(UC_ARCH_MIPS,UC_MODE_MIPS32|UC_MODE_LITTLE_ENDIAN)
        self.u.mem_map(0,0x4000000);self.u.reg_write(UC_MIPS_REG_CP0_STATUS,0x10000001)
        firmware=FIRMWARE.read_bytes();assert hashlib.sha256(firmware).hexdigest()==FIRMWARE_SHA
        self.write(0x80004000,firmware)
        with elf.open('rb') as f:
            e=ELFFile(f)
            for segment in e.iter_segments():
                if segment['p_type']=='PT_LOAD':self.write(segment['p_vaddr'],segment.data())
            self.symbols={s.name:s['st_value'] for s in e.get_section_by_name('.symtab').iter_symbols()}
        self.word(0x80477658,WINDOW);self.word(WINDOW,0x00110001)
        self.word(WINDOW+0x88,0x80083c20);self.word(0x808599e0,3)
        self.word(0x8057b020,WINDOW);self.word(WINDOW+0xd8,0);self.word(0x80859b78,0)
        self.word(0x80859a9c,0);self.word(0x8057adfc,0x81090000)
        self.write(0x81090035,b'\x0a');self.word(0x80859a84,1)
        self.word(0x80ccd050+0x2c,0);self.write(0x80ccd080,b'Test.bda\0')
        self.write(0x83c00020,struct.pack('<2I',0x03e00008,0x24020007))
        self.make_header();self.calls=[];self.payload_states=[]
        self.open_failure=False;self.close_failure=False;self.seek_failure=False
        self.loader_failure=False;self.dispatch_busy=False
        self.pause_at=None
        self.handlers={0x80083c20,0x800781e0,0x80078968,0x80084cb4,
                       0x80085324,0x80103c4c,0x8007766c,0x80077c6c,
                       0x8004d028,0x80056c9c,0x80056cec,0x8004d760,0x80056f8c,
                       0x83c00020}
        for address in self.handlers:self.u.hook_add(UC_HOOK_CODE,self.intercept,begin=address,end=address)
    def make_header(self,version=0x01000102):
        self.file_size=0x200
        words=[0]*34
        words[:7]=[0x004b4242,0x5d245562,version,0x47,self.file_size-4,0x88,0x88]
        words[33]=sum(struct.pack('<33I',*words[:33]))^0x322d464b
        words[:11]=[v^0x44525744 for v in words[:11]]
        self.header=struct.pack('<34I',*words)
    def write(self,a,data):self.u.mem_write(a&0x1fffffff,data)
    def read(self,a,n):return bytes(self.u.mem_read(a&0x1fffffff,n))
    def word(self,a,v=None):
        if v is None:return struct.unpack('<I',self.read(a,4))[0]
        self.write(a,struct.pack('<I',v&0xffffffff))
    def variable(self,name,v=None):return self.word(self.symbols[name],v)
    def intercept(self,u,a,size,userdata):
        if a==self.pause_at:
            self.pause_at=None;u.emu_stop();return
        self.calls.append(a);value=0
        if a==0x80083c20:
            if self.dispatch_busy:self.word(0x80859a9c,1)
            value=37
        elif a==0x8004d028:value=0 if self.open_failure else 0x810a0000
        elif a==0x80056cec:
            self.write(u.reg_read(UC_MIPS_REG_A0),self.header);value=len(self.header)
        elif a==0x80056c9c:value=0xffffffff if self.close_failure else 0
        elif a==0x8004d760:value=0xffffffff if self.seek_failure else self.file_size
        elif a==0x80056f8c:value=self.file_size
        elif a==0x8007766c:
            assert u.reg_read(UC_MIPS_REG_A0)==0
            if not self.loader_failure:
                self.wrapper_return=u.reg_read(UC_MIPS_REG_RA)
                u.reg_write(UC_MIPS_REG_PC,0x80077c5c);return
        elif a==0x83c00020:
            self.payload_states.append((self.variable('state'),self.variable('entries'),self.word(0x80859a9c)))
            return # Execute the actual modeled payload's jr ra / return 7.
        elif a==0x80077c6c:
            u.reg_write(UC_MIPS_REG_PC,self.wrapper_return);u.reg_write(UC_MIPS_REG_V0,1);return
        u.reg_write(UC_MIPS_REG_V0,value);u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
    def call(self,name,*args,stack=0x83fef000,stop_at=None):
        for reg,value in zip((UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,UC_MIPS_REG_A3),args):self.u.reg_write(reg,value)
        self.u.reg_write(UC_MIPS_REG_SP,stack);self.u.reg_write(UC_MIPS_REG_RA,0x80002000)
        address=self.symbols[name] if isinstance(name,str) else name
        self.pause_at=stop_at
        self.u.emu_start(address,0x80002000,count=500000)
        if stop_at is not None:
            assert self.u.reg_read(UC_MIPS_REG_PC)==stop_at,hex(self.u.reg_read(UC_MIPS_REG_PC))
            return
        assert self.u.reg_read(UC_MIPS_REG_PC)==0x80002000,hex(self.u.reg_read(UC_MIPS_REG_PC))
        return self.u.reg_read(UC_MIPS_REG_V0)
    def request(self,kind=2,path=PATH):
        self.write(BUFFER,path)
        return self.call('rd_shell_request',kind,BUFFER,len(path),BUFFER+512)
    def status(self):
        self.call('rd_shell_status',BUFFER+512)
        return struct.unpack('<8I',self.read(BUFFER+512,32))
    def timer(self,*,stop_at=None):return self.call(self.word(WINDOW+0x88),WINDOW,0x144,2,0,stop_at=stop_at)
    def resume_foreground(self,context):
        self.write(0x81090035,b'\x0a');self.u.context_restore(context)
        self.u.emu_start(self.u.reg_read(UC_MIPS_REG_PC),0x80002000,count=500000)
        assert self.u.reg_read(UC_MIPS_REG_PC)==0x80002000
        assert self.u.reg_read(UC_MIPS_REG_V0)==37

def main():
    elf=build();cases=[]
    vm=VM(elf)
    assert vm.call('rd_shell_init')==0 and vm.call('rd_shell_init')==0
    assert vm.call('rd_shell_check')==0 and vm.status()[4]==1
    assert vm.word(0x80083c20)==0x27bdffd8 # Never rewrite window proc code.
    assert vm.word(0x80077c68)==0 # Preserve loader's delay slot.
    assert vm.call('rd_shell_shutdown')==0 and vm.call('rd_shell_shutdown')==0
    assert vm.word(WINDOW+0x88)==0x80083c20 and vm.word(0x80077c64)==0x0040f809
    cases.append('verified atomic callback and loader-call install/restore; idempotent lifecycle')
    vm=VM(elf);assert vm.call('rd_shell_init')==0
    assert vm.request(1,b'')==0 and vm.status()[2]==1
    assert vm.call('rd_shell_stop_blocked')==1
    assert vm.call('rd_shell_shutdown')==2
    assert vm.call(vm.word(WINDOW+0x88),WINDOW,0x12,28,0)==37 and vm.status()[2]==1
    assert vm.timer()==37 and vm.status()[2]==4
    assert vm.calls[-6:]==[0x80083c20,0x800781e0,0x80078968,0x80084cb4,0x80085324,0x80103c4c]
    assert vm.word(0x80859a9c)==0 and vm.word(0x80859b6c)==vm.word(0x80859b70)==1
    cases.append('GUI timer safe point refreshes scan/category/favorites/cache; no stock USB sync')
    assert vm.request()==0 and vm.timer()==37
    assert vm.status()[2:4]==(4,0) and vm.status()[6:]==(1,7)
    assert vm.payload_states==[(3,1,1)]
    assert vm.request()==0 and vm.timer()==37 and vm.status()[6]==2
    cases.append('real loader jal instruction and bridge mark RUNNING before entry, DONE after return; cumulative entries')
    vm=VM(elf);assert vm.call('rd_shell_init')==0 and vm.request()==0
    vm.timer(stop_at=0x8007766c)
    assert vm.variable('state')==2 and vm.variable('callback_active')==1
    foreground=vm.u.context_save();vm.write(0x81090035,b'\x06')
    assert vm.call('rd_shell_stop_blocked',stack=0x83fee000)==1
    assert vm.call('rd_shell_shutdown',stack=0x83fee000)==2
    assert vm.variable('installed')==1
    vm.resume_foreground(foreground)
    assert vm.status()[2:4]==(4,0) and vm.status()[6:]==(1,7)
    cases.append('LOADING cannot detach hooks while native loader has not entered payload')
    for restart_before_return in (False,True):
        vm=VM(elf);assert vm.call('rd_shell_init')==0 and vm.request()==0
        vm.timer(stop_at=0x83c00020)
        assert vm.variable('state')==3 and vm.variable('callback_active')==1
        foreground=vm.u.context_save();vm.write(0x81090035,b'\x06')
        # Separate worker stack models preemption without overwriting the
        # still-live GUI stack and resident bridge's saved return address.
        worker=lambda name,*args:vm.call(name,*args,stack=0x83fee000)
        assert worker('rd_shell_active')==1 and worker('rd_shell_stop_blocked')==0
        assert worker('rd_shell_shutdown')==0 and worker('rd_shell_shutdown')==0
        assert vm.word(WINDOW+0x88)==0x80083c20 and vm.word(0x80077c64)==0x0040f809
        assert vm.variable('state')==3 and vm.variable('callback_active')==1
        assert worker('rd_shell_active')==1
        if restart_before_return:
            assert worker('rd_shell_init')==0
            assert vm.variable('state')==3 and vm.variable('callback_active')==1
            vm.write(BUFFER,PATH)
            assert worker('rd_shell_request',2,BUFFER,len(PATH),BUFFER+512)==2
        vm.resume_foreground(foreground)
        assert vm.status()[2:4]==(4,0) and vm.status()[6:]==(1,7)
        assert vm.word(0x80859a9c)==0 and vm.word(0x80859b6c)==vm.word(0x80859b70)==1
        assert vm.call('rd_shell_active')==0 and vm.call('rd_shell_stop_blocked')==0
        assert vm.variable('installed')==int(restart_before_return)
    cases.append('RUNNING detaches future hooks but retained payload/bridge returns to DONE; restart preserves job and refuses reentry')
    for mutate,expected in [(lambda v:v.make_header(0x01008102),5),
                            (lambda v:setattr(v,'open_failure',True),4),
                            (lambda v:setattr(v,'seek_failure',True),9),
                            (lambda v:setattr(v,'close_failure',True),9),
                            (lambda v:setattr(v,'loader_failure',True),6),
                            (lambda v:v.word(0x80ccd050+0x2c,0x80001234),4)]:
        vm=VM(elf);assert vm.call('rd_shell_init')==0;mutate(vm)
        assert vm.request()==0;vm.timer();assert vm.status()[2:4]==(5,expected),vm.status()
        assert vm.status()[6]==0 and vm.word(0x80859a9c)==0
    cases.append('invalid ABI, open/seek/close, missing entry and built-in-record failures never claim success')
    vm=VM(elf);vm.call('rd_shell_init')
    for candidate in (b'',b'A:\\Test.bda',PATH+b'\0',PATH.replace(b'Test.bda',b'../Bad.bda'),
                      PATH.replace(b'Test.bda',b'Test.exe'),PATH.replace(b'Test.bda',b'Bad\x81.bda'),
                      PATH.replace(b'Test.bda',b'A'*241+b'.bda')):
        assert vm.request(2,candidate)==1,candidate
    valid=PATH.replace(b'Test.bda',b'\x81\x5c.BDA')
    assert vm.request(2,valid)==0 # Valid DBCS trail 5c is not a path separator.
    assert vm.request()==2
    vm.call('rd_shell_poll',299);assert vm.status()[2]==1
    vm.call('rd_shell_poll',300);assert vm.status()[2:4]==(5,7)
    cases.append('bounded strict GBK paths, duplicate job refusal, 200-tick queue timeout')
    for mode in ('busy','skin','context','race','modal','child','menu_transition'):
        vm=VM(elf);vm.call('rd_shell_init')
        if mode=='busy':vm.word(0x80859a9c,1);assert vm.request()==2
        elif mode=='skin':vm.word(0x808599e0,0);assert vm.request()==2
        elif mode=='modal':vm.word(0x8057b020,WINDOW+0x400);assert vm.request()==2
        elif mode=='child':vm.word(WINDOW+0xd8,WINDOW+0x400);assert vm.request()==2
        elif mode=='menu_transition':vm.word(0x80859b78,1);assert vm.request()==2
        elif mode=='context':
            assert vm.request()==0;vm.write(0x81090035,b'\x06');vm.timer();assert vm.status()[2:4]==(5,8)
        else:
            assert vm.request()==0;vm.dispatch_busy=True;vm.timer();assert vm.status()[2:4]==(5,8)
    cases.append('foreground BDA, unverified skin, wrong task and timer race reject safely')
    vm=VM(elf);vm.word(0x800781e0,0);assert vm.call('rd_shell_init')==3
    vm=VM(elf);vm.call('rd_shell_init');vm.word(WINDOW+0x88,0x80001234)
    assert vm.call('rd_shell_check')==3 and vm.call('rd_shell_shutdown')==3
    assert vm.word(WINDOW+0x88)==0x80001234
    cases.append('firmware mismatch and foreign callback ownership refusal')
    report=dict(ok=True,actual_compiled_MIPS=True,GUI_and_filesystem_modeled=True,
                full_firmware_tested=False,hardware_tested=False,cases=cases,
                firmware_sha256=FIRMWARE_SHA,ELF_sha256=hashlib.sha256(elf.read_bytes()).hexdigest())
    (OUT/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
