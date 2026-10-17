"""Execute the actual compiled MIPS resident wake code with RTOS/IRQ shims.

The original IRQ, raw clock, OSTimeDly and OSTimeDlyResume are explicit models.
This checks scheduling decisions and IRQ publication, not physical timing.
"""
from pathlib import Path
import hashlib
import json
import struct
import subprocess
from elftools.elf.elffile import ELFFile
from unicorn import Uc,UC_ARCH_MIPS,UC_MODE_MIPS32,UC_MODE_LITTLE_ENDIAN,UC_HOOK_CODE
from unicorn.mips_const import (UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,
    UC_MIPS_REG_A3,UC_MIPS_REG_V0,UC_MIPS_REG_RA,UC_MIPS_REG_SP,UC_MIPS_REG_PC,
    UC_MIPS_REG_CP0_STATUS)

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'build/h1-debug/wake-test'
ORIGINAL_IRQ=0x80165988
RAW_TICK=0x80165b04
OS_DELAY=0x8000e8fc
OS_RESUME=0x8000e810
IRQ_SLOT=0x8057b6d4
TASK_SLOT=0x80a6d6f0+6*4
CURRENT_TASK=0x8057adfc
WORKER_TCB=0x8058e000
GUI_TCB=0x8058e100
CONTROL=0x81900000


def build():
    OUT.mkdir(parents=True,exist_ok=True)
    tc=ROOT/'.tools/toolchain/bin';elf=OUT/'wake.elf'
    args=[tc/'mipsel-none-elf-gcc.exe','-EL','-march=mips32','-mabi=32','-msoft-float',
          '-mno-abicalls','-G0','-fno-pic','-O2','-ffreestanding','-fno-builtin',
          '-fno-stack-protector','-Wall','-Wextra','-Werror','-nostdlib',
          '-Wl,--build-id=none','-Wl,-Ttext,0x82000000','-Wl,-e,fw_wake_install',
          '-I',ROOT/'h1-debug','-o',elf,ROOT/'h1-debug/firmware.c','-lgcc']
    result=subprocess.run(list(map(str,args)),capture_output=True,text=True)
    if result.returncode:raise RuntimeError(result.stderr)
    return elf


