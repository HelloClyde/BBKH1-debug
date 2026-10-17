"""Validate H1 Debug's key/touch/LCD bindings against the original V1.41 image.

This is static firmware evidence, not a claim of hardware execution.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import struct

ROOT=Path(__file__).resolve().parents[1]
FIRMWARE_SHA256='d05786e442f9aad62a8d0a0cb4f6d786bdc7c2fa353a7a2b152c9ed9f01b40ef'


def verify(path):
    blob=Path(path).read_bytes()
    digest=hashlib.sha256(blob).hexdigest()
    if digest!=FIRMWARE_SHA256:
        raise ValueError('Wrong firmware: recover all addresses before enabling hooks')
    def word(address):
        return struct.unpack_from('<I',blob,address-0x80004000)[0]
    expected={
        0x80008234:0x3c02802a, 0x80008238:0x8c424aa4,
        0x80068af0:0x0c00208d,  # GUI held query calls 80008234.
        0x8000cddc:0x27bdffe0, 0x8000cde0:0xafb00010,
        0x8000ce4c:0x8e050008,  # Queue object -> ring.
        0x8000ce50:0x8ca20018, 0x8000ce54:0x8ca30014,
        0x8000ce74:0xac520000,  # Queues pointer, not record bytes.
        0x800085d4:0xae020004, 0x800085dc:0xac34bc08,
        0x80008770:0xae020004, 0x80008778:0xac34bc08,
        0x8000af44:0x24050032,  # Fifty slots in native keyboard queue.
        0x8000af54:0xac22ae10,
        0x80040ebc:0x27bdffa8, 0x80040ec0:0xafbf0050,
        0x80040ec4:0xafb7004c, 0x80040ec8:0xafb60048,
        0x80040ecc:0x00a0b821, 0x80040ed0:0x0080b021,
        0x80040ed4:0xafb50044, 0x80040ed8:0xafb40040,
        0x8000c8b0:0x27bdffe0, 0x8000c8b4:0xafb10014,
        0x8000cc5c:0x3c028058, 0x8000cc60:0x9042adf9,
        0x8000d208:0x27bdffe8, 0x8000d20c:0xafb00010,
        0x80041e24:0x2403000b,  # SADC DOWN=11, subsequent MOVE=12.
        0x80041e48:0xae260004, 0x80041e50:0xac20aea8,
        0x80041e7c:0x2402000c,
        0x80041f14:0x24030008,  # SADC UP=8, [value=0,event=8].
        0x80041f58:0xae220004, 0x80041f60:0xac20aeb0,
        0x8011497c:0x0c0103af,  # Native GUI reads calibrated coordinates,
        0x8011499c:0x00042400, 0x801149a0:0x00641825, # packs y<<16|x.
        0x80005b48:0x3c02803f, 0x80005b4c:0x8c422408,
        0x80005824:0x3c02803f, 0x80005828:0x03e00008,
        0x8000582c:0x8c422438,
        0x803f2404:480, 0x803f2408:272, 0x803f240c:18,
    }
    for address,instruction in expected.items():
        if word(address)!=instruction:
            raise ValueError(f'Firmware evidence differs at {address:#010x}')
    # The coordinate hook replaces two stores after the six-word signature;
    # no original branch/jump may enter that displaced pair from elsewhere.
    for address in range(0x80004000,0x802a0000,4):
        instruction=word(address);op=instruction>>26;target=None
        if op in (2,3):target=(address+4)&0xf0000000|(instruction&0x3ffffff)<<2
        elif op in (1,4,5,6,7,20,21,22,23):
            immediate=instruction&65535
            if immediate&32768:immediate-=65536
            target=address+4+immediate*4
        if target in (0x80040ed4,0x80040ed8):
            raise ValueError(f'Unexpected entry into displaced touch stores from {address:#010x}')
    source=(ROOT/'h1-debug/io.c').read_text(encoding='utf-8')
    match=re.search(r'native_codes\[42\]\s*=\s*\{([^}]+)\}',source)
    if not match:
        raise ValueError('Missing native key conversion table')
    codes=bytes(int(v.strip()) for v in match.group(1).split(','))
    start=0x802a4a19-0x80004000
    if len(codes)!=42 or codes!=blob[start:start+42]:
        raise ValueError('Event matrix to held-query scancode mapping differs')
    return dict(ok=True,kind='Static firmware binding evidence',firmware_sha256=digest,
                checked_words=len(expected),checked_key_mappings=42,
                held_query='0x80008234',queue_post='0x8000CDDC',
                queue_handle_slot='0x8057AE10',queue_capacity=50,
                touch_coordinates='0x80040EBC',touch_events={'down':11,'move':12,'up':8},
                touch_patch='0x80040ED4',touch_getter_signature_preserved=True,
                touch_hooks=['0x8000C8B0','0x8000CC5C','0x8000D208'],
                queue_stores_pointers=True,screen_dimensions=[480,272],
                screen_bpp=18,hardware_tested=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--firmware',type=Path,default=ROOT/'build/minigpt-native/project.bin')
    args=parser.parse_args()
    print(json.dumps(verify(args.firmware),indent=2))


if __name__=='__main__':
    main()
