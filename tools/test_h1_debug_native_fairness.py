"""Frozen-SOF fallback and natural foreground progress in a private firmware.

Run after the normal lifecycle/virtual-touch tests, with the retained resident
stopped at the native utilities menu. QEMU has no USB host or packet FIFOs.
GDB prepares one synthetic 512-byte pending TX response and queues a released
virtual tap at the real worker's transport_service entry, then detaches.
The actual worker, IRQ22 chain, scheduler, GUI and Time BDA run naturally.
This is a functional frozen-FRAME check, never a sustained USB benchmark.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'vendor/h1-sdk/scripts'))
import navigate_emulator_game_slot as nav
from h1_debug_gdb import Remote


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--server', default='http://127.0.0.1:8847')
    p.add_argument('--gdb-port', type=int, default=8848)
    p.add_argument('--work-dir', type=Path, required=True)
    a = p.parse_args()
    work = a.work_dir.resolve()
    api = lambda path, data=None: nav.api(a.server, path, data)
    runtime = api('/api/status')
    assert Path(runtime['nand']).resolve() == work / 'h1-system.raw'
    assert Path(runtime['kernel']).resolve() == work / 'project.bin'
    build = json.loads((ROOT / 'dist/H1Debug.build.json').read_text())
    image = json.loads((work / 'image-build.json').read_text())
    digest = hashlib.sha256((ROOT / 'dist/H1Debug.bda').read_bytes()).hexdigest()
    assert image['bda_sha256'] == build['sha256'] == digest

    def words(address, count=1):
        data = api('/api/debug/memory?address=0x%x&count=%d' % (address & 0x1fffffff, count))['memory']
        result = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]{8})', data)]
        assert len(result) == count
        return result

    tcb = words(0x80a6d6f0 + 6 * 4)[0]
    assert tcb
    base = words(tcb + 4)[0]
    local = lambda name: base + build['symbols'][name] - 0x81000000
    # Actual queue consumers/coordinate acknowledgements identify priority10
    # as the native UI task. Priority7 is a separate dormant native service.
    gui_tcb = words(0x80a6d6f0 + 10 * 4)[0]

    def observation():
        names = ('polls', 'service_calls', 'active_windows', 'window_polls_max',
                 'frame_limit_exits', 'iteration_limit_exits', 'tick_limit_exits', 'idle_returns')
        return {'raw_tick': words(0x8057b064)[0], 'state': words(base + 24)[0],
                'gui_switches': words(gui_tcb + 0x3c)[0],
                'bda_prefix': words(0x83c00020, 16),
                **{name: words(local(name))[0] for name in names}}

    assert words(base + 24)[0] == 3
    nav.tap(a.server, 42, 135)
    time.sleep(.8)
    assert words(base + 24)[0] == 1
    nav.press_key(a.server, 25)
    time.sleep(.6)
    before = observation()
    nav.capture(a.server, work / 'fairness-before.png')

    remote = Remote(a.gdb_port)
    original = None
    try:
        remote.command('?')
        boundary = local('transport_service')
        remote.run_to(boundary)
        assert struct.unpack('<I', remote.memory(0x8057adfc, 4))[0] == tcb
        original = remote.command('g')
        regs = list(struct.unpack('<' + 'I' * (len(original) // 8), bytes.fromhex(original)))
        assert regs[37] == boundary and regs[32] & 1 and not regs[32] & 2
        stack = regs[29] - 256
        header = struct.unpack('<20I', remote.memory(base, 80))
        assert stack - 4096 > header[13]
        tick = struct.unpack('<I', remote.memory(0x8057b064, 4))[0]
        assert remote.command(f'M{stack + 16:x},4:' + struct.pack('<I', tick).hex()) == 'OK'
        for x, y, pressed in [(105, 52, 1), (105, 52, 0)]:
            edited = regs[:]
            edited[4:8] = [x, y, pressed, 4000]
            edited[29], edited[31], edited[37] = stack, boundary, local('rd_touch_set')
            assert remote.command('G' + struct.pack('<' + 'I' * len(edited), *edited).hex()) == 'OK'
            remote.breakpoint(boundary, True)
            remote.command('c')
            finished = bytes.fromhex(remote.command('g'))
            assert struct.unpack_from('<I', finished, 8)[0] == 0
            assert struct.unpack_from('<I', finished, 37 * 4)[0] == boundary
            remote.breakpoint(boundary, False)
            assert remote.command('G' + original) == 'OK'
        # The layout is the released debug_usb / debug_protocol ABI. Verify
        # owned/configured bytes before the only synthetic transport change.
        assert remote.memory(local('usb') + 52, 2) == bytes([1, 0])
        assert remote.memory(0xb304000c, 2) == bytes(2), 'Expected QEMU frozen FRAME'
        changes = [(local('usb') + 53, bytes([1])),
                   (local('configured_seen'), struct.pack('<I', 1)),
                   (local('protocol') + 528, bytes(range(256)) * 2),
                   (local('protocol') + 1060, struct.pack('<II', 512, 0)),
                   (local('zlp_pending'), bytes(4))]
        for address, data in changes:
            assert remote.command(f'M{address:x},{len(data):x}:' + data.hex()) == 'OK'
    finally:
        if original:
            assert remote.command('G' + original) == 'OK'
        remote.close()

    # No breakpoints, debug calls or input traffic in this wall-clock period.
    natural_start = time.monotonic()
    time.sleep(3)
    after = observation()
    natural_seconds = time.monotonic() - natural_start
    nav.capture(a.server, work / 'fairness-time-after-window.png')
    assert after['state'] == 1 and words(tcb + 4)[0] == base
    assert after['active_windows'] > before['active_windows']
    assert (after['iteration_limit_exits'] + after['tick_limit_exits'] >
            before['iteration_limit_exits'] + before['tick_limit_exits'])
    assert 1 < after['window_polls_max'] <= 4096
    assert after['frame_limit_exits'] == before['frame_limit_exits']
    assert after['gui_switches'] > before['gui_switches']
    assert after['bda_prefix'] != before['bda_prefix'], 'Queued virtual tap did not open Time'
    time.sleep(2)
    later = observation()
    nav.capture(a.server, work / 'fairness-time-natural-progress.png')
    assert later['gui_switches'] > after['gui_switches'] and later['polls'] > after['polls']
    # Undo the synthetic host configuration after the natural observation,
    # so the retained resident can be reused in another no-host test.
    remote = Remote(a.gdb_port)
    try:
        remote.command('?')
        remote.run_to(local('transport_service'))
        for address, data in [(local('usb') + 53, bytes(1)),
                              (local('configured_seen'), bytes(4))]:
            assert remote.command(f'M{address:x},{len(data):x}:' + data.hex()) == 'OK'
    finally:
        remote.close()
    nav.press_key(a.server, 24)
    time.sleep(.6)
    nav.tap(a.server, 42, 135)
    time.sleep(1)
    assert words(base + 24)[0] == 3
    assert words(0x8057b6d4, 2) == [0x80165988, 0]
    nav.press_key(a.server, 25)
    time.sleep(.5)
    nav.capture(a.server, work / 'fairness-final-menu.png')
    report = {'ok': True, 'bda_sha256': digest, 'firmware_sha256': image['kernel_sha256'],
              'method': __doc__, 'physical_hardware_tested': False,
              'usb_transport_tested': False, 'continuous_usb_load_tested': False,
              'synthetic_pending_tx_bytes': 512, 'gdb_absent_during_natural_observation': True,
              'synthetic_configuration_restored_after_observation': True,
              'natural_observation_seconds': natural_seconds, 'before': before, 'after': after,
              'later_natural_progress': later, 'final_state': 'STOPPED',
              'resident_base': hex(base), 'gui_task_control_block': hex(gui_tcb),
              'gui_task_priority': 10}
    (work / 'foreground-fairness-verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
