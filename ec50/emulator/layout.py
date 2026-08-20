"""Where everything physically sits on the EC-50's front panel.

Geometry only - the silkscreen, not the semantics. A key here is a rectangle,
a name and a button index; what a real Event Master would *do* when you press
it is no business of this driver or this emulator. Pressing DEST_3 queues key
event 3 and nothing else.

Proportions measured off the front panel diagram in the EC-50 quick start
guide, which is drawn to the panel: twelve columns on a 54-unit pitch carrying
52-unit keys, so the gaps are hairlines, and rows that butt up against each
other with each Assign row's display sitting directly on its own keycap. The
arrangement - destinations across the back, layers below them with the coloured
layer-function caps to their right, three Assign rows filling the middle, the
T-bar at the back right corner and CUT / ALL TRANS at the front right - is from
that diagram and the product photo.

Cell numbers and button indices come from `protocol.BUTTON_INFO`, which is
Barco's own showConsoleMap.csv.

Coordinates are in panel units; the browser scales the whole field to fit.
"""

from __future__ import annotations

from .. import protocol as P

# -- the grid ----------------------------------------------------------------
# Barco's diagram: 52 wide on a 54 pitch. Held here as a ratio so the display,
# which has to keep the cell's real 2:1 shape, can set the scale.

PAD = 16
KEY_W = 96
COL = 100                       # 4% gap, as measured
ARROW_W = 62
ROW_GAP = 4                     # rows very nearly touch

# The 64x32 cell at 1.5x. Unlike Barco's diagram, which draws a square button,
# this keeps the display's true 2:1 shape - it is showing real pixels.
LCD_SCALE = 1.5
LCD_W = int(P.CELL_W * LCD_SCALE)       # 96
LCD_H = int(P.CELL_H * LCD_SCALE)       # 48

CAP_H = 50                      # the domed keycap under an Assign display
CAP_INSET = 5                   # the cap is narrower than the display above it
DOME_H = 58                     # a plain key, destinations and layers
ASSIGN_H = LCD_H + CAP_H        # display and cap are one key, stacked
LABEL_H = 24                    # silkscreen band above each group

# -- columns -----------------------------------------------------------------

X0 = PAD
X_ARROWS = X0 + 12 * COL + 6            # page up / page down, stacked
X_PAGE = X_ARROWS + ARROW_W + 4         # the page display
X_SIDE0 = X_PAGE + KEY_W + 16           # show configuration, and CUT/ALL TRANS
X_SIDE1 = X_SIDE0 + COL
X_TBAR = X_SIDE1 + KEY_W + 18
TBAR_W = 148
WIDTH = X_TBAR + TBAR_W + PAD

# -- rows --------------------------------------------------------------------

Y_SCALE = 4                             # the 1..12 numbers printed above the keys
Y_DEST_LABEL = 22
Y_DEST = Y_DEST_LABEL + LABEL_H
Y_LAYER_LABEL = Y_DEST + DOME_H + ROW_GAP
Y_LAYER = Y_LAYER_LABEL + LABEL_H
Y_ASSIGN_LABEL = Y_LAYER + DOME_H + ROW_GAP
Y_ASSIGN = tuple(Y_ASSIGN_LABEL + LABEL_H + i * (ASSIGN_H + ROW_GAP)
                 for i in range(3))
Y_BOTTOM = Y_ASSIGN[2] + ASSIGN_H + 10
HEIGHT = Y_BOTTOM + 48 + PAD

# -- silkscreen --------------------------------------------------------------

LAYER_LABELS = ["BG", "1", "2", "3", "4", "5", "6", "7", "8"]

# Three pairs of coloured caps to the right of the layer keys, as on the panel:
# blue freeze, green arm and match, red layer transition.
FUNCTION_KEYS = [
    (9,  ("FREEZE_PGM", "Freeze\nPGM"), ("FREEZE_PVW", "Freeze\nPVW"), "blue"),
    (10, ("ARM", "Arm"), ("MATCH", "Match\nPGM"), "green"),
    (11, ("LAYER_TRANS", "Layer\nTrans"), ("LAYER_CUT", "Layer\nCut"), "red"),
]

_BY_NAME = {name: index for index, (name, *_) in P.BUTTON_INFO.items()}


def _key(name, x, y, w, h, kind, label="", cell=None, tint=None):
    index = _BY_NAME[name]
    _, csv_cell, reg, _bit = P.BUTTON_INFO[index]
    return {
        "id": name, "name": name, "index": index, "kind": kind,
        "label": label, "cell": cell,
        "x": x, "y": y, "w": w, "h": h,
        "tint": tint, "led": reg != 0xFFFF,
        # Barco's CSV files a display against some keys that do not carry one;
        # `cell` is what is physically on the key, csv_cell is what the file says.
        "csv_cell": csv_cell,
    }


def _display(cell, x, y, label):
    """A cell with no key under it - the two page-number windows."""
    return {"id": f"display{cell}", "name": f"DISPLAY_{cell}", "index": None,
            "kind": "display", "label": label, "cell": cell,
            "x": x, "y": y, "w": KEY_W, "h": LCD_H,
            "tint": None, "led": False, "csv_cell": cell}


