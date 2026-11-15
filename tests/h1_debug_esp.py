"""Round-trip the ESP text tunnel through the actual relocated MIPS debugger."""
import base64
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from h1_debug import Client
from h1_debug_esp import ESPTransport
from h1_debug_device import ResidentUSB


class UART:
    def __init__(self):
        self.usb = ResidentUSB()
        self.lines = []
        self.opened = False
        self.closed = False
        self.pending = bytearray()

    def reset_input_buffer(self):
        self.lines.clear()
        self.pending.clear()

    @property
    def in_waiting(self):
        if not self.pending:
            self.pending.extend(self.readline())
        return len(self.pending)

    def read(self, count):
        result=bytes(self.pending[:count]);del self.pending[:count];return result

    def write(self, data):
        if data == b'H1CFG/1 DBG ON\n':
            self.opened = True
            self.lines.extend([b'I (7) h1_usb: H1 CDC connected\n', b'H1DBG/1 MODE 1\n'])
        elif data == b'H1CFG/1 DBG OFF\n':
            self.opened = False
        else:
            self.assert_command(data)
            _, sequence, encoded = data.strip().rsplit(b' ', 2)
            packet = base64.b64decode(encoded, validate=True)
            self.usb.write(packet)
            self.lines.append(b'H1DBG/1 ACK ' + sequence + b'\n')
        return len(data)

    def assert_command(self, data):
        assert self.opened and data.startswith(b'H1CFG/1 DBG DATA ')

    def readline(self, limit=2048):
        if self.lines:
            return self.lines.pop(0)
        reply = self.usb.read(180)
        if reply:
            return b'H1DBG/1 DATA ' + base64.b64encode(reply) + b'\n'
        return b''

    def flush(self):
        pass

    def close(self):
        self.closed = True


class BridgeTests(unittest.TestCase):
    def test_existing_client_identity_memory_and_stop_over_uart(self):
        uart = UART()
        tunnel = ESPTransport('COM6', serial_port=uart)
        with Client(port='esp://COM6', transport=tunnel) as client:
            self.assertEqual(client.identity, 'H1DEBUG/1 V1.41 RESIDENT')
            self.assertEqual(client.capabilities()['features'], 0x3fff)
            data = bytes(range(256)) * 4
            client.write_memory(0x83d80000, data)
            self.assertEqual(client.read_memory(0x83d80000, len(data)), data)
            self.assertTrue(client.stop()['stop_acknowledged'])
        self.assertTrue(uart.closed)
        self.assertFalse(uart.opened)

    def test_disconnected_usb_is_reported_without_replaying_a_write(self):
        uart = UART()
        tunnel = ESPTransport('COM6', serial_port=uart)
        uart.lines.append(b'H1DBG/1 GONE\n')
        with self.assertRaises(OSError):
            tunnel.read(12)
        tunnel.close()

    def test_uart_timeout_in_middle_of_a_line_preserves_data(self):
        uart = UART()
        tunnel = ESPTransport('COM6', serial_port=uart)
        uart.lines.extend([b'H1DBG/1 DATA AQ', b'IDBA==\n'])
        result = bytearray()
        while len(result)<4:
            result.extend(tunnel.read(4-len(result)))
        self.assertEqual(result, bytes((1,2,3,4)))
        tunnel.close()


if __name__ == '__main__':
    unittest.main()
