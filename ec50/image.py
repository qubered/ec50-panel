"""Pictures on a one-bit panel.

A cell is 64x32 and every pixel is either ink or nothing, so a photograph has to
survive being reduced to two levels. Four steps, in order:

  1. luma      - colour collapses to brightness
  2. resample  - a box filter down to the cell, averaging whole source blocks
                 rather than point-sampling, so detail turns into grey instead
                 of aliasing away
  3. sharpen   - an unsharp mask against a box blur, putting back the contrast
                 the downscale averaged out of thin strokes
  4. threshold - grey becomes ink or nothing

Step 4 is the one that decides how it looks, and there is no single right
answer. Flat artwork - a logo, an icon, a screen of text - wants one hard cut,
placed by Otsu's method; error diffusion would turn its flat areas into noise.
A photograph wants a threshold that varies across the picture, so detail
survives at both ends of the range. `dither="auto"` reads the histogram and
picks: two clean tones get the hard cut, anything else gets the local one.

Polarity is the other half of it, and also not fixed. A set pixel is DARK on a
lit backlight. Companion draws its buttons light-on-dark, so most artwork wants
inverting - white text becomes dark text on a lit key, matching the font. But a
photograph does not, and inverting one turns it into a negative. `polarity=
"auto"` takes whichever way round leaves less ink, so the majority tone is the
one that stays lit.
"""

from __future__ import annotations

import struct
import zlib

from . import protocol as P

DITHERS = ("auto", "otsu", "adaptive", "atkinson", "floyd", "bayer", "none")
POLARITIES = ("auto", "dark", "light")
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


def otsu_stats(luma):
    """Otsu's cut, and how cleanly it separates: (threshold, 0.0-1.0).

    The threshold is the first level of the upper class, so it is used with
    `<` rather than `<=`. That matters at the ends: a button that is flat
    black behind flat white splits at level 0, and comparing `v < 0` inks
    nothing at all - the cell comes out blank.

    The second number is the between-class variance over the total. Near 1
    means two well-separated tones - a logo, an icon, a screen of text - and a
    threshold will render it exactly. Low means continuous tone, where a single
    cut has to throw away everything on the wrong side of it.
    """
    hist = [0] * 256
    for v in luma:
        hist[v] += 1
    total = len(luma)
    if not total:
        return 128, 0.0
    grand = sum(i * hist[i] for i in range(256))
    mean = grand / total
    variance = sum(hist[i] * (i - mean) ** 2 for i in range(256)) / total
    below = weight = 0
    best, cut = -1.0, 128
    for t in range(256):
        weight += hist[t]
        if weight == 0:
            continue
        rest = total - weight
        if rest == 0:
            break
        below += t * hist[t]
        spread = weight * rest * (below / weight - (grand - below) / rest) ** 2
        if spread > best:
            best, cut = spread, t
    if variance <= 0 or best <= 0:
        return cut + 1, 0.0
    return cut + 1, min(1.0, (best / total ** 2) / variance)


