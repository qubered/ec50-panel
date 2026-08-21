"""Pictures on a one-bit panel.

A cell is 64x32 and every pixel is either ink or nothing, so a photograph has to
survive being reduced to two levels. Three steps, in order:

  1. luma      - colour collapses to brightness
  2. resample  - a box filter down to the cell, averaging whole source blocks
                 rather than point-sampling, so detail turns into grey instead
                 of aliasing away
  3. dither    - the grey becomes texture

Step 3 is what makes it work. A plain threshold throws away every mid tone;
dithering pushes the quantisation error into the neighbours that have not been
decided yet, so a 50% grey comes out as a checkerboard rather than a flat block.

Ink convention: a set pixel is DARK on a lit backlight, the same as the font.
So ink lands where the source is dark. Companion buttons are drawn light-on-dark
and want `invert=True`, which is the default on that path - it keeps a bitmap
looking like the text the panel would otherwise be showing.
"""

from __future__ import annotations

import struct
import zlib

from . import protocol as P

DITHERS = ("atkinson", "floyd", "bayer", "none")
FITS = ("contain", "cover", "stretch")

# 8x8 ordered dither, the classic recursive Bayer matrix. Values 0-63.
_BAYER8 = (
     0, 32,  8, 40,  2, 34, 10, 42,
    48, 16, 56, 24, 50, 18, 58, 26,
    12, 44,  4, 36, 14, 46,  6, 38,
    60, 28, 52, 20, 62, 30, 54, 22,
     3, 35, 11, 43,  1, 33,  9, 41,
    51, 19, 59, 27, 49, 17, 57, 25,
    15, 47,  7, 39, 13, 45,  5, 37,
    63, 31, 55, 23, 61, 29, 53, 21,
)

# Error diffusion kernels as (dx, dy, weight).
_FLOYD = ((1, 0, 7 / 16), (-1, 1, 3 / 16), (0, 1, 5 / 16), (1, 1, 1 / 16))
# Atkinson passes on only 6/8 of the error. Losing the rest is the point: it
# clips shadows and highlights to solid black and white, which reads far better
# on a small monochrome cell than a technically-faithful mush.
_ATKINSON = ((1, 0, .125), (2, 0, .125), (-1, 1, .125),
             (0, 1, .125), (1, 1, .125), (0, 2, .125))


# -- colour ----------------------------------------------------------------

