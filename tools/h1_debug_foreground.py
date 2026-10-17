"""Full-resolution transfer/foreground check using an already open H1 Client.

This module never opens or closes a serial port. The caller must first verify
the native utilities menu: Time at (105, 52), H1Debug at (42, 135). Call
run_foreground_check(client, output_directory) on that one existing session.
These measurements deliberately interleave input/readback and are not pure
transfer benchmarks. The original menu snapshot stays pinned across the BDA
switch and is downloaded again with lossless RLE before its token is released.
"""
from pathlib import Path
import hashlib
import json
import struct
import time

import h1_debug as h1

GUI_PRIORITY = 10  # Native input dequeue / window dispatcher task in V1.41.


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _save_png(path, meta, data):
    from PIL import Image
    assert (meta['width'], meta['height']) == (480, 272)
    Image.frombytes('RGB', (480, 272), h1.decode_screen(meta, data)).save(path)
    return {'path': str(path.resolve()), 'snapshot_sha256': _sha(data),
            'png_sha256': _sha(path.read_bytes()), 'width': 480, 'height': 272}


def run_foreground_check(client, output_directory, *, time_position=(105, 52)):
    """Open Time at 25% of one raw transfer, verify it, then Escape to menu.

    Preconditions: H1 V1.41, the verified utilities page, resident running,
    and no foreground operation whose unsaved state should be preserved.
    Returns a report; a failure also saves the observations already collected.
    The client remains open. Input callbacks run between complete RPCs.
    """
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not client.capabilities()['features'] & 0x100:
        raise RuntimeError('This check requires the lossless RLE resident build')
    report = {'ok': False, 'test': 'Raw screenshot transfer with foreground interaction',
              'resolution': [480, 272], 'single_existing_client': True,
              'pure_throughput_benchmark': False, 'observations': []}
    report_path = output / 'foreground-verification.json'
    started = time.monotonic()

    def save():
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')

    def observe(stage, done=0, total=0):
        status = client.status()
        if status['state'] != 1 or status['usb_fault'] or status['last_error']:
            raise AssertionError(('Resident not healthy', status))
        tcb = struct.unpack('<I', client.read_memory(0x80a6d6f0 + GUI_PRIORITY * 4, 4))[0]
        if not 0x80000000 <= tcb < 0x83ffffc0:
            raise AssertionError(('Invalid native GUI task', hex(tcb)))
        tcb_tail = client.read_memory(tcb + 0x30, 16)
        if tcb_tail[5] != GUI_PRIORITY:
            raise AssertionError(('Native task priority mismatch', tcb_tail.hex()))
        prefix = client.read_memory(0x83c00020, 256)
        item = {'stage': stage, 'host_elapsed_seconds': time.monotonic() - started,
                'downloaded': done, 'total': total, 'status': status,
                'gui_task_control_block': hex(tcb),
                'gui_task_priority': GUI_PRIORITY,
                'gui_switch_count': struct.unpack_from('<I', tcb_tail, 12)[0],
                'bda_prefix_sha256': _sha(prefix)}
        report['observations'].append(item)
        save()
        return item

    token = 0
    try:
        baseline = observe('before_capture')
        meta = h1.words(client.rpc(6), h1.SCREEN_FIELDS, 'SCREEN_CAPTURE')
        token = meta['token']
        assert (meta['width'], meta['height']) == (480, 272), meta
        report['pinned_snapshot'] = meta
        action_sent = False
        checkpoints = set()

        def progress(done, total):
            nonlocal action_sent
            if not action_sent and done * 4 >= total:
                observe('before_time_tap', done, total)
                report['time_tap'] = client.tap(*time_position)
                action_sent = True
                observe('after_time_tap', done, total)
            quarter = min(4, done * 4 // total)
            if quarter and quarter not in checkpoints:
                checkpoints.add(quarter)
                observe('raw_quarter_' + str(quarter), done, total)

        raw, transfer = client.read_screen(meta, codec='raw', progress=progress)
        assert action_sent and len(raw) == meta['total']
        report['raw_transfer_with_interleaving'] = transfer
        report['pinned_menu_image'] = _save_png(output / 'pinned-menu.png', meta, raw)
        after_raw = observe('after_raw_download', len(raw), len(raw))
        assert after_raw['bda_prefix_sha256'] != baseline['bda_prefix_sha256'], \
            'Time BDA did not replace the launcher loading window'
        assert after_raw['gui_task_control_block'] == baseline['gui_task_control_block']
        switches = (after_raw['gui_switch_count'] - baseline['gui_switch_count']) & 0xffffffff
        assert switches > 0, 'Native GUI task did not receive execution'
        report['gui_switches_during_transfer'] = switches
        assert after_raw['status']['resident_base'] == baseline['status']['resident_base']
        # A second read of the exact token proves changing the foreground
        # cannot silently replace the image being downloaded by the first.
        same, rle_transfer = client.read_screen(meta, codec='rle')
        assert same == raw, 'Pinned snapshot changed across the foreground BDA switch'
        report['same_token_raw_rle_equal'] = True
        report['pinned_snapshot_sha256'] = _sha(raw)
        report['same_token_rle_transfer'] = rle_transfer
        client.empty(8, struct.pack('<I', token))
        token = 0

        time_meta, time_data = client.capture_screen(codec='rle')
        report['time_image'] = _save_png(output / 'time-after-transfer.png', time_meta, time_data)
        assert _sha(time_data) != _sha(raw), 'Foreground pixels did not change'
        report['escape'] = client.press(['ESCAPE'], ticks=8)
        time.sleep(.5)
        menu_meta, menu_data = client.capture_screen(codec='rle')
        report['returned_menu_image'] = _save_png(output / 'returned-menu.png', menu_meta, menu_data)
        assert _sha(menu_data) != _sha(time_data), 'Escape did not change foreground pixels'
        final = observe('after_escape')
        report['input_released'] = client.touch_status()['pressed'] == 0
        assert report['input_released']
        assert final['status']['resident_base'] == baseline['status']['resident_base']
        report['ok'] = True
        report['visual_review_required'] = ['time-after-transfer.png', 'returned-menu.png']
        return report
    except BaseException as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        if token and not client._broken:
            try:
                client.empty(8, struct.pack('<I', token))
            except Exception as error:
                report['snapshot_release_error'] = str(error)
        save()