def tone_count(luma, bins: int = 16, floor: float = 0.02) -> int:
    """How many coarse grey levels the picture actually occupies.

    Separability alone cannot tell flat artwork from a photograph - a linear
    ramp scores about 0.75 against a logo's 0.98, which is far too close to
    split on. Counting tones does: a logo or a screen of text lives in two or
    three buckets, a photograph spreads across most of them.
    """
    hist = [0] * bins
    for v in luma:
        hist[v * bins // 256] += 1
    cut = len(luma) * floor
    return sum(1 for n in hist if n > cut)


# Two clean tones and a hard cut loses nothing. Anything else has mid tones
# worth keeping, and wants a threshold that varies across the picture.
BIMODAL = 0.85
FLAT_TONES = 5


def pick_dither(luma) -> str:
    """Choose between a hard cut and a local one by looking at the histogram."""
    _, separability = otsu_stats(luma)
    if separability >= BIMODAL and tone_count(luma) <= FLAT_TONES:
        return "otsu"
    return "adaptive"


def otsu(luma) -> int:
    """The threshold that best splits the histogram into two classes.

    Companion buttons are mostly flat colour with a logo or icon on top, and
    error diffusion turns those flat areas into noise. A cut in the right place
    keeps the shape instead - the trick is finding it, and Otsu does that by
    maximising the variance between the two sides.
    """
    return otsu_stats(luma)[0]


def _integral(luma, w: int, h: int):
    """Summed-area table, so a window mean costs four lookups."""
    stride = w + 1
    out = [0] * (stride * (h + 1))
    for y in range(h):
        run = 0
        base, above = (y + 1) * stride, y * stride
        for x in range(w):
            run += luma[y * w + x]
            out[base + x + 1] = out[above + x + 1] + run
    return out


def sharpen(luma, w: int, h: int, amount: float = 1.0, radius: int = 2):
    """Unsharp mask against a box blur, using the summed-area table.

    Box-filtering a 72x72 button down to a 64x32 cell averages a one-pixel
    stroke into mid grey, and a global threshold then drops it. Adding back
    the difference from the local mean puts the contrast back before the cut,
    and leaves flat areas alone because there a pixel equals its own mean.
    """
    if amount <= 0:
        return luma
    ii, stride = _integral(luma, w, h), w + 1
    out = bytearray(w * h)
    for y in range(h):
        y0, y1 = max(0, y - radius), min(h - 1, y + radius)
        top, bot = y0 * stride, (y1 + 1) * stride
        for x in range(w):
            x0, x1 = max(0, x - radius), min(w - 1, x + radius)
            count = (x1 - x0 + 1) * (y1 - y0 + 1)
            total = ii[bot + x1 + 1] - ii[top + x1 + 1] - ii[bot + x0] + ii[top + x0]
            v = luma[y * w + x]
            out[y * w + x] = min(255, max(0, int(v + amount * (v - total / count))))
    return out


def adaptive_bits(luma, w: int, h: int, radius: int = 0, bias: float = 0.15):
    """Bradley's local mean threshold: ink where a pixel is darker than its
    neighbourhood by `bias`.

    A global cut loses whichever end of the range it is not aimed at - a dark
    logo on a dark ground, or fine detail in a bright corner. Comparing against
    the local mean keeps both, and flat areas stay clean because a pixel equal
    to its own neighbourhood never clears the bias.
    """
    r = radius or max(2, min(w, h) // 8)
    ii, stride = _integral(luma, w, h), w + 1
    bits = bytearray(w * h)
    for y in range(h):
        y0, y1 = max(0, y - r), min(h - 1, y + r)
        top, bot = y0 * stride, (y1 + 1) * stride
        for x in range(w):
            x0, x1 = max(0, x - r), min(w - 1, x + r)
            count = (x1 - x0 + 1) * (y1 - y0 + 1)
            total = ii[bot + x1 + 1] - ii[top + x1 + 1] - ii[bot + x0] + ii[top + x0]
            if luma[y * w + x] * count < total * (1.0 - bias):
                bits[y * w + x] = 1
    return bits


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


def fit_box(sw: int, sh: int, dw: int, dh: int, fit: str):
    """Where the scaled picture lands: (ox, oy, tw, th) in destination pixels."""
    if fit == "stretch" or (sw, sh) == (dw, dh):
        return 0, 0, dw, dh
    scale = min(dw / sw, dh / sh) if fit == "contain" else max(dw / sw, dh / sh)
    tw, th = max(1, round(sw * scale)), max(1, round(sh * scale))
    return (dw - tw) // 2, (dh - th) // 2, tw, th


def place(src, sw: int, sh: int, dw: int, dh: int,
          fit: str = "contain", pad: int = 255) -> bytearray:
    """Scale into a dw x dh frame. `contain` letterboxes, `cover` crops."""
    if fit == "stretch" or (sw, sh) == (dw, dh):
        return resample(src, sw, sh, dw, dh)
    ox, oy, tw, th = fit_box(sw, sh, dw, dh, fit)
    tmp = resample(src, sw, sh, tw, th)
    out = bytearray([pad]) * (dw * dh)
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
    """One byte per pixel, 1 where ink goes.

    `invert=False` inks where the source is dark, which is what a photograph
    wants. `invert=True` inks where it is light, which is what light-on-dark
    artwork wants.
    """
    if dither not in DITHERS:
        raise ValueError(f"dither must be one of {DITHERS}")
    if dither == "auto":
        dither = pick_dither(luma)
    if invert:
        luma = bytearray(255 - v for v in luma)
    bits = bytearray(w * h)

    if dither in ("none", "otsu"):
        cut = otsu(luma) if dither == "otsu" else threshold
        for i, v in enumerate(luma):
            bits[i] = 1 if v < cut else 0
        return bits

    if dither == "adaptive":
        return adaptive_bits(luma, w, h)

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


def pack_at(bits, w: int, h: int, ox: int = 0, oy: int = 0) -> bytes:
    """Pack a bit plane into a 64x32 cell with its top left at (ox, oy).

    Negative offsets crop, positive ones letterbox, and whatever the picture
    does not reach stays blank.
    """
    out = bytearray(P.CELL_SIZE)
    for y in range(max(0, oy), min(P.CELL_H, oy + h)):
        srow, drow = (y - oy) * w, y * P.CELL_STRIDE
        for x in range(max(0, ox), min(P.CELL_W, ox + w)):
            if bits[srow + x - ox]:
                out[drow + (x >> 3)] |= 1 << (x & 7)   # bit 0 is leftmost
    return bytes(out)


def pack(bits, w: int, h: int, ox: int = 0, oy: int = 0) -> bytes:
    """Cut a 64x32 cell out of a larger bit plane, at offset (ox, oy)."""
    return pack_at(bits, w, h, -ox, -oy)


# Sharpening helps a threshold recover detail the downscale averaged away.
# It does the opposite for error diffusion, which already carries that detail
# as texture and only gets noisier for the help.
THRESHOLDS = ("otsu", "none", "adaptive")


def to_cell(luma, sw: int, sh: int, dither: str = "auto",
            fit: str = "contain", polarity: str = "auto",
            threshold: int = 128, levels: bool = False,
            sharpen_amount: float | None = None) -> bytes:
    """The whole pipeline for one cell, ready for `EC50.set_bitmap`.

    The picture is scaled on its own and only then dropped into the cell, so
    letterbox padding never reaches the threshold. Mixing the two is what puts
    a hard line down the join: a local threshold sees bright padding beside the
    picture's edge, decides the edge is dark by comparison, and inks it.
    """
    if polarity not in POLARITIES:
        raise ValueError(f"polarity must be one of {POLARITIES}")
    ox, oy, tw, th = fit_box(sw, sh, P.CELL_W, P.CELL_H, fit)
    small = resample(luma, sw, sh, tw, th)
    if levels:
        small = autolevel(small)
    if sharpen_amount is None:
        sharpen_amount = 1.0 if dither in THRESHOLDS else 0.0
    if sharpen_amount:
        small = sharpen(small, tw, th, sharpen_amount)

    bits = to_bits(small, tw, th, dither, threshold, polarity == "light")
    if polarity == "auto" and sum(bits) * 2 > len(bits):
        # Whichever way round leaves less ink. A set pixel is dark on a lit
        # backlight, so the majority tone should be the one that stays lit:
        # white text on a black button becomes dark text on a lit key, and a
        # dark cat on a pale wall stays a dark cat instead of a negative.
        bits = to_bits(small, tw, th, dither, threshold, True)
    return pack_at(bits, tw, th, ox, oy)


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
