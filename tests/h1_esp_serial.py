"""Read-only console discovery and buffered bridge reads. No physical claims."""
import base64,json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from h1_esp_serial import open_console,read_chunk
from h1_debug_esp import ESPTransport

class Console:
    actual=921600;reject_fast=False;identity=True;chunk=4096
    def __init__(self,**kw):self.baudrate=kw.get('baudrate',115200);self.pending=bytearray();self.writes=[];self.closed=False;self.reads=0
    def open(self):assert self.dtr is False and self.rts is False
    def close(self):self.closed=True
    def reset_input_buffer(self):self.pending.clear()
    @property
    def in_waiting(self):return len(self.pending)
    def write(self,raw):
        self.writes.append(raw)
        assert raw==b'\nH1CFG/1 INFO\n','Probe may only send INFO'
        if self.baudrate==self.actual:
            info=dict(protocol='H1CFG/1',ok=True,target='esp32s3' if self.identity else 'other',firmware='h1-wifi/0.4.5')
            self.pending.extend(b'boot log\nH1CFG/1 '+json.dumps(info).encode()+b'\n')
        return len(raw)
    def read(self,n):
        self.reads+=1;n=min(n,self.chunk);out=bytes(self.pending[:n]);del self.pending[:n];return out

class Tests(unittest.TestCase):
    def test_fast_and_legacy_discovery_only_reads_info(self):
        for rate in (921600,115200):
            class Port(Console):actual=rate
            p,info=open_console('test',serial_factory=Port,probe_seconds=.01)
            self.assertEqual(p.baudrate,rate);self.assertEqual(len(p.writes),1 if rate==921600 else 2);p.close()
    def test_fragmented_info_and_logs(self):
        class Port(Console):chunk=3
        p,info=open_console('test',serial_factory=Port,probe_seconds=.01);self.assertEqual(info['target'],'esp32s3');p.close()
    def test_wrong_identity_closes_without_bridge_or_config(self):
        saved=[]
        class Port(Console):
            identity=False
            def __init__(self,**kw):super().__init__(**kw);saved.append(self)
        with self.assertRaises(TimeoutError):open_console('test',serial_factory=Port,probe_seconds=.01)
        self.assertTrue(saved[0].closed)
    def test_unsupported_fast_baud_falls_back(self):
        class Port(Console):
            actual=115200
            @property
            def baudrate(self):return self.rate
            @baudrate.setter
            def baudrate(self,v):
                if v==921600:raise ValueError('unsupported')
                self.rate=v
        p,info=open_console('test',serial_factory=Port,probe_seconds=.01);self.assertEqual(p.baudrate,115200);p.close()
    def test_queued_bridge_line_uses_bulk_read_and_preserves_remainder(self):
        p=Console();t=ESPTransport.__new__(ESPTransport);t.serial=p;t.lines=bytearray();t.buffer=bytearray();t.mode=True;t.ack=0
        payload=bytes(range(256));p.pending.extend(b'H1DBG/1 DATA '+base64.b64encode(payload)+b'\nH1DBG/1 ACK 42\n')
        t._receive();self.assertEqual(t.buffer,payload);t._receive();self.assertEqual(t.ack,42);self.assertEqual(p.reads,1)
if __name__=='__main__':unittest.main()
