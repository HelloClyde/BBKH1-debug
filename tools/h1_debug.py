"""PC client for the standalone H1 resident debugger (H1DEBUG/1).

All commands print JSON. Only matching USB VID/PID ports are opened, and the
resident identity is checked before any mutation. Serial and Pillow imports
are lazy, so protocol tests and offline module compilation need no USB setup.
Native modules are trusted code and must return or poll_cancel cooperatively.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
import struct
import subprocess
import sys
import threading
import tempfile
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
MAGIC = b"H1DB"
IDENTITY = b"H1DEBUG/1 V1.41 RESIDENT"
VID, PID = 0x07C4, 0xA4A6
MAX_PAYLOAD = 512
HEADER = struct.Struct("<4sBBHHH")
STATUS_FIELDS = ("version", "state", "usb_configured", "usb_fault", "usb_resets",
                 "commands", "bad_frames", "tick", "poll_count", "last_poll_tick",
                 "resident_base", "resident_size", "keys_low", "keys_high",
                 "exec_state", "last_error")
CAP_FIELDS = ("version", "features", "max_payload", "ram_start", "ram_size",
              "module_abi", "forced_preemption", "tick_hz")
TOUCH_FIELDS = ("x", "y", "pressed", "lease_remaining")
USB_SNAPSHOT_FIELDS = ("tick", "fault_reason", "events", "cpccr", "cppcr", "clkgr", "opcr",
                       "cp0_status", "intc_imr", "pd_mask", "pd_pin", "pd_flag", "power",
                       "intr_txe", "intr_rxe", "intr_usbe", "frame", "address", "index",
                       "configured", "resets", "session_epoch", "rx_packets", "tx_packets")
USB_FAULT_REASONS = {1: "endpoint_tx_irq_enabled", 2: "endpoint_rx_irq_enabled",
                     4: "usb_bus_irq_enabled", 8: "native_usb_irq_unmasked",
                     16: "native_cable_irq_unmasked", 32: "high_speed_enabled",
                     64: "bus_reset_timeout", 128: "clock_phy_or_ownership_guard"}
SCREEN_FIELDS = ("token", "width", "height", "stride", "format", "total", "source", "tick")
FILE_STATUS_FIELDS = ("version", "state", "token", "size", "received", "expected_crc",
                      "actual_crc", "verify_offset", "error", "fs_errno", "flags",
                      "old_size", "old_crc", "last_activity_tick", "step_count", "reserved")
FILE_STATES = ("idle", "uploading", "verifying_temp", "committing", "verifying_target",
               "done", "aborting", "aborted", "failed", "recovery", "reading", "listing")
FILE_ERRORS = ("ok", "invalid", "busy", "binding", "io", "crc", "offset", "token",
               "open_file", "needs_recovery", "not_found", "cancelled", "transport", "expired",
               "exists", "not_directory", "is_directory", "not_empty", "cross_volume")
DESKTOP_FIELDS = ("job_id", "kind", "state", "error", "menu_idle", "app_index", "entry_count", "return_code")
DESKTOP_STATES = ("idle", "queued", "loading", "running", "done", "failed")
INFO_FIELDS = ("abi", "base", "image_capacity", "scratch_capacity", "stack_capacity",
               "output_capacity", "state", "image_size", "memory_size", "entry_offset",
               "upload_crc", "written", "runs", "flags", "forced_preemption", "api_size")
RESULT_FIELDS = ("state", "image_size", "memory_size", "entry_offset", "upload_crc",
                 "written", "runs", "flags", "start_tick", "end_tick", "budget_ticks",
                 "opcode", "arg_size", "output_size", "return_value")
EXEC_FLAGS = {1: "user_cancel", 2: "cooperative_timeout", 4: "USB_fault",
              8: "guard_damage", 16: "oversized_output", 32: "restore_failed"}
# Physical matrix indices from vendor/h1-sdk/sdk/include/h1_input.h.
KEY_NAMES = {name: index for index, name in enumerate((
    "Q", "W", "E", "R", "T", "Y", "U", "A", "S", "D", "F", "G", "H", "J",
    "PAGE_UP", "Z", "X", "C", "V", "B", "N", "PAGE_DOWN", "SPACE", "ESCAPE",
    "ENTER", "LEFT_ALT", "DOWN", "RIGHT_ALT", "I", "O", "P", "K", "L", "SYMBOL",
    "RIGHT", "M", "UP", "DELETE", "CONFIRM", "LEFT", "BACK", "SHIFT"), 1)}
KEY_NAMES.update(ESC=24, PGUP=15, PGDN=22)


class ProtocolError(RuntimeError):
    """The session cannot safely continue after an invalid/incomplete frame."""


class DeviceError(RuntimeError):
    def __init__(self, op, status, payload):
        self.op, self.status, self.payload = op, status, payload
        super().__init__(f"H1 command {op} failed with status {status}")


def integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError("Expected an integer or a 0x-prefixed string")


def bounded(value, low, high, name):
    value = integer(value)
    if not low <= value <= high:
        raise ValueError(f"{name} must be in {low}..{high}")
    return value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def device_path(value, *, max_bytes=240, for_put=False, desktop=False, directory=False, leaf=False):
    """Validate Unicode components before GBK encoding (DBCS can contain 0x5c)."""
    if not isinstance(value, str):
        raise ValueError("Device path must be a string")
    value = value.replace("/", "\\")
    if directory and len(value) > 3 and value.endswith("\\"):
        value = value[:-1]
    if len(value) < 3 or value[0].upper() not in "AB" or value[1:3] != ":\\":
        raise ValueError("Device path must be absolute A:\\ or B:\\; PC drive letters are not device paths")
    value = value[0].upper() + value[1:]
    components = value[3:].split("\\") if value[3:] else []
    if leaf and not components:
        raise ValueError("File management mutations require a non-root path")
    for component in components:
        if (not component or component in (".", "..") or component[-1] in " ." or
                any(ord(c) < 32 or ord(c) == 127 or c in '<>:"|?*' for c in component)):
            raise ValueError("Device path contains an invalid component")
    if desktop:
        if (value[0] != "A" or len(components) != 3 or components[:2] != ["应用", "程序"] or
                not components[2].lower().endswith(".bda") or len(components[2]) == 4):
            raise ValueError("Desktop run requires a direct A:\\应用\\程序\\NAME.bda file")
    try:
        encoded = value.encode("gbk", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("Device path is not representable in GBK") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"Device path exceeds {max_bytes} GBK bytes")
    if for_put:
        if not components:
            raise ValueError("File upload requires a filename")
        parent = value.rsplit("\\", 1)[0] + "\\"
        if len(parent.encode("gbk")) + 15 > 240:
            raise ValueError("Device directory leaves no room for the transaction's temporary filename")
    return value, encoded


def rename_paths(source, target):
    source, encoded_source = device_path(source, leaf=True)
    target, encoded_target = device_path(target, leaf=True)
    if source[0] != target[0]:
        raise ValueError("Rename requires source and target on the same device volume")
    # Compare ASCII case only; GBK characters are retained exactly.
    fold = lambda s: "".join(c.lower() if "A" <= c <= "Z" else c for c in s)
    if fold(source) == fold(target):
        raise ValueError("Rename source and target must differ")
    if fold(target).startswith(fold(source) + "\\"):
        raise ValueError("Rename target cannot be inside the source directory")
    return source, target, struct.pack("<I", len(encoded_source)) + encoded_source + encoded_target


def stream_digest(stream):
    digest, crc, size = hashlib.sha256(), 0, 0
    for block in iter(lambda: stream.read(65536), b""):
        size += len(block)
        if size > 0x7FFFFFFF:
            raise ValueError("Device files cannot exceed 0x7fffffff bytes")
        digest.update(block)
        crc = zlib.crc32(block, crc)
    return {"size": size, "crc32": crc, "sha256": digest.hexdigest()}


def save_receipt(path, value):
    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_failure_receipt(path, value):
    # Preserve the original failure even when the local evidence disk is full.
    try:
        save_receipt(path, value)
    except OSError as exc:
        value["receipt_save_error"] = str(exc)


def validate_bda_file(path):
    sdk = ROOT / "vendor/h1-sdk"
    if str(sdk) not in sys.path:
        sys.path.insert(0, str(sdk))
    from h1_bda.validate import validate_bda
    path = Path(path)
    if path.suffix.lower() != ".bda":
        raise ValueError("Deploy requires a complete .bda file, not an EXEC module")
    report = validate_bda(path)
    if not report["ok"]:
        raise ValueError("Invalid H1 BDA: " + "; ".join(report["errors"]))
    return report


def serial_module():
    bundled = ROOT / ".tools/usb-deps"
    if bundled.is_dir() and str(bundled) not in sys.path:
        sys.path.insert(0, str(bundled))
    try:
        import serial
        from serial.tools import list_ports
    except ImportError as exc:
        raise RuntimeError("USB commands require pyserial: python -m pip install pyserial") from exc
    return serial, list_ports


def ports():
    _, list_ports = serial_module()
    return [p for p in list_ports.comports() if (p.vid, p.pid) == (VID, PID)]


def frame(op, seq, body=b"", status=0):
    op = bounded(op, 0, 255, "opcode")
    seq = bounded(seq, 0, 65535, "sequence")
    status = bounded(status, 0, 65535, "status")
    body = bytes(body)
    if len(body) > MAX_PAYLOAD:
        raise ValueError("Frame payload exceeds 512 bytes")
    data = HEADER.pack(MAGIC, 1, op, seq, len(body), status) + body
    return data + struct.pack("<I", zlib.crc32(data))


def words(data, fields, name):
    if len(data) != 4 * len(fields):
        raise ProtocolError(f"{name} length mismatch: {len(data)}")
    return dict(zip(fields, struct.unpack("<" + "I" * len(fields), data)))


def key_number(key):
    if isinstance(key, str):
        name = key.upper().replace("-", "_")
        if name.startswith("H1_KEY_"):
            name = name[7:]
        if name in KEY_NAMES:
            return KEY_NAMES[name]
    return bounded(key, 1, 42, "physical key")


def key_masks(keys):
    if isinstance(keys, (str, int)):
        keys = [keys]
    mask = 0
    for key in keys:
        mask |= 1 << (key_number(key) - 1)
    return mask & 0xFFFFFFFF, mask >> 32


def validate_screen(meta):
    width, height, stride = meta["width"], meta["height"], meta["stride"]
    pixel_bytes = {1: 2, 2: 4}.get(meta["format"])
    if (not meta["token"] or not pixel_bytes or not 1 <= width <= 4096 or
            not 1 <= height <= 4096 or stride < width * pixel_bytes or
            meta["total"] != stride * height or meta["total"] > 16 * 1024 * 1024):
        raise ProtocolError("Unsupported screenshot metadata or dimensions")
    return pixel_bytes


def decode_screen(meta, data):
    """Decode an immutable RGB565 LE or BGRX8888 snapshot to packed RGB."""
    pixel_bytes = validate_screen(meta)
    if len(data) != meta["total"]:
        raise ProtocolError("Screenshot data length mismatch")
    rgb = bytearray(meta["width"] * meta["height"] * 3)
    out = 0
    for y in range(meta["height"]):
        row = y * meta["stride"]
        for x in range(meta["width"]):
            i = row + x * pixel_bytes
            if pixel_bytes == 2:
                value = data[i] | data[i + 1] << 8
                red, green, blue = (value >> 11) & 31, (value >> 5) & 63, value & 31
                rgb[out:out + 3] = bytes(((red << 3) | (red >> 2),
                                          (green << 2) | (green >> 4),
                                          (blue << 3) | (blue >> 2)))
            else:
                rgb[out:out + 3] = bytes((data[i + 2], data[i + 1], data[i]))
            out += 3
    return bytes(rgb)


def decode_screen_rle(encoded, pixel_bytes, decoded_count):
    """Decode one bounded op13 payload, preserving every pixel byte, including X."""
    if (pixel_bytes not in (2, 4) or not 0 < decoded_count <= 16384 or
            decoded_count % pixel_bytes or not 0 < len(encoded) <= 496):
        raise ProtocolError("Screenshot RLE bounds mismatch")
    data, pos = bytearray(), 0
    while pos < len(encoded):
        tag = encoded[pos]
        pos += 1
        repeat = tag >= 128
        pixels = (tag & 127) + 2 if repeat else tag + 1
        size = pixels * pixel_bytes
        stored = pixel_bytes if repeat else size
        if pos + stored > len(encoded) or len(data) + size > decoded_count:
            raise ProtocolError("Screenshot RLE run exceeds encoded/decoded bounds")
        part = encoded[pos:pos + stored]
        data.extend(part * pixels if repeat else part)
        pos += stored
    if len(data) != decoded_count:
        raise ProtocolError("Screenshot RLE decoded length mismatch")
    return bytes(data)


class Client:
    def __init__(self, port=None, *, timeout=4.0, transport=None):
        if not math.isfinite(timeout) or not 0.01 <= timeout <= 60:
            raise ValueError("RPC timeout must be 0.01..60 seconds")
        self.timeout = timeout
        self.seq = 0
        self._broken = False
        self._lock = threading.Lock()
        self._caps = None
        self._touch_position = None
        if transport is None:
            candidates = ports()
            if port:
                candidates = [p for p in candidates if p.device.casefold() == port.casefold()]
            if len(candidates) != 1:
                raise RuntimeError("Expected exactly one matching H1 USB port; found " +
                                   str([p.device for p in candidates]))
            self.port = candidates[0].device
            serial, _ = serial_module()
            self.serial = serial.Serial(self.port, 115200, timeout=min(.1, timeout),
                                        write_timeout=min(2.0, timeout))
        else:
            self.serial, self.port = transport, port or "injected-transport"
        try:
            self.serial.reset_input_buffer()
            identity = self.rpc(1)
            if identity != IDENTITY:
                raise ProtocolError(f"Resident debugger identity mismatch: {identity!r}")
            self.identity = identity.decode("ascii")
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._broken = True
        self.serial.close()

    def exact(self, count, deadline):
        data = bytearray()
        while len(data) < count and time.monotonic() < deadline:
            part = self.serial.read(count - len(data))
            if part:
                data.extend(part)
        if len(data) != count:
            raise TimeoutError(f"USB reply incomplete: {len(data)}/{count}; command outcome may be unknown")
        return bytes(data)

    def rpc(self, op, body=b""):
        op = bounded(op, 1, 127, "request opcode")
        body = bytes(body)
        if len(body) > MAX_PAYLOAD:
            raise ValueError("Request exceeds 512 bytes")
        with self._lock:
            if self._broken:
                raise ProtocolError("USB session is closed or desynchronized; reconnect explicitly")
            self.seq = (self.seq + 1) & 65535
            packet = frame(op, self.seq, body)
            deadline = time.monotonic() + self.timeout
            try:
                if self.serial.write(packet) != len(packet):
                    raise ProtocolError("Incomplete USB write; command outcome may be unknown")
                head = self.exact(HEADER.size, deadline)
                magic, version, reply, sequence, length, status = HEADER.unpack(head)
                if ((magic, version, reply, sequence) != (MAGIC, 1, op | 128, self.seq) or
                        length > MAX_PAYLOAD):
                    raise ProtocolError("USB reply header/sequence/length mismatch")
                tail = self.exact(length + 4, deadline)
                if zlib.crc32(head + tail[:-4]) != struct.unpack("<I", tail[-4:])[0]:
                    raise ProtocolError("USB reply CRC32 mismatch")
            except BaseException:
                self._broken = True
                raise
            if status:
                raise DeviceError(op, status, tail[:-4])
            return tail[:-4]

    def empty(self, op, body=b""):
        if self.rpc(op, body):
            raise ProtocolError(f"Command {op} returned an unexpected payload")

    def status(self):
        result = words(self.rpc(2), STATUS_FIELDS, "STATUS")
        if result["version"] != 1:
            raise ProtocolError("Unsupported STATUS version")
        return result

    def usb_diagnostics(self):
        if not (self._caps or self.capabilities())["features"] & 0x1000:
            raise RuntimeError("This resident has no retained USB diagnostics; install the diagnostic BDA and restart H1")
        data = self.rpc(14)
        if len(data) != 256:
            raise ProtocolError("USB_DIAG length mismatch")
        v = struct.unpack("<64I", data)
        if v[0] not in (1, 2) or v[1] != 64 or v[4] > 1 or v[6] != 24 or (v[0] == 1 and v[7]):
            raise ProtocolError("USB_DIAG schema mismatch")
        def snapshot(values):
            result = dict(zip(USB_SNAPSHOT_FIELDS, values))
            result["fault_names"] = [name for bit, name in USB_FAULT_REASONS.items() if result["fault_reason"] & bit]
            result["usb_clock_gated"] = bool(result["clkgr"] & (1 << 11))
            result["phy_enabled"] = bool(result["opcr"] & 0x40)
            return result
        return {"version": v[0], "open_count": v[2], "fault_count": v[3],
                "clock_link_reconnects": v[7],
                "last_fault_valid": bool(v[4]), "last_fault_open": v[5],
                "current": snapshot(v[8:32]),
                "last_fault": snapshot(v[32:56]) if v[4] else None,
                "resident": dict(zip(("state", "stop_request", "last_error", "completed_stops",
                                      "last_stop_error", "last_stop_tick", "poll_count", "last_poll_tick"), v[56:])),
                "retention": "same resident allocation across stop/start; cleared by H1 reboot",
                "read_only": True}

    def capabilities(self):
        result = words(self.rpc(9), CAP_FIELDS, "CAPS")
        if (result["version"] != 1 or result["max_payload"] != MAX_PAYLOAD or
                result["ram_start"] != 0x80000000 or result["ram_size"] != 0x04000000 or
                result["module_abi"] != 1 or result["forced_preemption"] != 0):
            raise ProtocolError("Unsupported resident debugger capabilities")
        self._caps = result
        return result

    def memory_range(self, address, count):
        address = bounded(address, 0, 0xFFFFFFFF, "address")
        count = bounded(count, 1, 0x04000000, "byte count")
        caps = self._caps or self.capabilities()
        if not caps["ram_start"] <= address < address + count <= caps["ram_start"] + caps["ram_size"]:
            raise ValueError("Memory access must stay in cached H1 RAM 0x80000000..0x83ffffff")
        return address, count

    def read_memory(self, address, count):
        address, count = self.memory_range(address, count)
        data = bytearray()
        while len(data) < count:
            wanted = min(MAX_PAYLOAD, count - len(data))
            part = self.rpc(3, struct.pack("<II", address + len(data), wanted))
            if len(part) != wanted:
                raise ProtocolError("Memory read length mismatch")
            data.extend(part)
        return bytes(data)

    def write_memory(self, address, data):
        data = bytes(data)
        address, _ = self.memory_range(address, len(data))
        for offset in range(0, len(data), 508):
            self.empty(4, struct.pack("<I", address + offset) + data[offset:offset + 508])
        return {"address": address, "bytes_written": len(data), "sha256": sha(data)}

    def keys(self, low=0, high=0, lease_ticks=0):
        low = bounded(low, 0, 0xFFFFFFFF, "keys_low")
        high = bounded(high, 0, 0x3FF, "keys_high")
        lease_ticks = bounded(lease_ticks, 1, 4000, "key lease ticks") if low or high else 0
        self.empty(5, struct.pack("<III", low, high, lease_ticks))
        return {"keys_low": low, "keys_high": high, "lease_ticks": lease_ticks,
                "automatic_release": True, "units": "raw device ticks"}

    def press(self, keys, ticks=8):
        low, high = key_masks(keys)
        if not low and not high:
            raise ValueError("At least one physical key 1..42 is required")
        return self.keys(low, high, ticks)

    def release(self):
        return self.keys()

    def _require_touch(self):
        if not (self._caps or self.capabilities())["features"] & 0x80:
            raise RuntimeError("This resident build has no touch support; install the touch-enabled BDA and restart H1")

    def touch(self, x, y, pressed=True, lease_ticks=80):
        """Hold/move one synthetic contact in 480x272 screen coordinates."""
        x, y = bounded(x, 0, 479, "touch x"), bounded(y, 0, 271, "touch y")
        if type(pressed) not in (bool, int) or pressed not in (0, 1):
            raise ValueError("Touch pressed must be 0 or 1")
        lease_ticks = bounded(lease_ticks, 1, 4000, "touch lease ticks") if pressed else 0
        self._require_touch()
        # Preserve the attempted coordinate even if the queue accepted only
        # part of an operation and returned DeviceError: cleanup must still up.
        self._touch_position = (x, y)
        self.empty(11, struct.pack("<4I", x, y, int(pressed), lease_ticks))
        return {"x": x, "y": y, "pressed": bool(pressed), "lease_ticks": lease_ticks,
                "automatic_release": True, "units": "raw device ticks"}

    def touch_status(self):
        self._require_touch()
        result = words(self.rpc(12), TOUCH_FIELDS, "TOUCH_STATUS")
        if (result["x"] > 479 or result["y"] > 271 or result["pressed"] > 1 or
                result["lease_remaining"] > 4000 or
                (not result["pressed"] and result["lease_remaining"])):
            raise ProtocolError("TOUCH_STATUS bounds mismatch")
        self._touch_position = result["x"], result["y"]
        return result

    def release_touch(self):
        if self._touch_position is None:
            state = self.touch_status()
            self._touch_position = state["x"], state["y"]
        return self.touch(*self._touch_position, pressed=False, lease_ticks=0)

    def _gesture_release(self, failure):
        if self._broken:
            return
        try:
            self.release_touch()
        except Exception as release_error:
            if failure is None:
                raise
            failure.touch_release_error = str(release_error)

    def _touch_wait_until(self, deadline, lease_ticks):
        # Renew a stationary contact during long holds and sparse swipes.
        # This is a PC scheduling interval, not a device tick conversion.
        intervals = max(1, math.ceil(max(0, deadline - time.monotonic()) / .25))
        for _ in range(intervals):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(.25, remaining))
            if remaining <= .25 or time.monotonic() >= deadline:
                return
            self.touch(*self._touch_position, lease_ticks=lease_ticks)

    def tap(self, x, y, *, hold_seconds=.12, lease_ticks=80):
        x, y = bounded(x, 0, 479, "touch x"), bounded(y, 0, 271, "touch y")
        lease_ticks = bounded(lease_ticks, 1, 4000, "touch lease ticks")
        if not math.isfinite(hold_seconds) or not .01 <= hold_seconds <= 10:
            raise ValueError("Tap hold time must be 0.01..10 seconds")
        self._require_touch()
        start, failure = time.monotonic(), None
        try:
            self.touch(x, y, lease_ticks=lease_ticks)
            self._touch_wait_until(time.monotonic() + hold_seconds, lease_ticks)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            self._gesture_release(failure)
        return {"gesture": "tap", "x": x, "y": y, "hold_seconds": hold_seconds,
                "lease_ticks": lease_ticks, "release_acknowledged": True,
                "host_elapsed_seconds": time.monotonic() - start}

    def swipe(self, x0, y0, x1, y1, *, duration_seconds=.5, steps=12, lease_ticks=80):
        x0, x1 = bounded(x0, 0, 479, "start x"), bounded(x1, 0, 479, "end x")
        y0, y1 = bounded(y0, 0, 271, "start y"), bounded(y1, 0, 271, "end y")
        steps = bounded(steps, 2, 120, "swipe steps")
        lease_ticks = bounded(lease_ticks, 1, 4000, "touch lease ticks")
        if not math.isfinite(duration_seconds) or not .05 <= duration_seconds <= 10:
            raise ValueError("Swipe duration must be 0.05..10 seconds")
        self._require_touch()
        start, failure, points = time.monotonic(), None, []
        try:
            self.touch(x0, y0, lease_ticks=lease_ticks)
            points.append([x0, y0])
            schedule_start = time.monotonic()
            for index in range(1, steps + 1):
                now = time.monotonic()
                # Slow USB must not accumulate an arbitrarily long gesture.
                # Skip overdue intermediate points; always send the endpoint.
                if index < steps and now >= schedule_start + duration_seconds:
                    continue
                target = schedule_start + duration_seconds * index / steps
                if target > now:
                    self._touch_wait_until(target, lease_ticks)
                if index < steps and time.monotonic() >= schedule_start + duration_seconds:
                    continue
                fraction = index / steps
                x, y = round(x0 + (x1 - x0) * fraction), round(y0 + (y1 - y0) * fraction)
                self.touch(x, y, lease_ticks=lease_ticks)
                points.append([x, y])
        except BaseException as exc:
            failure = exc
            raise
        finally:
            self._gesture_release(failure)
        return {"gesture": "swipe", "points_sent": points, "requested_steps": steps,
                "duration_seconds": duration_seconds, "lease_ticks": lease_ticks,
                "release_acknowledged": True, "host_elapsed_seconds": time.monotonic() - start,
                "timing_includes_usb_latency": True}

    def _screen_codec(self, codec):
        if codec not in ("auto", "raw", "rle"):
            raise ValueError("Screenshot codec must be auto, raw or rle")
        if codec == "raw":
            return codec
        supported = bool((self._caps or self.capabilities())["features"] & 0x100)
        if codec == "rle" and not supported:
            raise RuntimeError("This resident build has no lossless screenshot RLE support")
        return "rle" if supported else "raw"

    def read_screen(self, meta, *, codec="auto", progress=None):
        """Download a retained snapshot; progress(done,total) runs between RPCs."""
        if progress is not None and not callable(progress):
            raise ValueError("Screenshot progress must be callable")
        pixel_bytes = validate_screen(meta)
        codec = self._screen_codec(codec)
        if codec == "rle" and meta["total"] % pixel_bytes:
            raise ProtocolError("Screenshot RLE total is not pixel aligned")
        token, data = meta["token"], bytearray()
        requests = encoded_bytes = sent_bytes = received_bytes = 0
        start = time.monotonic()
        while len(data) < meta["total"]:
            offset = len(data)
            wanted = min(16384 if codec == "rle" else 500, meta["total"] - offset)
            block = self.rpc(13 if codec == "rle" else 7, struct.pack("<III", token, offset, wanted))
            header = 16 if codec == "rle" else 12
            if (len(block) <= header or len(block) > MAX_PAYLOAD or
                    struct.unpack_from("<III", block) != (token, offset, meta["total"])):
                raise ProtocolError("Screenshot chunk token/offset/length mismatch")
            if codec == "rle":
                count = struct.unpack_from("<I", block, 12)[0]
                if not 0 < count <= wanted or count % pixel_bytes:
                    raise ProtocolError("Screenshot RLE chunk progress/alignment mismatch")
                part = decode_screen_rle(block[16:], pixel_bytes, count)
            else:
                part = block[12:]
                if len(part) > wanted:
                    raise ProtocolError("Screenshot chunk exceeds requested length")
            data.extend(part)
            requests += 1
            encoded_bytes += len(block) - header
            sent_bytes += 28
            received_bytes += 16 + len(block)
            if progress is not None:
                progress(len(data), meta["total"])
        elapsed = time.monotonic() - start
        stats = {"codec": codec, "decoded_bytes": len(data), "encoded_bytes": encoded_bytes,
                 "requests": requests, "protocol_bytes_sent": sent_bytes,
                 "protocol_bytes_received": received_bytes, "elapsed_seconds": elapsed,
                 "decoded_bytes_per_second": len(data) / elapsed if elapsed else None,
                 "progress_callback": progress is not None,
                 "scope": "SCREEN_READ RPCs; protocol framing included, USB packet overhead excluded"}
        return bytes(data), stats

    def capture_screen(self, *, codec="auto"):
        codec = self._screen_codec(codec)
        start = time.monotonic()
        meta = words(self.rpc(6), SCREEN_FIELDS, "SCREEN_CAPTURE")
        token = meta["token"]
        try:
            data, transfer = self.read_screen(meta, codec=codec)
        except BaseException:
            if token and not self._broken:
                try:
                    self.empty(8, struct.pack("<I", token))
                except Exception:
                    pass
            raise
        self.empty(8, struct.pack("<I", token))
        return {**meta, "codec": codec, "transfer": transfer,
                "capture_elapsed_seconds": time.monotonic() - start}, data

    def screenshot(self, output, *, codec="auto"):
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("PNG screenshots require Pillow: python -m pip install pillow") from exc
        meta, data = self.capture_screen(codec=codec)
        image = Image.frombytes("RGB", (meta["width"], meta["height"]), decode_screen(meta, data))
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG")
        return {**meta, "output": str(path.resolve()), "snapshot_sha256": sha(data),
                "png_sha256": sha(path.read_bytes())}

    def _require_feature(self, mask, label):
        if not (self._caps or self.capabilities())["features"] & mask:
            raise RuntimeError(f"This resident build has no {label} support")

    def file_status(self):
        self._require_feature(0x200, "file transfer")
        result = words(self.rpc(40), FILE_STATUS_FIELDS, "FILE_STATUS")
        if (result["version"] != 1 or result["state"] >= len(FILE_STATES) or
                result["size"] > 0x7FFFFFFF or result["received"] > result["size"]):
            raise ProtocolError("File status bounds mismatch")
        result["state_name"] = FILE_STATES[result["state"]]
        result["error_name"] = FILE_ERRORS[result["error"]] if result["error"] < len(FILE_ERRORS) else "unknown"
        return result

    def file_stat(self, path):
        path, encoded = device_path(path)
        self._require_feature(0x200, "file transfer")
        result = words(self.rpc(48, encoded), ("exists", "size", "is_directory"), "FILE_STAT")
        if (result["exists"] > 1 or result["is_directory"] > 1 or result["size"] > 0x7FFFFFFF or
                (not result["exists"] and (result["size"] or result["is_directory"]))):
            raise ProtocolError("File stat bounds mismatch")
        return {"path": path, **result}

    def file_list(self, path, *, max_entries=10000):
        path, encoded = device_path(path, directory=True)
        max_entries = bounded(max_entries, 1, 1000000, "directory entry limit")
        self._require_feature(0x800, "file management")
        token, failure, close_sent = None, None, False
        receipt = {"operation": "file_list", "path": path, "entries": [], "completed": False}
        try:
            token = words(self.rpc(60, encoded), ("token",), "FILE_LIST_BEGIN")["token"]
            if not token:
                raise ProtocolError("Directory listing token is zero")
            receipt["token"] = token
            while True:
                # NEXT advances the native cursor, so even this read operation
                # must not be retried after an unconfirmed response.
                raw = self.rpc(61, struct.pack("<I", token))
                if len(raw) < 16:
                    raise ProtocolError("Directory entry header is truncated")
                done, size, attributes, name_length = struct.unpack_from("<4I", raw)
                if done > 1 or size > 0x7FFFFFFF or len(raw) != 16 + name_length:
                    raise ProtocolError("Directory entry bounds mismatch")
                if done:
                    if size or attributes or name_length:
                        raise ProtocolError("Directory completion contains an entry")
                    break
                if not 1 <= name_length <= 496 or b"\0" in raw[16:]:
                    raise ProtocolError("Directory entry name bounds mismatch")
                try:
                    name = raw[16:].decode("gbk", errors="strict")
                except UnicodeError as exc:
                    raise ProtocolError("Directory entry is not valid GBK") from exc
                if name in (".", "..") or "\\" in name or "/" in name:
                    raise ProtocolError("Directory entry name is not a basename")
                if len(receipt["entries"]) >= max_entries:
                    raise RuntimeError("Directory entry limit exceeded; increase --max-entries to finish listing")
                receipt["entries"].append({"name": name, "size": size,
                                           "attributes": attributes, "is_directory": bool(attributes & 0x10)})
            close_sent = True
            self.empty(62, struct.pack("<I", token))
            token = None
            receipt.update(completed=True, count=len(receipt["entries"]))
            return receipt
        except BaseException as exc:
            failure = exc
            receipt.update(error=str(exc), outcome_unknown=self._broken)
            if isinstance(exc, DeviceError):
                receipt.update(error_name=FILE_ERRORS[exc.status] if exc.status < len(FILE_ERRORS) else "unknown")
                if len(exc.payload) == 4:
                    receipt["fs_errno"] = struct.unpack("<I", exc.payload)[0]
            exc.file_listing = receipt
            raise
        finally:
            if token and not close_sent and not self._broken:
                try:
                    self.empty(62, struct.pack("<I", token))
                except Exception as close_error:
                    if failure is not None:
                        receipt["close_error"] = str(close_error)
                    else:
                        raise

    def _file_mutation(self, op, body, receipt):
        self._require_feature(0x800, "file management")
        receipt.update(completed=False)
        try:
            # The device performs one native operation and then acknowledges.
            # A lost ACK cannot safely be treated as permission to repeat it.
            self.empty(op, body)
            receipt["completed"] = True
            return receipt
        except BaseException as exc:
            receipt.update(error=str(exc), outcome_unknown=(self._broken or not isinstance(exc, DeviceError) or
                                                           exc.status in (4, 9, 12)))
            if isinstance(exc, DeviceError):
                receipt.update(error_name=FILE_ERRORS[exc.status] if exc.status < len(FILE_ERRORS) else "unknown")
                if len(exc.payload) == 4:
                    receipt["fs_errno"] = struct.unpack("<I", exc.payload)[0]
            exc.file_management = receipt
            raise

    def file_delete(self, path):
        path, encoded = device_path(path, leaf=True)
        return self._file_mutation(63, encoded, {"operation": "file_delete", "path": path})

    def file_rename(self, source, target):
        source, target, body = rename_paths(source, target)
        return self._file_mutation(64, body, {"operation": "file_rename", "source": source, "target": target})

    def file_mkdir(self, path):
        path, encoded = device_path(path, leaf=True)
        return self._file_mutation(65, encoded, {"operation": "file_mkdir", "path": path})

    def file_rmdir(self, path):
        path, encoded = device_path(path, leaf=True)
        return self._file_mutation(66, encoded, {"operation": "file_rmdir", "path": path})

    def file_paths(self, token):
        token = bounded(token, 1, 0xFFFFFFFF, "file token")
        self._require_feature(0x200, "file transfer")
        paths = {}
        for kind, name in enumerate(("target", "temporary", "backup")):
            raw = self.rpc(49, struct.pack("<II", token, kind))
            if len(raw) > 240 or b"\0" in raw:
                raise ProtocolError("File recovery path bounds mismatch")
            try:
                path = raw.decode("gbk", errors="strict")
                if path:
                    device_path(path)
            except (UnicodeError, ValueError) as exc:
                raise ProtocolError("Invalid GBK recovery path") from exc
            paths[name] = path
        return paths

    def _file_wait(self, token, wait_seconds):
        if not math.isfinite(wait_seconds) or not .1 <= wait_seconds <= 3600:
            raise ValueError("File completion wait must be 0.1..3600 seconds")
        deadline = time.monotonic() + wait_seconds
        while True:
            state = self.file_status()
            if state["token"] != token:
                raise ProtocolError("File transaction token changed")
            if state["state"] in (5, 7, 8, 9):
                return state
            if time.monotonic() >= deadline:
                error = TimeoutError("File outcome is not confirmed; query file status and paths, do not repeat commit")
                error.file_status = state
                raise error
            time.sleep(.02)

    def file_abort(self, token, *, wait_seconds=30):
        token = bounded(token, 1, 0xFFFFFFFF, "file token")
        if not math.isfinite(wait_seconds) or not .1 <= wait_seconds <= 3600:
            raise ValueError("File completion wait must be 0.1..3600 seconds")
        self._require_feature(0x200, "file transfer")
        self.empty(44, struct.pack("<I", token))
        state = self._file_wait(token, wait_seconds)
        return {"status": state, "paths": self.file_paths(token)}

    def file_put(self, source, target, *, wait_seconds=120, receipt_path=None):
        target, encoded = device_path(target, for_put=True)
        if not math.isfinite(wait_seconds) or not .1 <= wait_seconds <= 3600:
            raise ValueError("File completion wait must be 0.1..3600 seconds")
        source = Path(source)
        token, commit_sent = None, False
        receipt = {"operation": "file_put", "source": str(source.resolve()), "target": target,
                   "target_gbk_hex": encoded.hex(), "phase": "prepared", "completed": False}
        start = time.monotonic()
        with source.open("rb") as stream:
            expected = stream_digest(stream)
            stream.seek(0)
            receipt.update(expected)
            save_receipt(receipt_path, receipt)
            self._require_feature(0x200, "file transfer")
            try:
                begin = words(self.rpc(41, struct.pack("<II", expected["size"], expected["crc32"]) + encoded),
                              ("token", "next_offset"), "FILE_PUT_BEGIN")
                if not begin["token"] or begin["next_offset"]:
                    raise ProtocolError("File upload begin token/offset mismatch")
                token = begin["token"]
                receipt.update(token=token, phase="uploading", received=0)
                save_receipt(receipt_path, receipt)
                offset, crc, digest = 0, 0, hashlib.sha256()
                while offset < expected["size"]:
                    block = stream.read(min(504, expected["size"] - offset))
                    if not block:
                        raise RuntimeError("Local source became shorter during upload")
                    reply = words(self.rpc(42, struct.pack("<II", token, offset) + block),
                                  ("next_offset",), "FILE_WRITE")
                    if reply["next_offset"] != offset + len(block):
                        raise ProtocolError("File upload offset acknowledgement mismatch")
                    crc = zlib.crc32(block, crc)
                    digest.update(block)
                    offset += len(block)
                    receipt["received"] = offset
                if stream.read(1) or crc != expected["crc32"] or digest.hexdigest() != expected["sha256"]:
                    raise RuntimeError("Local source changed during upload; commit was not sent")
                receipt["phase"] = "commit_requested"
                save_receipt(receipt_path, receipt)
                commit_sent = True  # A lost ACK must never lead to automatic commit retry.
                self.empty(43, struct.pack("<I", token))
                state = self._file_wait(token, wait_seconds)
                receipt.update(status=state, paths=self.file_paths(token), phase=state["state_name"])
                if (state["state"] != 5 or state["size"] != expected["size"] or
                        state["received"] != expected["size"] or state["expected_crc"] != expected["crc32"] or
                        state["actual_crc"] != expected["crc32"] or not state["flags"] & 8):
                    raise RuntimeError("File commit was not verified; inspect transaction status and recovery paths")
                receipt.update(completed=True, device_crc_verified=True,
                               elapsed_seconds=time.monotonic() - start)
                save_receipt(receipt_path, receipt)
                return receipt
            except BaseException as exc:
                # Before commit, healthy framing permits one explicit abort.
                # After commit, only read status: rename/rollback may be active.
                if not self._broken:
                    try:
                        if token and not commit_sent:
                            self.empty(44, struct.pack("<I", token))
                        state = self.file_status()
                        receipt["status"] = state
                        if state["token"]:
                            receipt["paths"] = self.file_paths(state["token"])
                    except Exception as inspection_error:
                        receipt["inspection_error"] = str(inspection_error)
                observed = receipt.get("status", {})
                terminal = observed.get("token") == token and observed.get("state") in (5, 7, 8, 9)
                receipt.update(error=str(exc), outcome_unknown=self._broken or (commit_sent and not terminal),
                               elapsed_seconds=time.monotonic() - start)
                save_failure_receipt(receipt_path, receipt)
                exc.file_transfer = receipt
                raise

    def file_get(self, source, output):
        source, encoded = device_path(source)
        self._require_feature(0x200, "file transfer")
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + ".part-" + str(time.time_ns()))
        token, failure, close_sent = None, None, False
        receipt = {"operation": "file_get", "source": source, "output": str(output.resolve()),
                   "partial": str(partial.resolve()), "completed": False}
        start = time.monotonic()
        try:
            begin = words(self.rpc(45, encoded), ("token", "size"), "FILE_GET_BEGIN")
            if not begin["token"] or begin["size"] > 0x7FFFFFFF:
                raise ProtocolError("File download begin bounds mismatch")
            token, size = begin["token"], begin["size"]
            receipt.update(token=token, size=size)
            offset, crc, digest = 0, 0, hashlib.sha256()
            with partial.open("xb") as stream:
                while offset < size:
                    wanted = min(512, size - offset)
                    block = self.rpc(46, struct.pack("<III", token, offset, wanted))
                    if len(block) != wanted:
                        raise ProtocolError("File download was truncated or changed")
                    stream.write(block)
                    digest.update(block)
                    crc = zlib.crc32(block, crc)
                    offset += len(block)
            close_sent = True
            self.empty(47, struct.pack("<I", token))
            token = None
            partial.replace(output)
            receipt.update(completed=True, crc32=crc, sha256=digest.hexdigest(),
                           device_crc_verified=False, elapsed_seconds=time.monotonic() - start)
            return receipt
        except BaseException as exc:
            failure = exc
            receipt.update(error=str(exc), outcome_unknown=self._broken)
            exc.file_transfer = receipt
            raise
        finally:
            if token and not close_sent and not self._broken:
                try:
                    self.empty(47, struct.pack("<I", token))
                except Exception as close_error:
                    if failure is None:
                        raise
                    receipt["close_error"] = str(close_error)

    def desktop_status(self):
        self._require_feature(0x400, "desktop control")
        state = words(self.rpc(52), DESKTOP_FIELDS, "DESKTOP_STATUS")
        if state["state"] >= len(DESKTOP_STATES) or state["kind"] > 2 or state["menu_idle"] > 1:
            raise ProtocolError("Desktop status bounds mismatch")
        state["state_name"] = DESKTOP_STATES[state["state"]]
        return state

    def _desktop_action(self, kind, body, wait_seconds):
        if not math.isfinite(wait_seconds) or not .1 <= wait_seconds <= 3600:
            raise ValueError("Desktop wait must be 0.1..3600 seconds")
        before = self.desktop_status()
        if not before["menu_idle"] or before["state"] in (1, 2, 3):
            raise RuntimeError("Desktop is not idle; close the foreground application before issuing this command")
        receipt = {"operation": "desktop_refresh" if kind == 1 else "desktop_run", "before": before,
                   "completed": False, "request_sent": True}
        start = time.monotonic()
        try:
            queued = words(self.rpc(50 if kind == 1 else 51, body), ("job_id",), "DESKTOP_QUEUE")
            if not queued["job_id"]:
                raise ProtocolError("Desktop queue returned an invalid job id")
            receipt.update(queued)
            deadline = time.monotonic() + wait_seconds
            while True:
                state = self.desktop_status()
                receipt["status"] = state
                if state["job_id"] != queued["job_id"] or state["kind"] != kind:
                    raise ProtocolError("Desktop job identity changed")
                if state["state"] == 5:
                    raise RuntimeError("Desktop job failed: " + str(state["error"]))
                if kind == 1 and state["state"] == 4:
                    break
                if kind == 2 and state["state"] in (3, 4):
                    if state["entry_count"] == before["entry_count"]:
                        raise ProtocolError("Desktop reports execution without a new payload entry")
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Desktop outcome is unconfirmed; query desktop status if the service is still connected, otherwise check the device; do not repeat run/refresh")
                time.sleep(.02)
            receipt.update(completed=True, payload_started=kind == 2,
                           elapsed_seconds=time.monotonic() - start)
            return receipt
        except BaseException as exc:
            receipt.update(error=str(exc), outcome_unknown=self._broken or
                           (not isinstance(exc, DeviceError) and receipt.get("status", {}).get("state") != 5))
            exc.desktop_job = receipt
            raise

    def desktop_refresh(self, *, wait_seconds=30):
        return self._desktop_action(1, b"", wait_seconds)

    def desktop_run(self, path, *, wait_seconds=30):
        path, encoded = device_path(path, desktop=True)
        return {"path": path, **self._desktop_action(2, encoded, wait_seconds)}

    def deploy(self, source, target, *, run=True, wait_seconds=120, receipt_path=None):
        # Freeze local input before validation. A rebuild at the original path
        # cannot change the bytes after validation or during the upload.
        source = Path(source)
        if source.suffix.lower() != ".bda":
            raise ValueError("Deploy requires a complete .bda file, not an EXEC module")
        with tempfile.TemporaryDirectory(prefix="h1-deploy-") as directory:
            snapshot = Path(directory) / source.name
            with source.open("rb") as incoming, snapshot.open("xb") as frozen:
                shutil.copyfileobj(incoming, frozen, 65536)
            return self._deploy_snapshot(snapshot, source, target, run, wait_seconds, receipt_path)

    def _deploy_snapshot(self, snapshot, source, target, run, wait_seconds, receipt_path):
        validation = validate_bda_file(snapshot)
        validation["path"] = str(source.resolve())
        target, _ = device_path(target, for_put=True, desktop=True)
        before = self.desktop_status()
        if not before["menu_idle"] or before["state"] in (1, 2, 3):
            raise RuntimeError("Deploy needs an idle desktop; close the foreground application first")
        receipt = {"operation": "deploy", "source": str(Path(source).resolve()), "target": target,
                   "bda": validation, "completed": False}
        save_receipt(receipt_path, receipt)
        try:
            upload_receipt = Path(str(receipt_path) + ".upload.json") if receipt_path else None
            receipt["upload"] = self.file_put(snapshot, target, wait_seconds=wait_seconds, receipt_path=upload_receipt)
            receipt["upload"]["original_source"] = str(source.resolve())
            save_receipt(receipt_path, receipt)
            receipt["refresh"] = self.desktop_refresh(wait_seconds=wait_seconds)
            save_receipt(receipt_path, receipt)
            if run:
                receipt["run"] = self.desktop_run(target, wait_seconds=wait_seconds)
            receipt["completed"] = True
            save_receipt(receipt_path, receipt)
            return receipt
        except BaseException as exc:
            receipt["error"] = str(exc)
            for name in ("file_transfer", "desktop_job"):
                if hasattr(exc, name):
                    receipt[name] = getattr(exc, name)
            save_failure_receipt(receipt_path, receipt)
            exc.deployment = receipt
            raise

    def stop(self):
        self.empty(10)
        self.close()
        return {"stop_acknowledged": True}

    def exec_info(self):
        info = words(self.rpc(26), INFO_FIELDS, "EXEC_INFO")
        if (info["abi"] != 1 or info["image_capacity"] not in (65536, 524288) or
                info["scratch_capacity"] != 16384 or info["stack_capacity"] != 32768 or
                info["output_capacity"] != 4096 or info["forced_preemption"] or
                info["api_size"] != 28 or info["base"] % 32 or
                not 0x80000020 <= info["base"] < info["base"] + info["image_capacity"] <= 0x84000000):
            raise ProtocolError("Unsupported native module ABI/address")
        return info

    def upload(self, metadata, data):
        data = bytes(data)
        info = self.exec_info()
        if info["state"] in (3, 4):
            raise RuntimeError("A native module is still active")
        if (metadata["abi"] != info["abi"] or metadata["base"] != info["base"] or
                len(data) != metadata["image_size"] or sha(data) != metadata["sha256"] or
                zlib.crc32(data) != metadata["crc32"] or
                not 4 <= len(data) <= metadata["memory_size"] <= info["image_capacity"] or
                metadata["entry_offset"] % 4 or not 0 <= metadata["entry_offset"] <= len(data) - 4):
            raise ValueError("Compiled module does not match device RAM/package")
        self.empty(27, struct.pack("<5I", len(data), metadata["memory_size"],
                                  metadata["entry_offset"], metadata["crc32"], 1))
        for offset in range(0, len(data), 508):
            self.empty(28, struct.pack("<I", offset) + data[offset:offset + 508])
        self.empty(29)
        ready = self.exec_info()
        if (ready["state"], ready["image_size"], ready["memory_size"], ready["written"],
                ready["upload_crc"]) != (2, len(data), metadata["memory_size"], len(data), metadata["crc32"]):
            raise ProtocolError("Module commit readback mismatch")
        return ready

    def exec_result(self):
        data = self.rpc(31)
        if len(data) != 60:
            raise ProtocolError("EXEC_RESULT length mismatch")
        result = dict(zip(RESULT_FIELDS, struct.unpack("<14Ii", data)))
        if result["state"] > 5 or result["output_size"] > 4096:
            raise ProtocolError("EXEC_RESULT bounds mismatch")
        result["flag_names"] = [name for bit, name in EXEC_FLAGS.items() if result["flags"] & bit]
        result["elapsed_ticks"] = (result["end_tick"] - result["start_tick"]) & 0xFFFFFFFF if result["state"] == 5 else None
        result["tick_units"] = "raw uncalibrated device ticks"
        return result

    def exec_output(self, result=None):
        result = result or self.exec_result()
        if result["state"] != 5:
            raise RuntimeError("Module output requires a completed execution")
        output = bytearray()
        while len(output) < result["output_size"]:
            wanted = min(504, result["output_size"] - len(output))
            block = self.rpc(33, struct.pack("<IH", len(output), wanted))
            if (not 8 < len(block) <= 8 + wanted or
                    struct.unpack_from("<II", block) != (len(output), result["output_size"])):
                raise ProtocolError("Module output token/length mismatch")
            output.extend(block[8:])
        return bytes(output)

    def cancel(self):
        self.empty(32)
        return {"cancel_requested": True, "forced_preemption": False}

    def reset_module(self):
        self.empty(34)
        return self.exec_info()

    def run_module(self, opcode=0, args=b"", budget_ticks=80, *, wait_seconds=15.0, poll_interval=.02):
        opcode = bounded(opcode, 0, 0xFFFFFFFF, "module opcode")
        budget_ticks = bounded(budget_ticks, 1, 400, "module budget ticks")
        args = bytes(args)
        if (len(args) > 256 or not math.isfinite(wait_seconds) or not 0 < wait_seconds <= 3600 or
                not math.isfinite(poll_interval) or not .001 <= poll_interval <= .25):
            raise ValueError("Invalid module arguments or host wait interval")
        start = time.perf_counter()
        self.empty(30, struct.pack("<II", opcode, budget_ticks) + args)
        deadline = time.monotonic() + wait_seconds
        cancelled = False
        while True:
            try:
                result = self.exec_result()
            except TimeoutError as exc:
                raise TimeoutError("Execution is unconfirmed: no USB reply; this can be transport loss, debugger stop/reset, or native code not polling. Reconnect explicitly and inspect usb-diag/retained results; do not automatically rerun the module") from exc
            if result["state"] == 5:
                break
            if result["state"] not in (3, 4):
                raise ProtocolError("RUN lost execution state")
            if time.monotonic() >= deadline:
                if cancelled:
                    raise TimeoutError("Cooperative cancellation is unconfirmed; native execution cannot be forcibly stopped")
                self.cancel()
                cancelled = True
                deadline = time.monotonic() + 2
            time.sleep(poll_interval)
        result["host_elapsed_seconds"] = time.perf_counter() - start
        result["host_elapsed_includes_usb_and_polling"] = True
        result["execution_returned"] = True
        output = self.exec_output(result)
        result["output_sha256"] = sha(output)
        return result, output

    def calibrate_ticks(self, seconds=2.0):
        if not math.isfinite(seconds) or not .1 <= seconds <= 10:
            raise ValueError("Calibration interval must be 0.1..10 seconds")
        before_start = time.perf_counter()
        before = self.status()["tick"]
        before_end = time.perf_counter()
        time.sleep(seconds)
        after_start = time.perf_counter()
        after = self.status()["tick"]
        after_end = time.perf_counter()
        ticks = (after - before) & 0xFFFFFFFF
        elapsed = (after_start + after_end - before_start - before_end) / 2
        if not ticks or ticks >= 0x80000000:
            raise RuntimeError("Device tick did not advance consistently during calibration")
        return {"estimated_tick_hz": ticks / elapsed, "ticks": ticks,
                "host_midpoint_seconds": elapsed,
                "usb_boundary_uncertainty_seconds": (before_end - before_start + after_end - after_start) / 2,
                "method": "PC monotonic clock estimate; USB latency and tick quantization apply"}


def run_tool(args):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout or "Module compiler failed")
    return result.stdout


def build_module(source, base, directory, image_capacity=65536):
    """Build the existing module_abi.h contract without importing an RF client."""
    source, directory = Path(source).resolve(), Path(directory).resolve()
    base = integer(base)
    if (image_capacity not in (65536, 524288) or not source.is_file() or base % 32 or
            not 0x80000020 <= base < base + image_capacity <= 0x84000000):
        raise ValueError("Invalid source, cached module base, or image capacity")
    directory.mkdir(parents=True, exist_ok=True)
    toolchain = ROOT / ".tools/toolchain/bin"
    gcc = toolchain / "mipsel-none-elf-gcc.exe"
    linker, elf, raw = directory / "module.ld", directory / "module.elf", directory / "module.bin"
    linker.write_text("""ENTRY(h1_module_entry)
