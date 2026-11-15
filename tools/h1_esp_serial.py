"""Identify an ESP console with read-only INFO, without toggling reset pins."""
import json
import time

BAUDRATES = (921600, 115200)

def read_chunk(port, limit=4096):
    """Drain bytes already queued; never wait for an arbitrary large read size."""
    return port.read(max(1, min(limit, port.in_waiting)))

def open_console(name, *, timeout=.1, probe_seconds=1.0, serial_factory=None):
    if serial_factory is None:
        import serial
        serial_factory = serial.Serial
    port = serial_factory(port=None, baudrate=115200, timeout=timeout, write_timeout=2)
    port.dtr = False
    port.rts = False
    port.port = name
    try:
        port.open()
        for baud in BAUDRATES:
            try:
                port.baudrate = baud
            except (ValueError, OSError):
                continue  # Older adapters may reject the high rate.
            port.reset_input_buffer()
            command = b'\nH1CFG/1 INFO\n'  # Discard an incomplete old/wrong-rate line.
            if port.write(command) != len(command):
                raise OSError('Short ESP INFO write')
            pending = bytearray()
            deadline = time.monotonic() + probe_seconds
            while time.monotonic() < deadline:
                pending.extend(read_chunk(port))
                if len(pending) > 8192:
                    raise OSError('ESP INFO line exceeds limit')
                while b'\n' in pending:
                    line, _, pending = pending.partition(b'\n')
                    marker = line.find(b'H1CFG/1 {')
                    if marker < 0:
                        continue
                    try:
                        info = json.loads(line[marker+8:])
                    except (ValueError, UnicodeError):
                        continue
                    if (info.get('protocol') == 'H1CFG/1' and info.get('ok') is True and
                        info.get('target') == 'esp32s3' and
                        str(info.get('firmware', '')).startswith('h1-wifi/')):
                        return port, info
        raise TimeoutError('No verified ESP console at 921600 or 115200 baud')
    except BaseException:
        port.close()
        raise