def luma_from_rgb(raw: bytes) -> bytearray:
    """Rec. 601 luma from packed RGB triples."""
    out = bytearray(len(raw) // 3)
    for i in range(len(out)):
        j = i * 3
        out[i] = (raw[j] * 77 + raw[j + 1] * 150 + raw[j + 2] * 29) >> 8
    return out


def autolevel(luma: bytearray, clip: float = 0.01) -> bytearray:
    """Stretch the histogram to the full range, ignoring `clip` at each end.

    Companion buttons are often a flat colour a long way from black or white;
    without this they dither to a uniform texture with no contrast at all.
    """
    hist = [0] * 256
    for v in luma:
        hist[v] += 1
    cut = int(len(luma) * clip)
    lo, n = 0, 0
    for i in range(256):
        n += hist[i]
        if n > cut:
            lo = i
            break
    hi, n = 255, 0
    for i in range(255, -1, -1):
        n += hist[i]
        if n > cut:
            hi = i
            break
    if hi - lo < 8:
        return luma
    span = hi - lo
    lut = bytes(min(255, max(0, (v - lo) * 255 // span)) for v in range(256))
    return bytearray(lut[v] for v in luma)


# -- geometry --------------------------------------------------------------

def resample(src, sw: int, sh: int, dw: int, dh: int) -> bytearray:
    """Box filter to an arbitrary size. Averages, so downscaling keeps detail."""
    if (sw, sh) == (dw, dh):
        return bytearray(src)
    out = bytearray(dw * dh)
    for y in range(dh):
        y0 = y * sh // dh
        y1 = max(y0 + 1, (y + 1) * sh // dh)
        row = y * dw
        for x in range(dw):
            x0 = x * sw // dw
            x1 = max(x0 + 1, (x + 1) * sw // dw)
            total = count = 0
            for yy in range(y0, y1):
                base = yy * sw
                for xx in range(x0, x1):
                    total += src[base + xx]
                    count += 1
            out[row + x] = total // count
    return out


def place(src, sw: int, sh: int, dw: int, dh: int,
          fit: str = "contain", pad: int = 255) -> bytearray:
    """Scale into a dw x dh frame. `contain` letterboxes, `cover` crops."""
    if fit == "stretch" or (sw, sh) == (dw, dh):
        return resample(src, sw, sh, dw, dh)
    scale = min(dw / sw, dh / sh) if fit == "contain" else max(dw / sw, dh / sh)
    tw, th = max(1, round(sw * scale)), max(1, round(sh * scale))
    tmp = resample(src, sw, sh, tw, th)
    out = bytearray([pad]) * (dw * dh)
    ox, oy = (dw - tw) // 2, (dh - th) // 2
    for y in range(dh):
        sy = y - oy
        if not (0 <= sy < th):
            continue
        srow, drow = sy * tw, y * dw
        for x in range(dw):
            sx = x - ox
            if 0 <= sx < tw:
                out[drow + x] = tmp[srow + sx]
    return out


# -- dithering -------------------------------------------------------------

def to_bits(luma, w: int, h: int, dither: str = "atkinson",
            threshold: int = 128, invert: bool = False) -> bytearray:
    """One byte per pixel, 1 where ink goes."""
    if dither not in DITHERS:
        raise ValueError(f"dither must be one of {DITHERS}")
    if invert:
        luma = bytearray(255 - v for v in luma)
    bits = bytearray(w * h)

    if dither == "none":
        for i, v in enumerate(luma):
            bits[i] = 1 if v < threshold else 0
        return bits

    if dither == "bayer":
        # Ordered: one table lookup per pixel and no feedback, so it is the
        # only mode cheap enough to run on every key of a page change.
        for y in range(h):
            row, brow = y * w, (y & 7) * 8
            for x in range(w):
                if luma[row + x] < _BAYER8[brow + (x & 7)] * 4 + 2:
                    bits[row + x] = 1
        return bits

    kernel = _FLOYD if dither == "floyd" else _ATKINSON
    buf = [float(v) for v in luma]
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            old = buf[i]
            ink = old < 128
            bits[i] = 1 if ink else 0
            err = old - (0.0 if ink else 255.0)
            for dx, dy, weight in kernel:
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and ny < h:
                    buf[ny * w + nx] += err * weight
    return bits


def pack(bits, w: int, h: int, ox: int = 0, oy: int = 0) -> bytes:
    """Cut a 64x32 cell bitmap out of a bit plane, at offset (ox, oy)."""
    out = bytearray(P.CELL_SIZE)
    for y in range(min(P.CELL_H, h - oy)):
        srow, drow = (y + oy) * w, y * P.CELL_STRIDE
        for x in range(min(P.CELL_W, w - ox)):
            if bits[srow + ox + x]:
                out[drow + (x >> 3)] |= 1 << (x & 7)   # bit 0 is leftmost
    return bytes(out)


def to_cell(luma, sw: int, sh: int, dither: str = "atkinson",
            fit: str = "contain", invert: bool = False,
            threshold: int = 128, levels: bool = False) -> bytes:
    """The whole pipeline for one cell, ready for `EC50.set_bitmap`."""
    scaled = place(luma, sw, sh, P.CELL_W, P.CELL_H, fit,
                   pad=0 if invert else 255)
    if levels:
        scaled = autolevel(scaled)
    return pack(to_bits(scaled, P.CELL_W, P.CELL_H, dither, threshold, invert),
                P.CELL_W, P.CELL_H)


def guess_dims(nbytes: int, prefer=(P.CELL_W, P.CELL_H)):
    """Work out what shape a satellite BITMAP payload is.

    Companion is asked for a cell-shaped bitmap but does not promise one, so
    take the requested size if the length agrees, a square if that fits, and
    otherwise admit defeat rather than rendering a sheared mess.
    """
    px = nbytes // 3
    if px == prefer[0] * prefer[1]:
        return prefer
    side = int(px ** 0.5)
    if side and side * side == px:
        return side, side
    return None


# -- loading ---------------------------------------------------------------

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def load_png(data: bytes):
    """Minimal PNG reader: any colour type, 1/2/4/8/16 bits, non-interlaced.

    Written out longhand so the package keeps needing nothing but the standard
    library - Pillow is a heavy dependency for turning one logo into 2048 dots.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, plte = 8, bytearray(), b""
    w = h = depth = ctype = interlace = 0
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        pos += 8
        chunk = data[pos:pos + length]
        pos += length + 4
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"PLTE":
            plte = chunk
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if interlace:
        raise ValueError("interlaced PNGs are not supported; re-save without Adam7")
    if ctype not in _CHANNELS:
        raise ValueError(f"unsupported PNG colour type {ctype}")

    channels = _CHANNELS[ctype]
    bpp = max(1, channels * depth // 8)
    stride = (w * channels * depth + 7) // 8
    raw = zlib.decompress(bytes(idat))

    # Undo the per-scanline filters.
    out = bytearray(stride * h)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            raise ValueError(f"bad PNG filter {ftype}")
        out[y * stride:(y + 1) * stride] = line
        prev = line

    # Expand to one byte per sample.
    if depth == 16:
        samples = out[0::2]
        stride //= 2
    elif depth < 8:
        samples = bytearray(w * channels * h)
        step, mask = depth, (1 << depth) - 1
        scale = 255 // mask if ctype == 0 else 1
        for y in range(h):
            base, dst = y * stride, y * w * channels
            for i in range(w * channels):
                bit = i * step
                byte = out[base + (bit >> 3)]
                val = (byte >> (8 - step - (bit & 7))) & mask
                samples[dst + i] = val * scale
        stride = w * channels
    else:
        samples = out

    luma = bytearray(w * h)
    for y in range(h):
        base, dst = y * stride, y * w
        for x in range(w):
            i = base + x * channels
            if ctype == 3:
                p = samples[i] * 3
                r, g, b = plte[p], plte[p + 1], plte[p + 2]
            elif channels >= 3:
                r, g, b = samples[i], samples[i + 1], samples[i + 2]
            else:
                r = g = b = samples[i]
            luma[dst + x] = (r * 77 + g * 150 + b * 29) >> 8
    return w, h, luma


def load_netpbm(data: bytes):
    """P5 (grey) and P6 (RGB) binary Netpbm, which pdftoppm and friends emit."""
    if data[:2] not in (b"P5", b"P6"):
        raise ValueError("not a binary Netpbm")
    fields, pos = [], 2
    while len(fields) < 3:
        while pos < len(data) and data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            while data[pos:pos + 1] not in (b"\n", b""):
                pos += 1
            continue
        end = pos
        while end < len(data) and not data[end:end + 1].isspace():
            end += 1
        fields.append(int(data[pos:end]))
        pos = end
    pos += 1
    w, h, _ = fields
    body = data[pos:]
    return (w, h, luma_from_rgb(body) if data[:2] == b"P6" else bytearray(body[:w * h]))


def load(path: str):
    """Read a picture. Returns (width, height, luma)."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return load_png(data)
    if data[:2] in (b"P5", b"P6"):
        return load_netpbm(data)
    raise ValueError(f"{path}: need a PNG or a binary PGM/PPM")


# -- preview ---------------------------------------------------------------

_HALF = {(0, 0): " ", (1, 0): "▀", (0, 1): "▄", (1, 1): "█"}


def preview(bits, w: int, h: int) -> str:
    """Two pixel rows per character, so a cell prints at its real aspect."""
    lines = []
    for y in range(0, h, 2):
        top, bot = y * w, (y + 1) * w
        lines.append("".join(
            _HALF[(bits[top + x], bits[bot + x] if y + 1 < h else 0)]
            for x in range(w)))
    return "\n".join(lines)