SECTIONS {
 . = BASE;
 __module_base = .;
 .text : { KEEP(*(.text.h1_module_entry)) *(.text*) }
 .rodata : { *(.rodata*) }
 .data : { *(.data*) *(.sdata*) }
 . = ALIGN(16); __module_image_end = .;
 .bss (NOLOAD) : { *(.bss*) *(.sbss*) *(COMMON) . = ALIGN(16); }
 __module_memory_end = .;
 ASSERT(__module_memory_end - __module_base <= CAPACITY, "Module exceeds RAM arena")
 /DISCARD/ : { *(.comment*) *(.note*) *(.MIPS.abiflags*) *(.reginfo*) *(.eh_frame*) }
}
""".replace("BASE", hex(base)).replace("CAPACITY", str(image_capacity)), encoding="ascii")
    flags = ["-EL", "-march=mips32", "-mabi=32", "-msoft-float", "-mno-abicalls", "-G0",
             "-fno-pic", "-O2", "-ffreestanding", "-fno-builtin", "-fno-common",
             "-fno-stack-protector", "-ffunction-sections", "-fdata-sections",
             "-fno-strict-aliasing", "-Wall", "-Wextra", "-Werror",
             "-I", ROOT / "experiments/h1-usb-runner", "-I", ROOT / "vendor/h1-sdk/sdk/include",
             "-MMD", "-MF", directory / "module.d", "-ffile-prefix-map=" + str(ROOT) + "=h1-module"]
    run_tool([gcc, *flags, "-c", source, "-o", directory / "module.o"])
    run_tool([gcc, *flags, "-nostdlib", "-Wl,--build-id=none", "-Wl,--gc-sections",
              f"-Wl,-T,{linker}", f"-Wl,-Map,{directory}/module.map", "-o", elf,
              directory / "module.o", "-lgcc"])
    if run_tool([toolchain / "mipsel-none-elf-nm.exe", "-u", elf]).strip():
        raise RuntimeError("Unresolved native module imports")
    symbols = {line.split()[-1]: int(line.split()[0], 16) for line in
               run_tool([toolchain / "mipsel-none-elf-nm.exe", elf]).splitlines() if len(line.split()) == 3}
    run_tool([toolchain / "mipsel-none-elf-objcopy.exe", "-O", "binary", elf, raw])
    image_size = symbols["__module_image_end"] - base
    memory_size = symbols["__module_memory_end"] - base
    data = raw.read_bytes()
    if len(data) > image_size or not 4 <= image_size <= memory_size <= image_capacity:
        raise RuntimeError("Unexpected native module image layout")
    data = data.ljust(image_size, b"\0")
    raw.write_bytes(data)
    metadata = {"abi": 1, "base": base, "image_capacity": image_capacity,
                "image_size": image_size, "memory_size": memory_size,
                "entry_offset": symbols["h1_module_entry"] - base, "crc32": zlib.crc32(data),
                "sha256": sha(data), "ELF_sha256": sha(elf.read_bytes()), "source": str(source),
                "source_sha256": sha(source.read_bytes()),
                "ABI_sha256": sha((ROOT / "experiments/h1-usb-runner/module_abi.h").read_bytes()),
                "binary": str(raw), "hardware_verified": False, "sandbox": False,
                "forced_preemption": False}
    (directory / "module.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata, data


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", help="An enumerated H1 VID/PID port; arbitrary COM ports are never opened")
    p.add_argument("--timeout", type=float, default=4.0, help="Per-RPC deadline in seconds")
    p.add_argument("--report", type=Path, help="Also save JSON result")
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("list", "ping", "status", "caps", "usb-diag", "release", "stop", "result", "cancel", "touch-status", "release-touch"):
        sub.add_parser(command)
    q = sub.add_parser("read"); q.add_argument("address", type=integer); q.add_argument("count", type=integer); q.add_argument("--output", type=Path)
    q = sub.add_parser("write"); q.add_argument("address", type=integer); q.add_argument("file", type=Path, nargs="?"); q.add_argument("--hex", dest="hex_data")
    q = sub.add_parser("keys"); q.add_argument("keys", type=key_number, nargs="+"); q.add_argument("--ticks", type=integer, default=8)
    q = sub.add_parser("press"); q.add_argument("keys", type=key_number, nargs="+"); q.add_argument("--ticks", type=integer, default=8)
    q = sub.add_parser("touch"); q.add_argument("x", type=integer); q.add_argument("y", type=integer); q.add_argument("--ticks", type=integer, default=80)
    q = sub.add_parser("tap"); q.add_argument("x", type=integer); q.add_argument("y", type=integer); q.add_argument("--seconds", type=float, default=.12); q.add_argument("--ticks", type=integer, default=80)
    q = sub.add_parser("swipe"); q.add_argument("x0", type=integer); q.add_argument("y0", type=integer); q.add_argument("x1", type=integer); q.add_argument("y1", type=integer)
    q.add_argument("--seconds", type=float, default=.5); q.add_argument("--steps", type=integer, default=12); q.add_argument("--ticks", type=integer, default=80)
    q = sub.add_parser("screenshot"); q.add_argument("output", type=Path)
    q.add_argument("--raw", action="store_true", help="Force uncompressed transfer for benchmarking")
    q = sub.add_parser("output"); q.add_argument("--output", type=Path)
    q = sub.add_parser("calibrate"); q.add_argument("--seconds", type=float, default=2.0)
    files = sub.add_parser("file", help="Transfer and manage files on the device filesystem").add_subparsers(dest="file_command", required=True)
    files.add_parser("status")
    q = files.add_parser("stat"); q.add_argument("path")
    q = files.add_parser("list", help="List one directory; no recursive traversal")
    q.add_argument("path"); q.add_argument("--max-entries", type=integer, default=10000)
    for action in ("delete", "mkdir", "rmdir"):
        q = files.add_parser(action); q.add_argument("path")
    q = files.add_parser("rename", help="Rename or move within one volume without replacing a destination")
    q.add_argument("source"); q.add_argument("target")
    q = files.add_parser("paths"); q.add_argument("token", type=integer)
    q = files.add_parser("abort"); q.add_argument("token", type=integer); q.add_argument("--wait-seconds", type=float, default=30)
    q = files.add_parser("get"); q.add_argument("source"); q.add_argument("output", type=Path)
    q = files.add_parser("put"); q.add_argument("source", type=Path); q.add_argument("target")
    q.add_argument("--wait-seconds", type=float, default=120); q.add_argument("--receipt", type=Path)
    desktop = sub.add_parser("desktop").add_subparsers(dest="desktop_command", required=True)
    desktop.add_parser("status")
    q = desktop.add_parser("refresh"); q.add_argument("--wait-seconds", type=float, default=30)
    q = desktop.add_parser("run"); q.add_argument("path"); q.add_argument("--wait-seconds", type=float, default=30)
    q = sub.add_parser("deploy", help="Validate/upload a complete BDA, refresh the desktop, and confirm payload entry")
    q.add_argument("source", type=Path); q.add_argument("target"); q.add_argument("--no-run", action="store_true")
    q.add_argument("--wait-seconds", type=float, default=120); q.add_argument("--receipt", type=Path)
    ex = sub.add_parser("exec").add_subparsers(dest="exec_command", required=True)
    for command in ("info", "result", "cancel", "reset"):
        ex.add_parser(command)
    q = ex.add_parser("output"); q.add_argument("--output", type=Path)
    q = ex.add_parser("compile"); q.add_argument("source", type=Path); q.add_argument("--base", type=integer, required=True); q.add_argument("--directory", type=Path, required=True)
    q = ex.add_parser("upload"); q.add_argument("metadata", type=Path)
    def execution_args(q):
        q.add_argument("source", type=Path, nargs="?")
        q.add_argument("--directory", type=Path, default=ROOT / "build/h1-debug-module")
        q.add_argument("--opcode", type=integer, default=0)
        q.add_argument("--args-hex", default="")
        q.add_argument("--budget-ticks", type=integer, default=80)
        q.add_argument("--wait-seconds", type=float, default=15.0)
        q.add_argument("--output", type=Path)
    execution_args(ex.add_parser("run"))
    q = sub.add_parser("benchmark"); execution_args(q); q.add_argument("--repeat", type=integer, default=5); q.add_argument("--calibrate-seconds", type=float)
    return p


def binary_result(data, output, **extra):
    result = {**extra, "bytes": len(data), "sha256": sha(data)}
    if output:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        result["output"] = str(output.resolve())
    else:
        result["hex"] = data.hex()
    return result


def execute(a):
    command = a.exec_command if a.command == "exec" else a.command
    if a.command == "list":
        return {"ports": [{"port": p.device, "description": p.description,
                            "vid": p.vid, "pid": p.pid} for p in ports()]}
    if command == "compile":
        metadata, _ = build_module(a.source, a.base, a.directory)
        return metadata
    # Validate local inputs before touching USB.
    if command == "file":
        if a.file_command in ("stat", "get"):
            device_path(a.path if a.file_command == "stat" else a.source)
        if a.file_command == "put":
            device_path(a.target, for_put=True)
            if not a.source.is_file():
                raise ValueError("Upload source is not a file")
        if a.file_command == "list":
            device_path(a.path, directory=True)
            bounded(a.max_entries, 1, 1000000, "directory entry limit")
        if a.file_command in ("delete", "mkdir", "rmdir"):
            device_path(a.path, leaf=True)
        if a.file_command == "rename":
            rename_paths(a.source, a.target)
        if a.file_command in ("paths", "abort"):
            bounded(a.token, 1, 0xFFFFFFFF, "file token")
    if command == "desktop" and a.desktop_command == "run":
        device_path(a.path, desktop=True)
    if command == "deploy":
        device_path(a.target, for_put=True, desktop=True)
        validate_bda_file(a.source)
    if command in ("file", "desktop", "deploy") and hasattr(a, "wait_seconds"):
        if not math.isfinite(a.wait_seconds) or not .1 <= a.wait_seconds <= 3600:
            raise ValueError("Completion wait must be 0.1..3600 seconds")
    if command == "deploy" or (command == "file" and a.file_command == "put"):
        if a.receipt is None:
            a.receipt = ROOT / "build/h1-debug-transfers" / (str(time.time_ns()) + ".json")
    if command == "write":
        if (a.file is None) == (a.hex_data is None):
            raise ValueError("Supply exactly one input file or --hex")
        write_data = a.file.read_bytes() if a.file else bytes.fromhex(a.hex_data)
    if command in ("run", "benchmark"):
        args = bytes.fromhex(a.args_hex)
        bounded(a.opcode, 0, 0xFFFFFFFF, "module opcode")
        bounded(a.budget_ticks, 1, 400, "module budget ticks")
        if len(args) > 256:
            raise ValueError("Module arguments exceed 256 bytes")
    if command == "benchmark":
        bounded(a.repeat, 1, 1000, "benchmark repeat count")
    with Client(a.port, timeout=a.timeout) as client:
        if command == "file":
            if a.file_command == "status": return client.file_status()
            if a.file_command == "stat": return client.file_stat(a.path)
            if a.file_command == "list": return client.file_list(a.path, max_entries=a.max_entries)
            if a.file_command == "delete": return client.file_delete(a.path)
            if a.file_command == "rename": return client.file_rename(a.source, a.target)
            if a.file_command == "mkdir": return client.file_mkdir(a.path)
            if a.file_command == "rmdir": return client.file_rmdir(a.path)
            if a.file_command == "paths": return client.file_paths(a.token)
            if a.file_command == "abort": return client.file_abort(a.token, wait_seconds=a.wait_seconds)
            if a.file_command == "get": return client.file_get(a.source, a.output)
            if a.file_command == "put":
                return {**client.file_put(a.source, a.target, wait_seconds=a.wait_seconds, receipt_path=a.receipt),
                        "receipt": str(a.receipt.resolve())}
        if command == "desktop":
            if a.desktop_command == "status": return client.desktop_status()
            if a.desktop_command == "refresh": return client.desktop_refresh(wait_seconds=a.wait_seconds)
            if a.desktop_command == "run": return client.desktop_run(a.path, wait_seconds=a.wait_seconds)
        if command == "deploy":
            return {**client.deploy(a.source, a.target, run=not a.no_run,
                                  wait_seconds=a.wait_seconds, receipt_path=a.receipt),
                    "receipt": str(a.receipt.resolve())}
        if command == "ping": return {"port": client.port, "identity": client.identity}
        if command == "status": return client.status()
        if command == "usb-diag": return client.usb_diagnostics()
        if command == "caps": return client.capabilities()
        if command == "read": return binary_result(client.read_memory(a.address, a.count), a.output, address=a.address)
        if command == "write": return client.write_memory(a.address, write_data)
        if command in ("keys", "press"): return client.press(a.keys, a.ticks)
        if command == "release": return client.release()
        if command == "touch": return client.touch(a.x, a.y, lease_ticks=a.ticks)
        if command == "touch-status": return client.touch_status()
        if command == "release-touch": return client.release_touch()
        if command == "tap": return client.tap(a.x, a.y, hold_seconds=a.seconds, lease_ticks=a.ticks)
        if command == "swipe": return client.swipe(a.x0, a.y0, a.x1, a.y1, duration_seconds=a.seconds, steps=a.steps, lease_ticks=a.ticks)
        if command == "screenshot": return client.screenshot(a.output, codec="raw" if a.raw else "auto")
        if command == "stop": return client.stop()
        if command == "info": return client.exec_info()
        if command == "result": return client.exec_result()
        if command == "output": return binary_result(client.exec_output(), a.output)
        if command == "cancel": return client.cancel()
        if command == "reset": return client.reset_module()
        if command == "calibrate": return client.calibrate_ticks(a.seconds)
        if command == "upload":
            metadata = json.loads(a.metadata.read_text(encoding="utf-8-sig"))
            path = Path(metadata["binary"])
            if not path.is_absolute(): path = a.metadata.resolve().parent / path
            return client.upload(metadata, path.read_bytes())
        info = client.exec_info()
        receipt = {}
        if a.source:
            metadata, data = build_module(a.source, info["base"], a.directory, info["image_capacity"])
            receipt.update(module=metadata, upload=client.upload(metadata, data))
        calibration = client.calibrate_ticks(a.calibrate_seconds) if command == "benchmark" and a.calibrate_seconds is not None else None
        runs = []
        for _ in range(a.repeat if command == "benchmark" else 1):
            result, output = client.run_module(a.opcode, args, a.budget_ticks, wait_seconds=a.wait_seconds)
            runs.append(result)
            if result["flags"]:
                break
        receipt.update(result=runs[-1], output=binary_result(output, a.output))
        if command == "benchmark":
            ticks = [r["elapsed_ticks"] for r in runs]
            receipt.update(runs=runs, requested_repeats=a.repeat, completed_repeats=len(runs),
                           raw_ticks={"min": min(ticks), "median": statistics.median(ticks), "max": max(ticks)},
                           calibration=calibration)
            if calibration:
                receipt["estimated_seconds"] = [tick / calibration["estimated_tick_hz"] for tick in ticks]
        if a.source:
            a.directory.mkdir(parents=True, exist_ok=True)
            (a.directory / "execution.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        return receipt


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        result = {"ok": True, **execute(a)}
        if a.report:
            a.report.parent.mkdir(parents=True, exist_ok=True)
            a.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        error = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, DeviceError):
            error.update(opcode=exc.op, status=exc.status, payload_hex=exc.payload.hex())
        for name in ("touch_release_error", "file_transfer", "file_status", "file_listing", "file_management",
                     "desktop_job", "deployment"):
            if hasattr(exc, name):
                error[name] = getattr(exc, name)
        if getattr(a, "receipt", None):
            error["receipt"] = str(a.receipt.resolve())
        if a.report:
            try:
                save_receipt(a.report, error)
            except OSError as report_error:
                error["report_error"] = str(report_error)
        print(json.dumps(error, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