class VM:
    def __init__(self,elf):
        self.u=Uc(UC_ARCH_MIPS,UC_MODE_MIPS32|UC_MODE_LITTLE_ENDIAN)
        self.u.mem_map(0,64*1024*1024)
        self.u.reg_write(UC_MIPS_REG_CP0_STATUS,0x10000001)
        firmware=(ROOT/'build/minigpt-native/project.bin').read_bytes()
        self.write(0x80004000,firmware)
        with elf.open('rb') as handle:
            parsed=ELFFile(handle)
            for segment in parsed.iter_segments():
                if segment['p_type']=='PT_LOAD':self.write(segment['p_vaddr'],segment.data())
            self.symbols={s.name:s['st_value'] for s in parsed.get_section_by_name('.symtab').iter_symbols()}
        self.word(IRQ_SLOT,ORIGINAL_IRQ);self.word(IRQ_SLOT+4,0)
        self.word(TASK_SLOT,WORKER_TCB);self.word(CURRENT_TASK,WORKER_TCB)
        self.word(WORKER_TCB+4,CONTROL);self.word(WORKER_TCB+0x30,0)
        self.write(WORKER_TCB+0x35,b'\x06');self.write(GUI_TCB+0x35,b'\x07')
        self.tick=100;self.tick_step=0;self.tick_reads=0
        self.irq_calls=[];self.resume_calls=[];self.delay_calls=[]
        self.resume_result=0
        for address in (ORIGINAL_IRQ,RAW_TICK,OS_DELAY,OS_RESUME):
            self.u.hook_add(UC_HOOK_CODE,self.intercept,begin=address,end=address)

    def write(self,address,data):self.u.mem_write(address&0x1fffffff,data)
    def read(self,address,size):return bytes(self.u.mem_read(address&0x1fffffff,size))
    def word(self,address,value=None):
        if value is None:return struct.unpack('<I',self.read(address,4))[0]
        self.write(address,struct.pack('<I',value))
    def state(self,name,value=None):return self.word(self.symbols[name],value)
    def intercept(self,u,address,size,user):
        argument=u.reg_read(UC_MIPS_REG_A0)
        if address==RAW_TICK:
            self.tick_reads+=1;result=self.tick
            self.tick=(self.tick+self.tick_step)&0xffffffff
        elif address==ORIGINAL_IRQ:
            self.irq_calls.append(argument)
            self.tick=(self.tick+1)&0xffffffff
            result=0
        elif address==OS_DELAY:
            assert argument==1,('resident must use native one-tick sleep',argument)
            assert self.state('wake_pending')==1
            assert u.reg_read(UC_MIPS_REG_CP0_STATUS)&1==0, 'wake publication and sleep must be atomic'
            self.delay_calls.append(dict(deadline=self.state('wake_at'),tick=self.tick,
                                         pending=self.state('wake_pending')))
            self.word(WORKER_TCB+0x30,1);result=0
        else:
            assert argument==6,'must only wake the resident task'
            self.resume_calls.append(argument);result=self.resume_result
            if not result:self.word(WORKER_TCB+0x30,0)
        u.reg_write(UC_MIPS_REG_V0,result)
        u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
    def call(self,name,*args):
        for reg,value in zip((UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,UC_MIPS_REG_A3),args):
            self.u.reg_write(reg,value)
        self.u.reg_write(UC_MIPS_REG_SP,0x83fef000);self.u.reg_write(UC_MIPS_REG_RA,0x80002000)
        address=self.symbols[name] if isinstance(name,str) else name
        self.u.emu_start(address,0x80002000,count=10_000_000)
        assert self.u.reg_read(UC_MIPS_REG_PC)==0x80002000,hex(self.u.reg_read(UC_MIPS_REG_PC))
        return self.u.reg_read(UC_MIPS_REG_V0)
    def interrupt(self,argument=0):
        before=len(self.irq_calls)
        saved=self.u.reg_read(UC_MIPS_REG_CP0_STATUS)
        self.u.reg_write(UC_MIPS_REG_CP0_STATUS,saved&~1)
        self.call(self.word(IRQ_SLOT),argument)
        self.u.reg_write(UC_MIPS_REG_CP0_STATUS,saved)
        assert self.irq_calls[before:]==[argument],'original handler must run exactly once, with original argument'


