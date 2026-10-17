"""Physical touch/ESC regression and explicit remote reopening of H1Debug.

Run input first and inspect its screenshots, then close. The close stage records
only the acknowledged launch and CDC disappearance; a human must confirm the
success notice after USB debugging has stopped.
"""
from pathlib import Path
import argparse, hashlib, json, struct, time
import h1_debug as host
import test_h1_debug_hardware as hw
from test_h1_debug_deploy_hardware import wait_menu


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage', choices=['input', 'close'])
    p.add_argument('--directory', type=Path, required=True)
    a = p.parse_args(); d = a.directory; d.mkdir(parents=True, exist_ok=True)
    report = {'stage': a.stage, 'completed': False}
    save = lambda: hw.save_json(d / (a.stage + '.json'), report)
    (d / a.stage).mkdir(parents=True, exist_ok=True)
    with hw.connect(None, 8, d / (a.stage + '-wire-' + str(time.time_ns()) + '.jsonl')) as c:
        report['initial'] = hw.probe(c, d / a.stage)
        assert c.desktop_status()['menu_idle']
        save()
        if a.stage == 'input':
            before = c.read_memory(0x83c00020, 1024)
            report['touch_time'] = c.tap(105, 52)
            time.sleep(.7)
            after = c.read_memory(0x83c00020, 1024)
            report['loaded_code'] = {'before': hashlib.sha256(before).hexdigest(),
                                     'after': hashlib.sha256(after).hexdigest()}
            assert before != after
            report['time_desktop_status'] = c.desktop_status()
            assert not report['time_desktop_status']['menu_idle']
            report['time_screen'] = c.screenshot(d / 'time.png')
            report['background'] = c.status()
            assert report['background']['resident_base'] == report['initial']['status']['resident_base']
            report['escape'] = c.press('ESCAPE', ticks=8)
            report['returned'] = wait_menu(c)
            time.sleep(.7)
            report['menu_screen'] = c.screenshot(d / 'menu-returned.png')
            report['final'] = c.status()
            assert report['final']['usb_resets'] == report['initial']['status']['usb_resets']
            assert not any(report['final'][k] for k in ('usb_fault', 'last_error', 'bad_frames'))
            report['completed'] = True; save()
        else:
            target = 'A:\\应用\\程序\\H1Debug.bda'
            report['target'] = target
            report['file'] = c.file_stat(target); assert report['file']['exists']
            report['request_sent'] = True; save()
            # This app stops the USB service. Do not poll desktop_run afterwards.
            report['job_id'] = struct.unpack('<I', c.rpc(51, target.encode('gbk')))[0]
            report['launch_acknowledged'] = True; save()
    if a.stage == 'close':
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and host.ports():
            time.sleep(.25)
        report['remaining_debug_ports'] = [p.device for p in host.ports()]
        report['cdc_disappeared'] = not report['remaining_debug_ports']
        report['user_notice_confirmation'] = 'pending'
        assert report['cdc_disappeared']
        report['completed'] = True; save()
    print(json.dumps({'ok': True, 'stage': a.stage, 'report': str(d / (a.stage + '.json'))}))


if __name__ == '__main__':
    main()
