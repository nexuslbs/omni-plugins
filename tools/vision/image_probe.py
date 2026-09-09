#!/usr/bin/env python3
"""image_probe: pure-stdlib image probing for the vision (read_image) plugin.

Detects PNG / JPEG / GIF / WebP / BMP from their magic bytes, extracts pixel
dimensions, maps to a mime type and computes the sha256 of the content. No
third-party imaging library is required (Pillow is NOT a dependency): the
probe only needs the container headers, which is exactly the information the
read_image tool reports to a vision-capable model.

Raised error: ImageProbeError with a human-readable message. The module never
reads more than the bytes it is given and never decodes pixel data.
"""

import hashlib
import struct


class ImageProbeError(Exception):
    """Raised when content is not a supported/recognizable image."""


MIME_BY_FORMAT = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}


def probe(data):
    """Return a dict with format/mime/width/height for supported images.

    data: bytes (entire image content, already size-capped by the caller).
    Raises ImageProbeError for unknown or unsupported content.
    """
    if not data:
        raise ImageProbeError("empty content is not an image")

    fmt = detect_format(data)
    if fmt is None:
        raise ImageProbeError(
            "unrecognized image format (supported: PNG, JPEG, GIF, WebP, "
            "BMP); magic bytes do not match any known container")

    if fmt == "png":
        width, height = _png_dims(data)
    elif fmt == "jpeg":
        width, height = _jpeg_dims(data)
    elif fmt == "gif":
        width, height = _gif_dims(data)
    elif fmt == "webp":
        width, height = _webp_dims(data)
    else:  # bmp
        width, height = _bmp_dims(data)

    return {
        "format": fmt,
        "mime": MIME_BY_FORMAT[fmt],
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def detect_format(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 2 and data[:2] == b"BM":
        return "bmp"
    return None


def _png_dims(data):
    # IHDR starts at byte 8: length(4) 'IHDR'(4) width(4) height(4)
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise ImageProbeError("PNG is missing its IHDR chunk")
    width, height = struct.unpack(">II", data[16:24])
    if width == 0 or height == 0:
        raise ImageProbeError("PNG has a zero dimension")
    return width, height


def _jpeg_dims(data):
    # Walk the segment table looking for a SOF marker (C0..CF except C4/C8/CC).
    off = 2
    n = len(data)
    while off + 9 < n:
        if data[off] != 0xFF:
            off += 1
            continue
        marker = data[off + 1]
        if marker in (0xD8, 0x01):
            off += 2
            continue
        if marker in (0xD9, 0xDA):
            break  # EOI or start of scan: no SOF seen
        if off + 3 >= n:
            break
        seg_len = struct.unpack(">H", data[off + 2:off + 4])[0]
        if seg_len < 2:
            break
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if off + 8 >= n:
                raise ImageProbeError("JPEG SOF segment is truncated")
            height, width = struct.unpack(">HH", data[off + 5:off + 9])
            if width == 0 or height == 0:
                raise ImageProbeError("JPEG has a zero dimension")
            return width, height
        off += 2 + seg_len
    raise ImageProbeError("JPEG dimensions could not be read (no SOF marker "
                          "before end of image)")


def _gif_dims(data):
    if len(data) < 10:
        raise ImageProbeError("GIF header is truncated")
    width, height = struct.unpack("<HH", data[6:10])
    if width == 0 or height == 0:
        raise ImageProbeError("GIF has a zero dimension")
    return width, height


def _webp_dims(data):
    fourcc = data[12:16]
    if fourcc == b"VP8X":
        # 10-byte features then 3-byte little-endian (width-1) and (height-1).
        if len(data) < 30:
            raise ImageProbeError("WebP VP8X header is truncated")
        w24 = data[24:27]
        h24 = data[27:30]
        width = (w24[0] | (w24[1] << 8) | (w24[2] << 16)) + 1
        height = (h24[0] | (h24[1] << 8) | (h24[2] << 16)) + 1
    elif fourcc == b"VP8 ":
        # Lossy: frame tag at 20, 3-byte LE (width & 0x3FFF), (height & 0x3FFF).
        if len(data) < 26:
            raise ImageProbeError("WebP VP8 header is truncated")
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
    elif fourcc == b"VP8L":
        # Lossless: 1-byte signature then 4 bytes: packed 14-bit w/h.
        if len(data) < 25:
            raise ImageProbeError("WebP VP8L header is truncated")
        packed = struct.unpack("<I", data[21:25])[0]
        width = (packed & 0x3FFF) + 1
        height = ((packed >> 14) & 0x3FFF) + 1
    else:
        raise ImageProbeError("unsupported WebP variant (fourcc %r)"
                              % fourcc.decode("latin1"))
    if width == 0 or height == 0:
        raise ImageProbeError("WebP has a zero dimension")
    return width, height


def _bmp_dims(data):
    if len(data) < 26:
        raise ImageProbeError("BMP header is truncated")
    width = struct.unpack("<i", data[18:22])[0]
    height = struct.unpack("<i", data[22:26])[0]
    width = abs(width)
    height = abs(height)
    if width == 0 or height == 0:
        raise ImageProbeError("BMP has a zero dimension")
    return width, height