def _arrows(prefix, y, h):
    """Page up over page down, filling the height of one group."""
    half = (h - ROW_GAP) // 2
    return [
        _key(f"{prefix}_UP", X_ARROWS, y, ARROW_W, half, "arrow", "↑"),
        _key(f"{prefix}_DOWN", X_ARROWS, y + half + ROW_GAP, ARROW_W, half,
             "arrow", "↓"),
    ]


def build() -> dict:
    keys: list[dict] = []
    groups: list[dict] = []
    displays: list[dict] = []

    # -- destinations -------------------------------------------------------
    groups.append({"label": "Destinations", "x": X0, "y": Y_DEST_LABEL,
                   "w": 12 * COL, "h": LABEL_H})
    for c in range(12):
        keys.append(_key(f"DEST_{c}", X0 + c * COL, Y_DEST, KEY_W, DOME_H, "dome"))
    keys += _arrows("DEST", Y_DEST_LABEL, LABEL_H + DOME_H)
    displays.append(_display(9, X_PAGE, Y_DEST_LABEL, "destination page"))

    # -- layers -------------------------------------------------------------
    groups.append({"label": "Layers", "x": X0, "y": Y_LAYER_LABEL,
                   "w": 9 * COL, "h": LABEL_H})
    for c, label in enumerate(LAYER_LABELS):
        keys.append(_key(f"LAYER_{c}", X0 + c * COL, Y_LAYER, KEY_W, DOME_H,
                         "dome", label))
    keys += _arrows("LAYER", Y_LAYER_LABEL, LABEL_H + DOME_H)
    displays.append(_display(10, X_PAGE, Y_LAYER_LABEL, "layer page"))

    # The coloured caps fill the layer row's spare columns, two deep.
    fn_h = (LABEL_H + DOME_H - ROW_GAP) // 2
    for slot, (top_name, top_label), (bot_name, bot_label), tint in FUNCTION_KEYS:
        x = X0 + slot * COL
        keys.append(_key(top_name, x, Y_LAYER_LABEL, KEY_W, fn_h, "fn",
                         top_label, tint=tint))
        keys.append(_key(bot_name, x, Y_LAYER_LABEL + fn_h + ROW_GAP, KEY_W,
                         fn_h, "fn", bot_label, tint=tint))

    # -- assign -------------------------------------------------------------
    groups.append({"label": "Assign", "x": X0, "y": Y_ASSIGN_LABEL,
                   "w": 12 * COL, "h": LABEL_H})
    for r in range(3):
        y = Y_ASSIGN[r]
        for c in range(12):
            keys.append(_key(f"ASSIGN_{r}_{c}", X0 + c * COL, y, KEY_W,
                             ASSIGN_H, "assign", cell=P.ASSIGN[r][c]))
        keys += _arrows(f"ASSIGN_{r}", y, ASSIGN_H)
        name = f"ASSIGN_{r}_LABEL"
        keys.append(_key(name, X_PAGE, y, KEY_W, ASSIGN_H, "assign",
                         cell=P.BUTTON_INFO[_BY_NAME[name]][1]))

    # -- show configuration -------------------------------------------------
    # Four contextual display keys, a 2x2 block beside the first two Assign rows.
    for r in range(2):
        for c in range(2):
            name = f"SHOW_CFG_{r}_{c}"
            keys.append(_key(name, (X_SIDE0, X_SIDE1)[c], Y_ASSIGN[r],
                             KEY_W, ASSIGN_H, "assign",
                             cell=P.BUTTON_INFO[_BY_NAME[name]][1]))

    # -- transitions --------------------------------------------------------
    keys.append(_key("CUT", X_SIDE0, Y_BOTTOM, KEY_W, 48, "trans", "CUT",
                     tint="red"))
    keys.append(_key("ALL_TRANS", X_SIDE1, Y_BOTTOM, KEY_W, 48, "trans",
                     "All Trans", tint="red"))

    # -- T-bar --------------------------------------------------------------
    # Back right corner, travelling towards the operator.
    tbar = {"x": X_TBAR, "y": Y_DEST_LABEL, "w": TBAR_W,
            "h": Y_LAYER + DOME_H - Y_DEST_LABEL,
            "min": P.TBAR_MIN, "max": P.TBAR_MAX}

    scale = {"y": Y_SCALE, "x": X0, "pitch": COL, "w": KEY_W,
             "labels": [str(i + 1) for i in range(12)]}

    return {
        "width": WIDTH, "height": HEIGHT,
        "cell": {"w": P.CELL_W, "h": P.CELL_H, "scale": LCD_SCALE,
                 "pw": LCD_W, "ph": LCD_H, "inset": CAP_INSET},
        "keys": keys, "displays": displays, "groups": groups,
        "tbar": tbar, "scale_strip": scale,
        "counts": {"buttons": len(P.BUTTON_INFO), "cells": P.NUM_CELLS},
    }


LAYOUT = build()
