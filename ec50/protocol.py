"""EC-50 wire protocol: register map, framebuffer layout, button table.

All of it decoded from USB captures of Barco's Event Master Toolset and then
confirmed on hardware. See the accompanying report for how each part was found.

SPI transaction, wrapped in MPSSE by the panel layer:
    <addr_hi> <addr_lo> <flags> <len-1>  [data ...]  0xAA 0xBB
    flags bit 7 set = read.

Nothing takes effect until it is latched via COMMIT (0x3828), which is a mask:
bit 0 commits the LED registers, bit 1 commits the framebuffer. Writes without
the latch are accepted and silently ignored - the single easiest mistake to make
with this panel.
"""

import base64
import zlib

# --- registers -------------------------------------------------------------

REG_FRAMEBUFFER = 0x0000
REG_KEYS = 0x3810          # byte 6 = key FIFO, bytes 5:4 = 16-bit T-bar
REG_COMMIT = 0x3828
COMMIT_LEDS = 1
COMMIT_LCD = 2

LED_REGS = (0x3950, 0x3954, 0x3958, 0x395C, 0x3960, 0x3964)

# LCD controller setup, issued once by the Toolset at startup. The panel keeps
# it across a host disconnect, so it is only needed after a power cycle.
INIT_REGS = (
    (0x3938, 0x0000003F),
    (0x3920, 0x00760131),
    (0x3924, 0x00760131),
    (0x3928, 0x00760131),
    (0x392C, 0x00180131),
    (0x3930, 0x00AF0131),
    (0x3934, 0x00AF0131),
)

# --- framebuffer -----------------------------------------------------------

CELL_W, CELL_H = 64, 32
CELL_STRIDE = CELL_W // 8       # 8 bytes per scanline, LSB is the LEFTMOST pixel
CELL_SIZE = CELL_STRIDE * CELL_H
NUM_CELLS = 45

# The display's right half is driven one row lower than the buffer says - two
# 32px-wide driver chips with a one-row addressing skew. Barco compensates by
# storing content at x >= 32 one row higher, consistently across every captured
# buffer. Without this, text straddling x=32 shows a visible 1px step.
SKEW_X = 32
SKEW_ROWS = 1

HEADER_LEN = 4                  # address + length; overlays cell 0's first row
COLOUR_BASE = 11524             # 45 x 4; only byte 3 of each entry is read
COLOUR2_BASE = 11780            # 45 x 4; uniformly 0xFF, purpose unknown
PAYLOAD_LEN = 11962

# A captured buffer with every cell blanked but the header and both colour
# tables intact. Zeroing the header makes the panel discard the whole write.
_BASE_B64 = (
    "eNrtzyEOACAMA8D9kl/wMCySx4GdnMAsuZNt0qQRcwcAAAAAAAAAAAAAQBOj4CY5y934pLL18/9taJ0HvKcFbg=="
)


def new_buffer() -> bytearray:
    buf = bytearray(zlib.decompress(base64.b64decode(_BASE_B64)))
    if len(buf) != PAYLOAD_LEN:
        raise RuntimeError(f"base buffer is {len(buf)} bytes, expected {PAYLOAD_LEN}")
    return buf


# --- colour ----------------------------------------------------------------

def colour(r: int, g: int, b: int) -> int:
    """r, g, b each 0-3 -> the byte selecting that colour.

    Layout is RRGGBBII. The low two bits must both be set or the panel ignores
    the colour and falls back to white. 64 colours are addressable.
    """
    return ((r & 3) << 6) | ((g & 3) << 4) | ((b & 3) << 2) | 3


class Colour:
    OFF = colour(0, 0, 0)        # 0x03
    DIM = colour(1, 1, 1)        # 0x57 - the Toolset's blank-button value
    WHITE = colour(3, 3, 3)      # 0xFF - its labelled-button value
    RED = colour(3, 0, 0)
    GREEN = colour(0, 3, 0)
    BLUE = colour(0, 0, 3)
    YELLOW = colour(3, 3, 0)
    CYAN = colour(0, 3, 3)
    MAGENTA = colour(3, 0, 3)
    ORANGE = colour(3, 1, 0)
    PINK = colour(3, 1, 2)


class Led:
    """Two bits per key. Not independent enables - 3 is off, there is no amber."""
    OFF = 0
    RED = 1
    GREEN = 2


# --- input -----------------------------------------------------------------

KEY_BYTE = 6            # 0xFF = queue empty, else 0x80|index down / index up
KEY_IDLE = 0xFF
TBAR_LO, TBAR_HI = 4, 5
TBAR_MIN, TBAR_MAX = 0x0040, 0xFC10     # measured across full-travel runs


def tbar_percent(value: int) -> float:
    pct = (value - TBAR_MIN) / (TBAR_MAX - TBAR_MIN) * 100.0
    return max(0.0, min(100.0, pct))


# --- button table ----------------------------------------------------------
# index -> (name, lcd cell or -1, LED register or 0xFFFF, LED bit position)
# 82 buttons: 45 carry an LCD cell, 72 carry an LED.

