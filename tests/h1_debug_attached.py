"""Compiled-MIPS attached acquisition and conflict cleanup; modeled MMIO only."""
import struct,unittest
from h1_debug_device import ResidentUSB
from unicorn.mips_const import UC_MIPS_REG_CP0_STATUS

class USB(ResidentUSB):
    def __init__(self):self.waits=[];self.conflict=False;super().__init__()
    def code(self,uc,address,size,data):
        if self.stubs.get(address)=='fw_delay':
            self.waits.append((self.ui[1],uc.reg_read(UC_MIPS_REG_CP0_STATUS)))
            self.ticks+=1
            if self.conflict:self.ui[6]=1
        return super().code(uc,address,size,data)

class AttachedTests(unittest.TestCase):
    def prepare(self):
        u=USB();u.call('debug_usb_close',u.symbols['usb'])
        u.put(0xb0010300,struct.pack('<I',1<<6));u.waits.clear()
        return u
    def test_attached_disconnects_then_connects(self):
        u=self.prepare()
        before=u.ticks;status=u.u.reg_read(UC_MIPS_REG_CP0_STATUS)
        self.assertEqual(u.call('debug_usb_open_attached',u.symbols['usb']),0)
        self.assertEqual(u.ticks-before,20);self.assertTrue(u.ui[1]&0x40)
        self.assertTrue(u.waits);self.assertTrue(all(power==0 and cp0==status for power,cp0 in u.waits))
    def test_competing_native_worker_fails_and_restores_ownership(self):
        u=self.prepare();masks=(u.imr,u.pdmask);u.conflict=True
        self.assertEqual(u.call('debug_usb_open_attached',u.symbols['usb']),3)
        self.assertEqual((u.imr,u.pdmask),masks)

if __name__=='__main__':unittest.main()
