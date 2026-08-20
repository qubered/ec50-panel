"""Variable-width bitmap font for the EC-50's 64x32 1bpp cells.

Letters are 5 columns wide, icons 8. Each glyph is a tuple of column bytes;
bit 0 is the top row, bit 7 the eighth. Eight rows rather than seven so
lowercase descenders (g j p q y) sit below the baseline.

Companion button text is arbitrary UTF-8, so anything without a glyph is folded
rather than dropped:

  * accents are stripped   - "CAMÉRA" renders as "CAMERA"
  * emoji map to a monochrome icon where one makes sense - a red circle, a
    green circle and a black circle all become a filled disc, because on a
    1-bit display that is all any of them can be
  * whatever is left becomes a small filled square

Use `render()` to eyeball a string as ASCII art without hardware.
"""

import unicodedata

GLYPH_H = 8
GAP = 1              # blank columns between glyphs

FONT = {
    " ": (0x00, 0x00, 0x00, 0x00, 0x00),
    "!": (0x00, 0x00, 0x5F, 0x00, 0x00),
    '"': (0x00, 0x07, 0x00, 0x07, 0x00),
    "#": (0x14, 0x7F, 0x14, 0x7F, 0x14),
    "$": (0x24, 0x2A, 0x7F, 0x2A, 0x12),
    "%": (0x23, 0x13, 0x08, 0x64, 0x62),
    "&": (0x36, 0x49, 0x55, 0x22, 0x50),
    "'": (0x00, 0x05, 0x03, 0x00, 0x00),
    "(": (0x00, 0x1C, 0x22, 0x41, 0x00),
    ")": (0x00, 0x41, 0x22, 0x1C, 0x00),
    "*": (0x14, 0x08, 0x3E, 0x08, 0x14),
    "+": (0x08, 0x08, 0x3E, 0x08, 0x08),
    ",": (0x00, 0x50, 0x30, 0x00, 0x00),
    "-": (0x08, 0x08, 0x08, 0x08, 0x08),
    ".": (0x00, 0x60, 0x60, 0x00, 0x00),
    "/": (0x20, 0x10, 0x08, 0x04, 0x02),
    "0": (0x3E, 0x51, 0x49, 0x45, 0x3E),
    "1": (0x00, 0x42, 0x7F, 0x40, 0x00),
    "2": (0x42, 0x61, 0x51, 0x49, 0x46),
    "3": (0x21, 0x41, 0x45, 0x4B, 0x31),
    "4": (0x18, 0x14, 0x12, 0x7F, 0x10),
    "5": (0x27, 0x45, 0x45, 0x45, 0x39),
    "6": (0x3C, 0x4A, 0x49, 0x49, 0x30),
    "7": (0x01, 0x71, 0x09, 0x05, 0x03),
    "8": (0x36, 0x49, 0x49, 0x49, 0x36),
    "9": (0x06, 0x49, 0x49, 0x29, 0x1E),
    ":": (0x00, 0x36, 0x36, 0x00, 0x00),
    ";": (0x00, 0x56, 0x36, 0x00, 0x00),
    "<": (0x00, 0x08, 0x14, 0x22, 0x41),
    "=": (0x14, 0x14, 0x14, 0x14, 0x14),
    ">": (0x41, 0x22, 0x14, 0x08, 0x00),
    "?": (0x02, 0x01, 0x51, 0x09, 0x06),
    "@": (0x32, 0x49, 0x79, 0x41, 0x3E),
    "A": (0x7E, 0x11, 0x11, 0x11, 0x7E),
    "B": (0x7F, 0x49, 0x49, 0x49, 0x36),
    "C": (0x3E, 0x41, 0x41, 0x41, 0x22),
    "D": (0x7F, 0x41, 0x41, 0x22, 0x1C),
    "E": (0x7F, 0x49, 0x49, 0x49, 0x41),
    "F": (0x7F, 0x09, 0x09, 0x09, 0x01),
    "G": (0x3E, 0x41, 0x49, 0x49, 0x7A),
    "H": (0x7F, 0x08, 0x08, 0x08, 0x7F),
    "I": (0x00, 0x41, 0x7F, 0x41, 0x00),
    "J": (0x20, 0x40, 0x41, 0x3F, 0x01),
    "K": (0x7F, 0x08, 0x14, 0x22, 0x41),
    "L": (0x7F, 0x40, 0x40, 0x40, 0x40),
    "M": (0x7F, 0x02, 0x0C, 0x02, 0x7F),
    "N": (0x7F, 0x04, 0x08, 0x10, 0x7F),
    "O": (0x3E, 0x41, 0x41, 0x41, 0x3E),
    "P": (0x7F, 0x09, 0x09, 0x09, 0x06),
    "Q": (0x3E, 0x41, 0x51, 0x21, 0x5E),
    "R": (0x7F, 0x09, 0x19, 0x29, 0x46),
    "S": (0x46, 0x49, 0x49, 0x49, 0x31),
    "T": (0x01, 0x01, 0x7F, 0x01, 0x01),
    "U": (0x3F, 0x40, 0x40, 0x40, 0x3F),
    "V": (0x1F, 0x20, 0x40, 0x20, 0x1F),
    "W": (0x3F, 0x40, 0x38, 0x40, 0x3F),
    "X": (0x63, 0x14, 0x08, 0x14, 0x63),
    "Y": (0x07, 0x08, 0x70, 0x08, 0x07),
    "Z": (0x61, 0x51, 0x49, 0x45, 0x43),
    "[": (0x00, 0x7F, 0x41, 0x41, 0x00),
    "\\": (0x02, 0x04, 0x08, 0x10, 0x20),
    "]": (0x00, 0x41, 0x41, 0x7F, 0x00),
    "^": (0x04, 0x02, 0x01, 0x02, 0x04),
    "_": (0x40, 0x40, 0x40, 0x40, 0x40),
    "`": (0x00, 0x01, 0x02, 0x04, 0x00),
    "a": (0x20, 0x54, 0x54, 0x54, 0x78),
    "b": (0x7F, 0x48, 0x44, 0x44, 0x38),
    "c": (0x38, 0x44, 0x44, 0x44, 0x20),
    "d": (0x38, 0x44, 0x44, 0x48, 0x7F),
    "e": (0x38, 0x54, 0x54, 0x54, 0x18),
    "f": (0x08, 0x7E, 0x09, 0x01, 0x02),
    "g": (0x18, 0xA4, 0xA4, 0xA4, 0x7C),
    "h": (0x7F, 0x08, 0x04, 0x04, 0x78),
    "i": (0x00, 0x44, 0x7D, 0x40, 0x00),
    "j": (0x40, 0x80, 0x84, 0x7D, 0x00),
    "k": (0x7F, 0x10, 0x28, 0x44, 0x00),
    "l": (0x00, 0x41, 0x7F, 0x40, 0x00),
    "m": (0x7C, 0x04, 0x18, 0x04, 0x78),
    "n": (0x7C, 0x08, 0x04, 0x04, 0x78),
    "o": (0x38, 0x44, 0x44, 0x44, 0x38),
    "p": (0xFC, 0x24, 0x24, 0x24, 0x18),
    "q": (0x18, 0x24, 0x24, 0x24, 0xFC),
    "r": (0x7C, 0x08, 0x04, 0x04, 0x08),
    "s": (0x48, 0x54, 0x54, 0x54, 0x20),
    "t": (0x04, 0x3F, 0x44, 0x40, 0x20),
    "u": (0x3C, 0x40, 0x40, 0x20, 0x7C),
    "v": (0x1C, 0x20, 0x40, 0x20, 0x1C),
    "w": (0x3C, 0x40, 0x30, 0x40, 0x3C),
    "x": (0x44, 0x28, 0x10, 0x28, 0x44),
    "y": (0x1C, 0xA0, 0xA0, 0xA0, 0x7C),
    "z": (0x44, 0x64, 0x54, 0x4C, 0x44),
    "{": (0x00, 0x08, 0x36, 0x41, 0x00),
    "|": (0x00, 0x00, 0x7F, 0x00, 0x00),
    "}": (0x00, 0x41, 0x36, 0x08, 0x00),
    "~": (0x08, 0x04, 0x08, 0x10, 0x08),
    # beyond ASCII, useful on a control surface
    "°": (0x00, 0x07, 0x05, 0x07, 0x00),
    "·": (0x00, 0x00, 0x18, 0x00, 0x00),
    "•": (0x00, 0x1C, 0x1C, 0x1C, 0x00),
    "£": (0x48, 0x7E, 0x49, 0x41, 0x42),
    "€": (0x14, 0x3E, 0x55, 0x41, 0x22),
    "¢": (0x18, 0x24, 0x7E, 0x24, 0x12),
    "¥": (0x15, 0x16, 0x7C, 0x16, 0x15),
    "±": (0x48, 0x48, 0x5E, 0x48, 0x48),
    "←": (0x08, 0x1C, 0x2A, 0x08, 0x08),
    "→": (0x08, 0x08, 0x2A, 0x1C, 0x08),
    "↑": (0x04, 0x02, 0x7F, 0x02, 0x04),
    "↓": (0x10, 0x20, 0x7F, 0x20, 0x10),
}

# ---------------------------------------------------------------------------
# Icons. Drawn as art for legibility, converted to column bytes at import.
# ---------------------------------------------------------------------------

_ICON_ART = {
    "▶": """........
              .#......
              .###....
              .#####..
              .######.
              .#####..
              .###....
              .#......""",
    "⏸": """........
             .##..##.
             .##..##.
             .##..##.
             .##..##.
             .##..##.
             .##..##.
             ........""",
    "⏹": """........
             .######.
             .######.
             .######.
             .######.
             .######.
             .######.
             ........""",
    "●": """........
             ..####..
             .######.
             .######.
             .######.
             .######.
             ..####..
             ........""",
    "○": """........
             ..####..
             .##..##.
             .#....#.
             .#....#.
             .##..##.
             ..####..
             ........""",
    "⏭": """........
             .#....##
             .###..##
             .#######
             .#######
             .###..##
             .#....##
             ........""",
    "⏮": """........
             ##....#.
             ##..###.
             .#######
             .#######
             ##..###.
             ##....#.
             ........""",
    "✓": """........
             .......#
             ......##
             .#...##.
             .##.##..
             ..####..
             ...##...
             ........""",
    "✗": """........
             .#....#.
             .##..##.
             ..####..
             ...##...
             ..####..
             .##..##.
             .#....#.""",
    "★": """...##...
             ...##...
             .######.
             ..####..
             ..####..
             .##..##.
             .#....#.
             ........""",
    "♥": """........
             .##..##.
             ########
             ########
             ########
             .######.
             ..####..
             ...##...""",
    "⚠": """...##...
            ..#..#..
            ..#..#..
            .#.##.#.
            .#.##.#.
            #......#
            #..##..#
            ########""",
    "🔊": """....#..#
             ...##.#.
             .####.#.
             .####..#
             .####..#
             .####.#.
             ...##.#.
             ....#..#""",
    "🔇": """........
             ....#...
             ...##.#.
             .####.#.
             .####...
             .####.#.
             ...##.#.
             ....#...""",
    "🔒": """..####..
             .##..##.
             .##..##.
             ########
             ##....##
             ##.##.##
             ##....##
             ########""",
    "🔓": """..####..
             .##..##.
             .##.....
             ########
             ##....##
             ##.##.##
             ##....##
             ########""",
    "⏰": """.#....#.
            ..####..
            .#.##.#.
            ##.##.##
            ##.####.
            ##....##
            ..####..
            .#....#.""",
    "💡": """..####..
             .##..##.
             ##....##
             ##....##
             .##..##.
             ..####..
             ..#..#..
             ..####..""",
    "🎤": """...##...
             ..####..
             ..####..
             ..####..
             ..####..
             .#.##.#.
             ..####..
             ....#...""",
    "📷": """........
             ..##....
             ########
             ##....##
             ##.##.##
             ##.##.##
             ##....##
             ########""",
    "🔥": """....#...
             ...##...
             ...###..
             ..#####.
             .###.###
             .##...##
             .###.###
             ..#####.""",
    "👍": """......##
             .....##.
             ....##..
             .#####..
             .#####..
             .#####..
             .#####..
             ..###...""",
    "▪": """........
             ..####..
             ..####..
             ..####..
             ..####..
             ..####..
             ........
             ........""",
}


def _to_columns(art):
    rows = [r.strip() for r in art.split("\n")]
    rows = [r for r in rows if r]
    width = max(len(r) for r in rows)
    cols = []
    for c in range(width):
        byte = 0
        for r, row in enumerate(rows[:GLYPH_H]):
            if c < len(row) and row[c] == "#":
                byte |= 1 << r
        cols.append(byte)
    return tuple(cols)


FONT.update({ch: _to_columns(art) for ch, art in _ICON_ART.items()})

FALLBACK = "▪"

# Characters with no glyph of their own but an obvious stand-in. On a 1-bit
# display every coloured disc is the same disc, so they all fold together.
_ALIASES = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": ".", " ": " ",
    "×": "x", "÷": "/", "−": "-", "‑": "-",
    "▸": "▶", "►": "▶", "▷": "▶", "⏵": "▶", "⯈": "▶",
    "⏯": "▶", "⏩": "⏭", "⏪": "⏮", "⏫": "↑", "⏬": "↓",
    "⏺": "●", "🔴": "●", "🟢": "●", "🔵": "●", "⚫": "●",
    "🟠": "●", "🟡": "●", "🟣": "●", "🟤": "●", "🔘": "●",
    "⚪": "○", "◯": "○", "◦": "·",
    "✅": "✓", "✔": "✓", "☑": "✓", "🗸": "✓",
    "❌": "✗", "✖": "✗", "❎": "✗", "🚫": "✗", "⛔": "✗", "🛑": "✗",
    "⭐": "★", "☆": "★", "🌟": "★", "✨": "★",
    "❤": "♥", "♡": "♥", "💚": "♥", "💙": "♥", "🧡": "♥", "💛": "♥",
    "⚡": "💡", "🔦": "💡", "🕯": "💡", "🌞": "💡",
    "🔈": "🔊", "🔉": "🔊", "📢": "🔊", "📣": "🔊",
    "🔕": "🔇", "🎥": "📷", "📹": "📷", "📺": "📷", "🎬": "📷", "🖥": "📷",
    "🎛": "🎤", "🎚": "🎤", "🎙": "🎤", "🎧": "🎤",
    "🔐": "🔒", "🔏": "🔒",
    "⏱": "⏰", "⌚": "⏰", "⏲": "⏰", "🕐": "⏰",
    "⬆": "↑", "⬇": "↓", "⬅": "←", "➡": "→",
    "▲": "↑", "▼": "↓", "◀": "←", "🔺": "↑", "🔻": "↓",
    "◼": "▪", "◾": "▪", "■": "▪", "⬛": "▪", "🔲": "▪", "🔳": "▪",
    "⬜": "○", "◻": "○", "▫": "·",
    "‼": "!", "⁉": "?", "❗": "!", "❓": "?", "⚠️": "⚠",
    "🆗": "✓", "🆖": "✗", "💥": "🔥", "☀": "💡",
}

# Variation selectors and zero-width joiners carry no glyph of their own.
_INVISIBLE = {"️", "︎", "‍", "​"}


def normalise(text: str) -> str:
    """Fold arbitrary text down to characters the font can draw."""
    out = []
    for ch in text:
        if ch in _INVISIBLE:
            continue
        if ch in FONT:
            out.append(ch)
            continue
        alias = _ALIASES.get(ch)
        if alias is not None:
            out.append(alias)
            continue
        stripped = "".join(c for c in unicodedata.normalize("NFKD", ch)
                           if not unicodedata.combining(c))
        if stripped and all(c in FONT for c in stripped):
            out.extend(stripped)
        else:
            out.append(FALLBACK)
    return "".join(out)


def char_width(ch) -> int:
    return len(FONT.get(ch, ()))


def text_width(text, scale) -> int:
    """Glyphs are variable width: letters are 5 columns, icons 8."""
    if not text:
        return 0
    return (sum(char_width(c) + GAP for c in text) - GAP) * scale


def pick_scale(text, cell_w):
    for scale in (3, 2, 1):
        if text_width(text, scale) <= cell_w:
            return scale
    return 1


def fit(text, cell_w, scale=None):
    """Normalise text and choose a scale that fits, truncating if it must."""
    text = normalise(text)
    if scale is None:
        scale = pick_scale(text, cell_w)
    while text and text_width(text, scale) > cell_w:
        text = text[:-1]
    return text, scale


def plot(text, cell_w, cell_h, scale=None, y=None):
    """Yield (x, y) pixels for `text` centred in a cell."""
    text, scale = fit(text, cell_w, scale)
    x = (cell_w - text_width(text, scale)) // 2
    if y is None:
        y = (cell_h - GLYPH_H * scale) // 2
    for ch in text:
        cols = FONT[ch]
        for c, bits in enumerate(cols):
            for r in range(GLYPH_H):
                if bits & (1 << r):
                    for dx in range(scale):
                        for dy in range(scale):
                            yield x + c * scale + dx, y + r * scale + dy
        x += (len(cols) + GAP) * scale


def render(text, cell_w=64, cell_h=32, scale=None):
    """ASCII-art a string, for eyeballing glyphs without hardware."""
    grid = [["."] * cell_w for _ in range(cell_h)]
    for x, y in plot(text, cell_w, cell_h, scale):
        if 0 <= x < cell_w and 0 <= y < cell_h:
            grid[y][x] = "#"
    rows = ["".join(r) for r in grid]
    while rows and not rows[0].strip("."):
        rows.pop(0)
    while rows and not rows[-1].strip("."):
        rows.pop()
    return rows