BUTTON_INFO = {
    0: ("DEST_0", -1, 0x3950, 22),
    1: ("DEST_1", -1, 0x3950, 20),
    2: ("DEST_2", -1, 0x3950, 18),
    3: ("DEST_3", -1, 0x3950, 16),
    4: ("DEST_4", -1, 0x3950, 14),
    5: ("DEST_5", -1, 0x3950, 12),
    6: ("DEST_6", -1, 0x3950, 10),
    7: ("DEST_7", -1, 0x3950, 8),
    8: ("DEST_8", -1, 0x3950, 6),
    9: ("DEST_9", -1, 0x3950, 4),
    10: ("DEST_10", -1, 0x3950, 2),
    11: ("DEST_11", 9, 0x3950, 0),
    12: ("DEST_UP", -1, 0xFFFF, 0),
    13: ("DEST_DOWN", -1, 0xFFFF, 0),
    25: ("LAYER_UP", -1, 0xFFFF, 0),
    26: ("FREEZE_PGM", -1, 0x3954, 10),
    27: ("ARM", -1, 0x3954, 8),
    28: ("LAYER_TRANS", -1, 0x3954, 6),
    29: ("SHOW_CFG_0_1", 13, 0x3954, 4),
    30: ("SHOW_CFG_0_0", 12, 0x3954, 2),
    31: ("SHOW_CFG_1_1", 16, 0x3954, 0),
    32: ("LAYER_0", -1, 0x3958, 30),
    33: ("LAYER_1", -1, 0x3958, 28),
    34: ("LAYER_2", -1, 0x3958, 26),
    35: ("LAYER_3", -1, 0x3958, 24),
    36: ("LAYER_4", -1, 0x3958, 22),
    37: ("LAYER_5", -1, 0x3958, 20),
    38: ("LAYER_6", -1, 0x3958, 18),
    39: ("LAYER_7", -1, 0x3958, 16),
    40: ("LAYER_8", 10, 0x3958, 14),
    41: ("LAYER_DOWN", -1, 0xFFFF, 0),
    42: ("FREEZE_PVW", -1, 0x3958, 10),
    43: ("MATCH", -1, 0x3958, 8),
    44: ("LAYER_CUT", -1, 0x3958, 6),
    48: ("ASSIGN_0_0", 36, 0x395C, 30),
    49: ("ASSIGN_0_1", 37, 0x395C, 28),
    50: ("ASSIGN_0_2", 2, 0x395C, 26),
    51: ("ASSIGN_0_3", 0, 0x395C, 24),
    52: ("ASSIGN_0_4", 1, 0x395C, 22),
    53: ("ASSIGN_0_5", 20, 0x395C, 20),
    54: ("ASSIGN_0_6", 18, 0x395C, 18),
    55: ("ASSIGN_0_7", 19, 0x395C, 16),
    56: ("ASSIGN_0_8", 29, 0x395C, 14),
    57: ("ASSIGN_0_9", 27, 0x395C, 12),
    58: ("ASSIGN_0_10", 28, 0x395C, 10),
    59: ("ASSIGN_0_11", 38, 0x395C, 8),
    60: ("ASSIGN_0_UP", -1, 0xFFFF, 0),
    61: ("ASSIGN_0_DOWN", -1, 0xFFFF, 0),
    62: ("ASSIGN_0_LABEL", 11, 0x395C, 2),
    63: ("SHOW_CFG_1_0", 15, 0x395C, 0),
    64: ("ASSIGN_1_0", 39, 0x3960, 24),
    65: ("ASSIGN_1_1", 40, 0x3960, 26),
    66: ("ASSIGN_1_2", 5, 0x3960, 28),
    67: ("ASSIGN_1_3", 3, 0x3960, 30),
    68: ("ASSIGN_1_4", 4, 0x3960, 16),
    69: ("ASSIGN_1_5", 23, 0x3960, 18),
    70: ("ASSIGN_1_6", 21, 0x3960, 20),
    71: ("ASSIGN_1_7", 22, 0x3960, 22),
    72: ("ASSIGN_1_8", 32, 0x3960, 8),
    73: ("ASSIGN_1_9", 30, 0x3960, 10),
    74: ("ASSIGN_1_10", 31, 0x3960, 12),
    75: ("ASSIGN_1_11", 41, 0x3960, 14),
    76: ("ASSIGN_1_UP", -1, 0xFFFF, 0),
    77: ("ASSIGN_1_DOWN", -1, 0xFFFF, 0),
    78: ("ASSIGN_1_LABEL", 14, 0x3960, 2),
    79: ("ALL_TRANS", -1, 0x3960, 0),
    80: ("ASSIGN_2_0", 42, 0x3964, 24),
    81: ("ASSIGN_2_1", 43, 0x3964, 26),
    82: ("ASSIGN_2_2", 8, 0x3964, 28),
    83: ("ASSIGN_2_3", 6, 0x3964, 30),
    84: ("ASSIGN_2_4", 7, 0x3964, 16),
    85: ("ASSIGN_2_5", 26, 0x3964, 18),
    86: ("ASSIGN_2_6", 24, 0x3964, 20),
    87: ("ASSIGN_2_7", 25, 0x3964, 22),
    88: ("ASSIGN_2_8", 35, 0x3964, 8),
    89: ("ASSIGN_2_9", 33, 0x3964, 10),
    90: ("ASSIGN_2_10", 34, 0x3964, 12),
    91: ("ASSIGN_2_11", 44, 0x3964, 14),
    92: ("ASSIGN_2_UP", -1, 0xFFFF, 0),
    93: ("ASSIGN_2_DOWN", -1, 0xFFFF, 0),
    94: ("ASSIGN_2_LABEL", 17, 0x3964, 2),
    95: ("CUT", -1, 0x3964, 0),
}

BUTTONS = {i: v[0] for i, v in BUTTON_INFO.items()}

# The Assign grid, rows top to bottom, columns left to right.
# Row 0's LED bits descend while rows 1 and 2 ascend, so these come from
# Barco's own showConsoleMap.csv rather than a loop.
ASSIGN = [
    [36, 37, 2, 0, 1, 20, 18, 19, 29, 27, 28, 38],
    [39, 40, 5, 3, 4, 23, 21, 22, 32, 30, 31, 41],
    [42, 43, 8, 6, 7, 26, 24, 25, 35, 33, 34, 44],
]
ASSIGN_INDEX = [
    list(range(48, 60)),
    list(range(64, 76)),
    list(range(80, 92)),
]
