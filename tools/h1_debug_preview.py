"""Fast 96x54 LCD preview through an existing H1 debug Client session.

preview(client, directory) does not open or close serial ports. It replaces the
current EXEC module, so finish/free any module-owned allocation first. The
sampled live framebuffer is read-only and checked against SCREEN_CAPTURE.
Three tiles can differ in time; this is not the immutable full snapshot.
"""
from pathlib import Path
import json
import struct

import h1_debug as h

SOURCE = h.ROOT / "h1-debug/examples/screen_preview.c"
FIELDS = ("magic", "version", "source_width", "source_height", "source_stride",
          "source_format", "source", "descriptor", "width", "height", "first_row",
          "rows", "pixel_bytes", "tick_begin", "tick_end", "sampling_rule")


def parse_tile(data, meta, first, rows):
    if len(data) < 64:
        raise h.ProtocolError("Preview header missing")
    tile = dict(zip(FIELDS, struct.unpack_from("<16I", data)))
    actual = tuple(tile[k] for k in ("magic", "version", "source_width", "source_height",
                                    "source_stride", "source_format", "source", "width",
                                    "height", "first_row", "rows", "pixel_bytes", "sampling_rule"))
    expected = (0x31565048, 1, meta["width"], meta["height"], meta["stride"],
                meta["format"], meta["source"], 96, 54, first, rows, 96 * rows * 2, 1)
    if actual != expected or len(data) != 64 + tile["pixel_bytes"]:
        raise h.ProtocolError("Preview tile source/layout/size mismatch")
    if tile["descriptor"] & 15 or not 0 <= tile["descriptor"] <= 0x04000000 - 16:
        raise h.ProtocolError("Preview descriptor outside aligned physical RAM")
    return tile, data[64:]


def preview(client, directory):
    """Sample 3 validated tiles and save PNG/raw/receipts on the same Client."""
    from PIL import Image
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    info = client.exec_info()
    cache = getattr(client, "_screen_preview_module", None)
    source_sha = h.sha(SOURCE.read_bytes())
    if (not cache or cache["source_sha256"] != source_sha or
            (info["base"], info["upload_crc"], info["image_size"], info["memory_size"], info["state"]) !=
            (cache["base"], cache["crc32"], cache["image_size"], cache["memory_size"], 5)):
        metadata, code = h.build_module(SOURCE, info["base"], directory / "module", info["image_capacity"])
        client.upload(metadata, code)
        client._screen_preview_module = metadata
    else:
        metadata = cache
    meta = h.words(client.rpc(6), h.SCREEN_FIELDS, "SCREEN_CAPTURE")
    token = meta["token"]
    tiles, pixels = [], bytearray()
    try:
        h.validate_screen(meta)
        if ((meta["width"], meta["height"]) != (480, 272) or
                meta["source"] & 0xE0000000 != 0xA0000000 or meta["source"] & 3 or
                (meta["source"] & 0x1FFFFFFF) + meta["total"] > 0x04000000):
            raise h.ProtocolError("Unsupported current LCD source metadata")
        for first in (0, 18, 36):
            args = struct.pack("<8I", 1, meta["source"], meta["width"], meta["height"],
                               meta["stride"], meta["format"], first, 18)
            result, data = client.run_module(0, args, budget_ticks=80)
            (directory / f"tile-{first:02d}.bin").write_bytes(data)
            if result["return_value"] != 0 or result["flags"]:
                raise RuntimeError("LCD preview module refused the frame: " + json.dumps(result))
            tile, rgb565 = parse_tile(data, meta, first, 18)
            if tiles and tile["descriptor"] != tiles[0]["header"]["descriptor"]:
                raise h.ProtocolError("LCD descriptor changed between preview tiles")
            tiles.append({"header": tile, "execution": result})
            pixels.extend(rgb565)
    finally:
        if token and not client._broken:
            client.empty(8, struct.pack("<I", token))
    output_meta = {"token": 1, "width": 96, "height": 54, "stride": 192,
                   "format": 1, "total": len(pixels), "source": meta["source"], "tick": meta["tick"]}
    rgb = h.decode_screen(output_meta, pixels)
    (directory / "preview.rgb565").write_bytes(pixels)
    Image.frombytes("RGB", (96, 54), rgb).save(directory / "preview.png")
    # A nearest-neighbor enlargement makes navigation inspection convenient.
    Image.frombytes("RGB", (96, 54), rgb).resize((480, 270), Image.Resampling.NEAREST).save(directory / "preview-5x.png")
    receipt = {"screen_capture": meta, "module": metadata, "tiles": tiles,
               "preview_width": 96, "preview_height": 54, "format": "RGB565_LE",
               "sampling": "floor(x*480/96), floor(y*272/54)", "atomic_snapshot": False,
               "pixel_sha256": h.sha(pixels), "image": str((directory / "preview.png").resolve()),
               "enlarged_image": str((directory / "preview-5x.png").resolve())}
    (directory / "preview.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt
