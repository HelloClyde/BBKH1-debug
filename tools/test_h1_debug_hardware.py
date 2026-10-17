"""Staged H1 resident debugger hardware acceptance with raw USB evidence.

Use `prepare` to compile the probes without opening USB. All other stages open
exactly one enumerated H1 debug VID/PID port and validate the resident identity.
The memory stage writes only a fresh, explicitly owned firmware-heap allocation;
its cleanup receipt survives transport failures. This tool never stops the BDA.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import struct
import sys
import time

import h1_debug as h

ROOT = Path(__file__).resolve().parents[1]
OWNED_SIZE = 512
OWNED_MAGIC = 0x314D454D
ALLOC, INSPECT, FREE = 0xD001, 0xD002, 0xD003
OWNED_SOURCE = r'''/* Owns exactly one untracked firmware-heap allocation. */
#include "module_abi.h"
static uint8_t *owned;
static uint32_t cookie_lo,cookie_hi;
static uint32_t get32(const uint8_t *p){
 return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
}
static uint32_t digest(const uint8_t *p){
 uint32_t v=2166136261u;
 for(unsigned i=0;i<512;++i)v=(v^p[i])*16777619u;
 return v;
}
int h1_module_entry(const h1_exec_api *a,h1_exec_call *c){
 if(a->version!=1 || a->size<28 || c->version!=1 || c->output_capacity<28)return -1;
 if(c->arg_size!=8)return -2;
 uint32_t lo=get32(c->args),hi=get32(c->args+4);
 if(owned && ((uint32_t)(uintptr_t)owned<0x80004000u ||
              (uint32_t)(uintptr_t)owned>0x83c00000u-512u))return -6;
 uint32_t table=a->runtime_table(0x83c00010u);
 uint32_t alloc=a->runtime_entry(table,8),release=a->runtime_entry(table,12);
 /* V1.41 SDK MEM+008/+00c: untracked global alloc/free, not BDA wrappers. */
 if(alloc!=0x800dc998u || release!=0x800dbb54u)return -3;
 if(c->opcode==0xd001u){
  if(owned || !(lo|hi))return -4;
  cookie_lo=lo;cookie_hi=hi;
  owned=((uint8_t *(*)(uint32_t))(uintptr_t)alloc)(512);
  if(!owned)return -5;
  uint32_t address=(uint32_t)(uintptr_t)owned;
  if(address<0x80004000u || address>0x83c00000u-512u)return -6;
  for(unsigned i=0;i<512;++i)owned[i]=(uint8_t)(i^0xa5u);
 }else if(c->opcode==0xd003u){
  if(owned && (lo!=cookie_lo || hi!=cookie_hi))return -7;
  if(owned){((void (*)(void *))(uintptr_t)release)(owned);owned=0;}
 }else if(c->opcode!=0xd002u)return -8;
 uint32_t *out=(uint32_t *)(void *)c->output;
 out[0]=0x314d454du;out[1]=cookie_lo;out[2]=cookie_hi;
 out[3]=(uint32_t)(uintptr_t)owned;out[4]=512;out[5]=owned?1u:0u;
 out[6]=owned?digest(owned):0u;c->output_size=28;return 0;
}
'''


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A completed replacement preserves the previous recovery receipt on error.
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def initial_bytes():
    return bytes((i ^ 0xA5) & 255 for i in range(OWNED_SIZE))


def replacement_bytes():
    return bytes((i * 73 + 19) & 255 for i in range(OWNED_SIZE))


def digest(data):
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class WireRecorder:
    """Record exact serial read/write chunks, including failed RPC evidence."""
    def __init__(self, serial, path):
        self.serial = serial
        self.path = Path(path)
        self.log = self.path.open("x", encoding="utf-8")
        self.closed = False

    def record(self, kind, **values):
        self.log.write(json.dumps({"kind": kind, "monotonic_ns": time.monotonic_ns(),
                                   "utc": datetime.now(timezone.utc).isoformat(), **values}) + "\n")
        self.log.flush()

    def reset_input_buffer(self):
        self.record("reset_input_buffer")
        self.serial.reset_input_buffer()

    def write(self, data):
        self.record("write_attempt", data_hex=bytes(data).hex(), bytes=len(data))
        count = self.serial.write(data)
        self.record("write_completed", bytes=count)
        return count

    def read(self, count):
        data = self.serial.read(count)
        if data:
            self.record("read", data_hex=bytes(data).hex(), bytes=len(data))
        return data

    def close(self):
        if self.closed:
            return
        try:
            self.serial.close()
        finally:
            self.record("close")
            self.log.close()
            self.closed = True


def connect(port, timeout, wire_path):
    require(.01 <= timeout <= 60, "RPC timeout must be 0.01..60 seconds")
    candidates = h.ports()
    if port:
        candidates = [p for p in candidates if p.device.casefold() == port.casefold()]
    require(len(candidates) == 1, "Expected exactly one matching H1 USB port: " + str([p.device for p in candidates]))
    serial, _ = h.serial_module()
    device = candidates[0].device
    raw = serial.Serial(device, 115200, timeout=min(.1, timeout), write_timeout=min(2.0, timeout))
    try:
        recorder = WireRecorder(raw, wire_path)
    except BaseException:
        raw.close()
        raise
    return h.Client(device, timeout=timeout, transport=recorder)


def owned_reply(output):
    require(len(output) == 28, "Owned allocation module reply length mismatch")
    magic, lo, hi, pointer, size, active, checksum = struct.unpack("<7I", output)
    require(magic == OWNED_MAGIC and size == OWNED_SIZE and active in (0, 1), "Owned allocation module reply mismatch")
    require(bool(pointer) == bool(active), "Owned allocation pointer/state mismatch")
    return {"cookie": [lo, hi], "pointer": pointer, "size": size, "active": bool(active), "digest": checksum}


def run_owned(client, opcode, cookie):
    result, output = client.run_module(opcode, struct.pack("<2I", *cookie), budget_ticks=80)
    require(result["flags"] == 0 and result["return_value"] == 0,
            "Owned allocation module failed: " + json.dumps(result))
    return owned_reply(output), result


def guard_no_allocation(state_path):
    path = Path(state_path)
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        require(state.get("released") is True,
                f"An owned allocation may still exist. Run cleanup with --state {path} before uploading another module")


def verify_owner(client, state):
    info, status = client.exec_info(), client.status()
    require((status["resident_base"], status["resident_size"]) == tuple(state["resident"]),
            "Resident allocation changed; cleanup refused")
    expected = state["module"]
    require((info["base"], info["image_size"], info["memory_size"], info["upload_crc"], info["written"]) ==
            (expected["base"], expected["image_size"], expected["memory_size"], expected["crc32"], expected["image_size"]),
            "Owned module was replaced or upload differs; cleanup refused")
    require(info["state"] in (2, 5), "Owned module is not ready/completed; cleanup refused")
    receipt, result = run_owned(client, INSPECT, state["cookie"])
    if receipt["active"]:
        require(receipt["cookie"] == state["cookie"], "Owned allocation cookie differs; cleanup refused")
        pointer, size = receipt["pointer"], receipt["size"]
        require(0x80004000 <= pointer <= 0x83C00000 - size, "Owned heap address is outside the supported arena")
        base, resident_size = state["resident"]
        require(pointer + size <= base or pointer >= base + resident_size,
                "Allocator returned debugger-protected memory; write refused")
        require(state.get("pointer") in (None, pointer), "Owned pointer changed; cleanup refused")
    return receipt, result


def cleanup_owned(client, state_path):
    path = Path(state_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("released"):
        return {"released": True, "already_released": True}
    receipt, inspection = verify_owner(client, state)
    state["last_inspection"] = receipt
    save_json(path, state)
    if not receipt["active"]:
        state.update(released=True, cleanup="Module confirms no live owned allocation")
        save_json(path, state)
        return {"released": True, "allocation_was_active": False, "inspection": inspection}
    pointer = receipt["pointer"]
    original = initial_bytes()
    client.write_memory(pointer, original)
    require(client.read_memory(pointer, OWNED_SIZE) == original, "Could not verify restored allocation bytes")
    state["restored"] = True
    save_json(path, state)
    released, execution = run_owned(client, FREE, state["cookie"])
    require(not released["active"], "Module did not confirm allocation release")
    state.update(released=True, cleanup="Original bytes restored, allocator free completed", free_result=execution)
    save_json(path, state)
    return {"released": True, "restored": True, "pointer": pointer,
            "bytes": OWNED_SIZE, "free_execution": execution}


def prepare(directory, base=0x81000020):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "owned_memory.c"
    source.write_text(OWNED_SOURCE, encoding="ascii")
    owned, _ = h.build_module(source, base, directory / "owned")
    compute, _ = h.build_module(ROOT / "h1-debug/examples/compute.c", base, directory / "compute")
    return {"hardware_accessed": False, "owned_memory_module": owned, "compute_module": compute,
            "ownership_policy": "Fresh MEM+008 allocation; restore and MEM+00c free; never guessed RAM"}


def probe(client, directory):
    result = {"identity": client.identity, "port": client.port,
              "caps": client.capabilities(), "status": client.status(), "exec": client.exec_info()}
    resident = result["status"]
    header = client.read_memory(resident["resident_base"], 80)
    (directory / "resident-header.bin").write_bytes(header)
    values = struct.unpack("<20I", header)
    require(values[:2] == (0x31444248, 1) and values[3:5] ==
            (resident["resident_base"], resident["resident_size"]), "Resident header/status mismatch")
    build = json.loads((ROOT / "dist/H1Debug.build.json").read_text(encoding="utf-8"))
    result["resident_build"] = {"image_id": f"{values[19]:08x}", "expected_image_id": build["image_id"],
                                "expected_bda_sha256": build["sha256"]}
    require(f"{values[19]:08x}" == build["image_id"], "Device still runs a different resident build; reboot after installing")
    expected = struct.pack("<3I", 0x3C028058, 0x03E00008, 0x8C42B064)
    raw = client.read_memory(0x80165B04, len(expected))
    (directory / "firmware-tick-entry.bin").write_bytes(raw)
    require(raw == expected, "Raw firmware tick entry differs from V1.41 fingerprint")
    prefix = client.read_memory(0x83C00000, 256)
    (directory / "foreground-prefix.bin").write_bytes(prefix)
    result["reads"] = {"firmware_tick_entry": {"address": 0x80165B04, "hex": raw.hex()},
                       "foreground_prefix": {"address": 0x83C00000, "bytes": len(prefix), "sha256": h.sha(prefix)}}
    require(result["status"]["state"] == 1 and result["status"]["usb_fault"] == 0,
            "Resident is not running without a USB ownership fault")
    return result


def screen(client, directory):
    from PIL import Image
    meta, pixels = client.capture_screen()
    (directory / "screen.raw").write_bytes(pixels)
    save_json(directory / "screen.json", meta)
    Image.frombytes("RGB", (meta["width"], meta["height"]), h.decode_screen(meta, pixels)).save(directory / "screen.png")
    return {"metadata": meta, "raw_sha256": h.sha(pixels), "image": str(directory / "screen.png")}


def expected_compute(seed, args):
    value = seed
    for byte in args:
        value = ((value << 5) ^ (value >> 2) ^ byte) & 0xFFFFFFFF
    return value


def compute(client, directory, state_path):
    guard_no_allocation(state_path)
    info = client.exec_info()
    original = (ROOT / "h1-debug/examples/compute.c").read_text(encoding="utf-8")
    results = []
    for variant, seed in enumerate((0x13579BDF, 0x2468ACE1)):
        build = directory / f"variant-{variant}"
        build.mkdir(parents=True, exist_ok=True)
        source = build / "compute.c"
        source.write_text(original.replace("0x13579bdfu", f"0x{seed:08x}u"), encoding="ascii")
        metadata, data = h.build_module(source, info["base"], build, info["image_capacity"])
        ready = client.upload(metadata, data)
        runs = []
        for expected_run in (1, 2):
            opcode, args = 0x19260000 + expected_run, b"H1 USB compute\0\xff"
            result, output = client.run_module(opcode, args)
            (build / f"output-{expected_run}.bin").write_bytes(output)
            require(result["return_value"] == 0 and result["flags"] == 0, "Compute module returned an error")
            require(len(output) == 16, "Compute output length mismatch")
            counter, value, echoed_opcode, tick = struct.unpack("<4I", output)
            require((counter, value, echoed_opcode) == (expected_run, expected_compute(seed, args), opcode),
                    "Compute output/BSS persistence/instruction replacement mismatch")
            runs.append({"execution": result, "output_words": [counter, value, echoed_opcode, tick]})
        results.append({"module": metadata, "upload": ready, "runs": runs})
    return {"variants": results, "exact_compute_verified": True,
            "bss_persistence_verified": True, "code_replacement_verified": True}


def memory(client, directory, state_path):
    guard_no_allocation(state_path)
    info, status = client.exec_info(), client.status()
    source = directory / "owned_memory.c"
    source.write_text(OWNED_SOURCE, encoding="ascii")
    metadata, data = h.build_module(source, info["base"], directory / "module", info["image_capacity"])
    ready = client.upload(metadata, data)
    cookie = list(struct.unpack("<2I", secrets.token_bytes(8)))
    state = {"version": 1, "released": False, "pointer": None, "cookie": cookie,
             "resident": [status["resident_base"], status["resident_size"]], "module": metadata,
             "created_utc": datetime.now(timezone.utc).isoformat(), "directory": str(directory.resolve())}
    # Persist recovery identity before allocation, even if its reply is lost.
    save_json(state_path, state)
    result = {"module": metadata, "upload": ready, "state_file": str(Path(state_path).resolve())}
    try:
        allocation, execution = run_owned(client, ALLOC, cookie)
        require(allocation["active"] and allocation["cookie"] == cookie, "Module did not return the expected allocation owner")
        state["pointer"] = allocation["pointer"]
        save_json(state_path, state)
        verified, _ = verify_owner(client, state)
        pointer = verified["pointer"]
        original = client.read_memory(pointer, OWNED_SIZE)
        (directory / "owned-original.bin").write_bytes(original)
        require(original == initial_bytes() and verified["digest"] == digest(original), "Allocated original data mismatch")
        replacement = replacement_bytes()
        (directory / "owned-test.bin").write_bytes(replacement)
        client.write_memory(pointer, replacement)
        observed = client.read_memory(pointer, OWNED_SIZE)
        (directory / "owned-readback.bin").write_bytes(observed)
        require(observed == replacement, "Owned heap write/readback mismatch")
        inspected, _ = verify_owner(client, state)
        require(inspected["digest"] == digest(replacement), "Native module does not observe host memory writes")
        result.update(allocation=allocation, execution=execution, bytes=OWNED_SIZE,
                      write_readback_verified=True, native_visibility_verified=True)
    except BaseException as exc:
        state["test_error"] = {"type": type(exc).__name__, "message": str(exc)}
        save_json(state_path, state)
        raise
    finally:
        # Never reconnect/retry a mutation after uncertain transport. The next
        # explicit cleanup command validates module identity and ownership anew.
        if not client._broken:
            result["cleanup"] = cleanup_owned(client, state_path)
    require(result.get("cleanup", {}).get("released"), "Owned heap cleanup was not confirmed; retain the state file")
    return result


def benchmark(client, directory, state_path, iterations):
    guard_no_allocation(state_path)
    require(1 <= iterations <= 1_000_000, "Acceptance benchmark iterations must be 1..1000000")
    info = client.exec_info()
    metadata, data = h.build_module(ROOT / "h1-debug/examples/benchmark.c", info["base"], directory / "module", info["image_capacity"])
    client.upload(metadata, data)
    expected = 0x12345678
    for i in range(iterations):
        expected = ((expected << 7) ^ (expected >> 3) ^ (i + 0x9E3779B9)) & 0xFFFFFFFF
    runs = []
    for repeat in range(3):
        result, output = client.run_module(args=struct.pack("<I", iterations), budget_ticks=400)
        (directory / f"benchmark-{repeat}.bin").write_bytes(output)
        require(result["flags"] == 0 and result["return_value"] == 0 and len(output) == 16,
                "Bounded benchmark execution failed")
        count, value, begin, end = struct.unpack("<4I", output)
        require(count == iterations and value == expected, "Benchmark calculation differs from PC reference")
        runs.append({"execution": result, "inner_ticks": (end - begin) & 0xFFFFFFFF,
                     "count": count, "value": value})
    return {"module": metadata, "runs": runs, "timing_units": "raw device ticks; no frequency assumption"}


def keys(client, directory, names, ticks):
    require(names, "The keys stage requires explicit --key arguments")
    before = screen(client, directory)
    sent = client.press(names, ticks)
    state = client.status()
    released = client.release()
    after_dir = directory / "after"
    after_dir.mkdir()
    after = screen(client, after_dir)
    return {"keys": names, "sent": sent, "status_after_press": state, "release": released,
            "before": before, "after": after,
            "visual_effect_requires_review": True}


def suite(client, directory, state_path, stages, iterations=200000):
    """Keep one COM handle/DTR session across all requested acceptance stages."""
    allowed = {"probe", "screen", "compute", "memory", "benchmark"}
    require(stages and all(stage in allowed for stage in stages), "Unsupported suite stage")
    results = {}
    for index, stage in enumerate(stages):
        child = directory / f"{index + 1:02d}-{stage}"
        child.mkdir()
        if stage == "probe": result = probe(client, child)
        elif stage == "screen": result = screen(client, child)
        elif stage == "compute": result = compute(client, child, state_path)
        elif stage == "memory": result = memory(client, child, state_path)
        elif stage == "benchmark": result = benchmark(client, child, state_path, iterations)
        save_json(child / "result.json", result)
        results[f"{index + 1:02d}-{stage}"] = result
        save_json(directory / "suite-progress.json", results)
    return {"single_usb_session": True, "stages": results}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=("prepare", "suite", "probe", "screen", "compute", "memory", "cleanup", "benchmark", "keys"))
    p.add_argument("--port")
    p.add_argument("--timeout", type=float, default=4.0)
    p.add_argument("--directory", type=Path, default=ROOT / "build/h1-debug/hardware")
    p.add_argument("--state", type=Path, help="Memory ownership recovery file (default DIRECTORY/memory-ownership.json)")
    p.add_argument("--base", type=h.integer, default=0x81000020, help="Offline prepare link base")
    p.add_argument("--key", action="append", help="Explicit physical key name/number, repeat for chords")
    p.add_argument("--ticks", type=h.integer, default=8)
    p.add_argument("--iterations", type=h.integer, default=200000)
    p.add_argument("--stages", nargs="+", default=["probe", "screen", "compute", "memory", "benchmark"],
                   help="Stages executed by suite using one continuous COM handle")
    a = p.parse_args(argv)
    a.directory = a.directory.resolve()
    state_path = a.state.resolve() if a.state else a.directory / "memory-ownership.json"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = a.directory / (a.stage + "-" + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    report = {"stage": a.stage, "started_utc": datetime.now(timezone.utc).isoformat(),
              "directory": str(directory), "hardware_accessed": False,
              "hardware_access_attempted": a.stage != "prepare", "ok": False}
    try:
        if a.stage == "prepare":
            report["result"] = prepare(directory, a.base)
        else:
            with connect(a.port, a.timeout, directory / "wire.jsonl") as client:
                report["hardware_accessed"] = True
                report["identity"] = client.identity
                report["port"] = client.port
                if a.stage == "probe": result = probe(client, directory)
                elif a.stage == "screen": result = screen(client, directory)
                elif a.stage == "compute": result = compute(client, directory, state_path)
                elif a.stage == "memory": result = memory(client, directory, state_path)
                elif a.stage == "cleanup": result = cleanup_owned(client, state_path)
                elif a.stage == "benchmark": result = benchmark(client, directory, state_path, a.iterations)
                elif a.stage == "keys": result = keys(client, directory, a.key, a.ticks)
                elif a.stage == "suite": result = suite(client, directory, state_path, a.stages, a.iterations)
                report["result"] = result
        report["ok"] = True
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    if a.stage != "prepare":
        report["hardware_accessed"] = (directory / "wire.jsonl").exists()
    report["ended_utc"] = datetime.now(timezone.utc).isoformat()
    save_json(directory / "report.json", report)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
