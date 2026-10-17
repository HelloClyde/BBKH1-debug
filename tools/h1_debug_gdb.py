"""Minimal acknowledged GDB remote client for isolated H1 firmware tests.

Extracted from the workspace research transport; no MiniGPT imports, private
files, or emulator connection are accessed at import time. Callers must verify
that the selected emulator/kernel/NAND belong to their explicit test instance.
"""
import socket
import struct

KERNEL_SHA = "d05786e442f9aad62a8d0a0cb4f6d786bdc7c2fa353a7a2b152c9ed9f01b40ef"

class Remote:
    def __init__(self,port=8798):
        self.sock=socket.create_connection(('127.0.0.1',port),timeout=15)
        self.breakpoints=set()
    def send(self,payload):
        data=payload.encode('ascii')
        self.sock.sendall(b'$'+data+b'#'+f'{sum(data)&255:02x}'.encode())
    def receive(self):
        # QEMU uses acknowledged packets; skip ACKs and validate the checksum.
        while True:
            c=self.sock.recv(1)
            if not c:raise ConnectionError('GDB socket closed')
            if c==b'$':break
        data=bytearray()
        while (c:=self.sock.recv(1))!=b'#':
            if not c:raise ConnectionError('GDB socket closed')
            data+=c
        check=b''
        while len(check)<2:
            c=self.sock.recv(2-len(check))
            if not c:raise ConnectionError('GDB socket closed')
            check+=c
        if sum(data)&255!=int(check,16):raise ValueError('GDB packet checksum')
        self.sock.sendall(b'+')
        result=data.decode('ascii')
        if result.startswith('E'):raise RuntimeError(result)
        return result
    def command(self,payload):
        self.send(payload);return self.receive()
    def breakpoint(self,address,enable):
        result=self.command(f'{"Z" if enable else "z"}0,{address:x},4')
        assert result=='OK',result
        if enable:self.breakpoints.add(address)
        else:self.breakpoints.discard(address)
    def registers(self):
        raw=bytes.fromhex(self.command('g'))
        regs=struct.unpack('<'+'I'*(len(raw)//4),raw)
        return dict(pc=f'0x{regs[37]:08x}',status=f'0x{regs[32]:08x}',
                    interrupts_enabled=bool(regs[32]&1),exception_level=bool(regs[32]&2),
                    ra=f'0x{regs[31]:08x}',sp=f'0x{regs[29]:08x}',
                    args=[f'0x{x:08x}' for x in regs[4:8]])
    def memory(self,address,size):
        return bytes.fromhex(self.command(f'm{address:x},{size:x}'))
    def run_to(self,address):
        self.breakpoint(address,True)
        try:
            self.command('c');result=self.registers()
            assert int(result['pc'],16)==address,result
            return result
        finally:self.breakpoint(address,False)
    def close(self):
        # Detach resumes QEMU. Breakpoints are emulator state only.
        try:
            for address in list(self.breakpoints):self.breakpoint(address,False)
            self.command('D')
        finally:self.sock.close()