def main():
    elf=build();vm=VM(elf);cases=[]
    assert vm.call('fw_matches')==1
    assert vm.call('fw_wake_install',CONTROL)==0
    installed=vm.word(IRQ_SLOT)
    assert installed==vm.symbols['tick_irq'] and vm.word(IRQ_SLOT+4)==0
    assert vm.call('fw_wake_install',CONTROL)==1
    before=vm.u.reg_read(UC_MIPS_REG_CP0_STATUS)
    vm.call('fw_delay',5)
    assert vm.u.reg_read(UC_MIPS_REG_CP0_STATUS)==before
    assert vm.state('wake_at')==105 and vm.state('wake_pending')==1
    for i in range(4):vm.interrupt(0xabc00000+i)
    assert vm.tick==104 and not vm.resume_calls and vm.state('wake_pending')==1
    # Model an IRQ in the publication/removal gap as an additional defense:
    # an overdue deadline must not be consumed while the task is still ready.
    vm.word(WORKER_TCB+0x30,0);vm.interrupt(0x12345678)
    assert vm.tick==105 and not vm.resume_calls and vm.state('wake_pending')==1
    vm.word(WORKER_TCB+0x30,1);vm.interrupt()
    assert vm.resume_calls==[6] and vm.state('wake_pending')==0
    vm.interrupt();assert vm.resume_calls==[6]
    cases.append('atomic arm/block, original handler forwarding, no early wake, pending retained until delayed, one resume')
    # Failed resume or a foreign/missing TCB must not consume the wake request.
    vm.call('fw_delay',1);vm.resume_result=61;vm.interrupt()
    assert len(vm.resume_calls)==2 and vm.state('wake_pending')==1
    vm.word(WORKER_TCB+4,CONTROL+32);vm.interrupt()
    assert len(vm.resume_calls)==2 and vm.state('wake_pending')==1
    for invalid in (0,0x80000000,0x83bfff90,WORKER_TCB+1):
        vm.word(TASK_SLOT,invalid);vm.interrupt()
        assert len(vm.resume_calls)==2 and vm.state('wake_pending')==1
    vm.word(TASK_SLOT,WORKER_TCB);vm.word(WORKER_TCB+4,CONTROL)
    vm.resume_result=0;vm.interrupt()
    assert len(vm.resume_calls)==3 and vm.state('wake_pending')==0
    cases.append('resume error, foreign owner and invalid TCB preserve pending wake for retry')
    # Deadline comparisons remain correct over uint32 raw tick wrap.
    vm.tick=0xfffffffe;vm.call('fw_delay',3)
    assert vm.state('wake_at')==1
    before=len(vm.resume_calls);vm.interrupt();vm.interrupt()
    assert vm.tick==0 and len(vm.resume_calls)==before
    vm.interrupt();assert vm.tick==1 and len(vm.resume_calls)==before+1
    cases.append('deadline comparison across raw tick wrap')
    # Stop restores native callback. Its dormant task then publishes a new
    # wait; reinstallation must keep that stale pending request to wake it.
    assert vm.call('fw_wake_remove')==0 and vm.word(IRQ_SLOT)==ORIGINAL_IRQ
    assert vm.call('fw_wake_remove')==0
    vm.call('fw_delay',20);deadline=vm.state('wake_at')
    before=len(vm.resume_calls)
    for _ in range(25):vm.interrupt()
    assert len(vm.resume_calls)==before and vm.state('wake_pending')==1
    assert vm.call('fw_wake_install',CONTROL)==0
    assert vm.state('wake_at')==deadline
    vm.interrupt();assert len(vm.resume_calls)==before+1 and not vm.state('wake_pending')
    cases.append('stop restores original callback; restart preserves overdue wait and wakes dormant worker')
    # Never overwrite a callback or argument changed by somebody else.
    vm.word(IRQ_SLOT,0x8000bad0)
    assert vm.call('fw_wake_remove')==1 and vm.word(IRQ_SLOT)==0x8000bad0
    assert vm.state('wake_installed')==1
    vm.word(IRQ_SLOT,installed);vm.word(IRQ_SLOT+4,1)
    assert vm.call('fw_wake_remove')==1 and vm.word(IRQ_SLOT+4)==1
    vm.word(IRQ_SLOT+4,0)
    assert vm.call('fw_wake_remove')==0 and vm.word(IRQ_SLOT)==ORIGINAL_IRQ
    vm.word(IRQ_SLOT+4,1)
    assert vm.call('fw_wake_install',CONTROL)==1 and vm.word(IRQ_SLOT)==ORIGINAL_IRQ
    vm.word(IRQ_SLOT+4,0);vm.word(IRQ_SLOT,0x8000bad0)
    assert vm.call('fw_wake_install',CONTROL)==1 and vm.word(IRQ_SLOT)==0x8000bad0
    cases.append('foreign IRQ callback/argument installation and restoration refused without overwrite')
    # GUI/main callers use the live raw counter and leave RTOS wake state
    # untouched; zero delay must be a no-op for either caller.
    vm.word(CURRENT_TASK,GUI_TCB);vm.tick=500;vm.tick_step=1
    before=(len(vm.delay_calls),vm.state('wake_at'),vm.state('wake_pending'))
    vm.call('fw_delay',4)
    assert vm.tick>=505 and (len(vm.delay_calls),vm.state('wake_at'),vm.state('wake_pending'))==before
    vm.word(CURRENT_TASK,0);vm.call('fw_delay',2)
    assert len(vm.delay_calls)==before[0]
    reads=vm.tick_reads;vm.call('fw_delay',0);assert vm.tick_reads==reads
    vm.word(CURRENT_TASK,WORKER_TCB);vm.call('fw_delay',0)
    assert vm.tick_reads==reads and len(vm.delay_calls)==before[0]
    cases.append('GUI/unknown caller raw-clock wait avoids inert OSTimeDly; zero delay does nothing')
    report=dict(ok=True,actual_compiled_MIPS=True,physical_hardware_tested=False,
                RTOS_context_switch_modeled=True,original_IRQ_modeled=True,
                cases=cases,ELF_sha256=hashlib.sha256(elf.read_bytes()).hexdigest(),
                source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                               for name in ('h1-debug/firmware.c','h1-debug/firmware.h','h1-debug/debug.h')})
    (OUT/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
