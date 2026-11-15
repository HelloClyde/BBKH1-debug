"""Serial-compatible H1Debug transport through the ESP USB2 configuration UART."""
import base64
import binascii
import time


class ESPTransport:
    def __init__(self, port, timeout=4.0, serial_port=None):
        self.timeout = min(.1, timeout)
        self.command_timeout = timeout
        self.buffer = bytearray()
        self.lines = bytearray()
        self.sequence = 0
        self.ack = 0
        self.mode = False
        self.closed = False
        if serial_port is None:
            from h1_esp_serial import open_console
            serial_port, self.info = open_console(port, timeout=self.timeout)
        self.serial = serial_port
        try:
            self.serial.reset_input_buffer()
            self.serial.write(b'H1CFG/1 DBG ON\n')
            deadline = time.monotonic() + timeout
            while not self.mode and time.monotonic() < deadline:
                self._receive()
            if not self.mode:
                raise TimeoutError('ESP debug bridge did not open; start H1Debug on H1 first')
        except BaseException:
            self.close()
            raise

    def _receive(self):
        if b'\n' not in self.lines:
            if hasattr(self.serial, 'in_waiting'):
                from h1_esp_serial import read_chunk
                self.lines.extend(read_chunk(self.serial))
            else:  # Existing injected/model transports.
                self.lines.extend(self.serial.readline(2048))
        if len(self.lines)>8192:
            raise OSError('ESP bridge line exceeds limit')
        if b'\n' not in self.lines:
            return
        line, _, remainder = self.lines.partition(b'\n')
        self.lines = bytearray(remainder)
        line = line.strip()
        if line.startswith(b'H1DBG/1 DATA '):
            try:
                self.buffer.extend(base64.b64decode(line[13:], validate=True))
            except binascii.Error as error:
                raise OSError('Invalid ESP bridge data') from error
        elif line.startswith(b'H1DBG/1 ACK '):
            self.ack = int(line[12:])
        elif line == b'H1DBG/1 MODE 1':
            self.mode = True
        elif line == b'H1DBG/1 MODE 0':
            self.mode = False
        elif line == b'H1DBG/1 GONE' or line.startswith(b'H1DBG/1 ERROR '):
            raise OSError(line.decode('ascii', 'replace'))

    def reset_input_buffer(self):
        self.buffer.clear()

    def write(self, data):
        if self.closed or not self.mode:
            raise OSError('ESP debug bridge is closed')
        data = bytes(data)
        if not 0 < len(data) <= 528:
            raise ValueError('ESP debug frame must be 1..528 bytes')
        self.sequence = self.sequence % 0xffffffff + 1
        line = b'H1CFG/1 DBG DATA ' + str(self.sequence).encode() + b' ' + base64.b64encode(data) + b'\n'
        if self.serial.write(line) != len(line):
            raise OSError('Short ESP bridge write; command outcome may be unknown')
        deadline = time.monotonic() + self.command_timeout
        while self.ack != self.sequence and time.monotonic() < deadline:
            self._receive()
        if self.ack != self.sequence:
            raise TimeoutError('ESP USB write acknowledgement missing; command outcome may be unknown')
        return len(data)

    def read(self, count):
        deadline = time.monotonic() + self.timeout
        while not self.buffer and time.monotonic() < deadline:
            self._receive()
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.serial.write(b'H1CFG/1 DBG OFF\n')
            self.serial.flush()
        finally:
            self.serial.close()
