"""Compare raw and lossless RLE reads of ONE immutable H1 snapshot.

Import compare_snapshot(client, directory) to reuse an existing serial session.
Only main() opens a port. Raw wire logging is supplied by the acceptance helper.
"""
from pathlib import Path
import argparse
import json
import struct
import time

import h1_debug as h


def compare_snapshot(client, directory):
    """Read both codecs against one token, verify all bytes, and save evidence."""
    from PIL import Image
    client._screen_codec("rle")  # Check before allocating a snapshot token.
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    meta = h.words(client.rpc(6), h.SCREEN_FIELDS, "SCREEN_CAPTURE")
    token = meta["token"]
    failure = None
    try:
        raw, raw_stats = client.read_screen(meta, codec="raw")
        (directory / "screen.raw").write_bytes(raw)
        decoded, rle_stats = client.read_screen(meta, codec="rle")
        (directory / "screen.rle-decoded.raw").write_bytes(decoded)
        equal = raw == decoded
        result = {"snapshot": meta, "same_snapshot": True, "byte_identical": equal,
                  "raw_sha256": h.sha(raw), "rle_decoded_sha256": h.sha(decoded),
                  "raw": raw_stats, "rle": rle_stats,
                  "transfer_speedup": raw_stats["elapsed_seconds"] / rle_stats["elapsed_seconds"]
                  if rle_stats["elapsed_seconds"] else None,
                  "elapsed_seconds": time.monotonic() - start}
        (directory / "comparison.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if not equal:
            raise h.ProtocolError("Raw and RLE screenshot bytes differ; both buffers were saved")
        image = Image.frombytes("RGB", (meta["width"], meta["height"]), h.decode_screen(meta, raw))
        image.save(directory / "screen.png", format="PNG")
        return result
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if token and not client._broken:
            try:
                client.empty(8, struct.pack("<I", token))
            except Exception:
                if failure is None:
                    raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--port")
    parser.add_argument("--timeout", type=float, default=4)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    from test_h1_debug_hardware import connect
    wire = args.directory / ("wire-" + str(time.time_ns()) + ".jsonl")
    with connect(args.port, args.timeout, wire) as client:
        print(json.dumps(compare_snapshot(client, args.directory), indent=2))


if __name__ == "__main__":
    main()
