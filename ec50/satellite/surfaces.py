"""How the EC-50's hardware maps onto Companion surfaces.

Six surfaces over one connection. A Companion surface sits on exactly one page,
so every band of the panel that has its own page arrows has to be its own
surface: the three Assign rows, Destinations, and Layers. What is left - the
transition group, the Show Config keys and the T-bar - has no arrows and shares
the last one.

Between them they account for every one of the 82 buttons and all 45 displays,
exactly once. `python -m ec50 satellite --check` asserts that.

The row page arrows are deliberately NOT Companion controls. Companion's
CAN_CHANGE_PAGE / CHANGE-PAGE pair lets a surface navigate its own pages, which
is precisely what those keys are for physically - and it keeps two columns free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import protocol as P

# Style presets. A control asks for what it can actually show: no bitmap for a
# key with no display, no LED colours for a key with no lamp. Companion encodes
# only what is asked for, so the difference is real work saved on every draw.
def style_presets(bitmaps: bool = False, leds: bool = True) -> dict:
    """The four combinations of display and LED that this panel has.

    Bitmaps are off by default. Requesting them costs about 8 KB per key - some
    360 KB for a full 45-cell refresh - and the built-in font renders better on
    a 2:1 monochrome cell than a downscaled square anyway. `--bitmaps` turns on
    the fallback for buttons that carry artwork instead of text.

    `leds` asks Companion for the colour of a style layer whose usage is set to
    Gauge, which is how a feedback reaches a lamp that has no display to write
    on. One segment, because the panel has one lamp per key rather than a ring.
    """
    lamp = {"segments": 1, "mode": "simple"} if leds else None
    lcd = {"colors": "hex", "text": True, "textStyle": True}
    if bitmaps:
        lcd["bitmap"] = {"w": P.CELL_W, "h": P.CELL_H}
    plain = {"colors": "hex", "text": True}
    if lamp is None:
        # Older Companions have no `leds` in their manifest schema. Keep the
        # preset names so the control map does not have to change with them.
        return {"default": plain, "led": plain, "lcd": lcd, "lcd-led": lcd}
    return {
        "default": plain,
        "led": {**plain, "leds": lamp},
        "lcd": lcd,
        "lcd-led": {**lcd, "leds": lamp},
    }


_NAME_TO_INDEX = {info[0]: idx for idx, info in P.BUTTON_INFO.items()}


def _idx(name: str) -> int:
    return _NAME_TO_INDEX[name]


def _cell(name: str) -> int:
    return P.BUTTON_INFO[_idx(name)][1]


@dataclass
class Control:
    """One entry in the surface's LAYOUT_MANIFEST."""
    id: str
    row: int
    column: int
    button: int | None = None      # panel button index, None if display-only
    cell: int | None = None        # LCD cell, None if the control has no display

    @property
    def has_display(self) -> bool:
        return self.cell is not None

    @property
    def has_led(self) -> bool:
        """72 of the 82 keys have a lamp; the ten page arrows do not."""
        entry = P.BUTTON_INFO.get(self.button)
        return bool(entry) and entry[2] != 0xFFFF

    @property
    def preset(self) -> str:
        if self.has_display:
            return "lcd-led" if self.has_led else "lcd"
        return "led" if self.has_led else "default"


@dataclass
class Surface:
    key: str                       # short suffix used in the device id
    name: str                      # PRODUCT_NAME shown in Companion
    controls: list[Control]
    page_up: int | None = None     # panel button that pages backward
    page_down: int | None = None   # ... and forward
    variables: list[dict] = field(default_factory=list)

    by_id: dict[str, Control] = field(init=False, default_factory=dict)
    by_button: dict[int, Control] = field(init=False, default_factory=dict)

    def __post_init__(self):
        self.by_id = {c.id: c for c in self.controls}
        self.by_button = {c.button: c for c in self.controls if c.button is not None}

    @property
    def can_change_page(self) -> bool:
        return self.page_up is not None or self.page_down is not None

    def wrap(self, columns: int) -> list[tuple[Control, int, int]]:
        """Lay the controls out `columns` wide, in declaration order.

        The panel's rows are wider than any Companion page grid - 12 Assign keys
        against a default of 8 columns - so a surface declared at its physical
        width would run off the page. Wrapping keeps every control reachable on
        a normal grid: with 8 columns an Assign row fills the top row and spills
        the last four keys plus its label onto the next.

        Companion does not expose the page grid size over the satellite protocol
        or the HTTP API, so the width has to be supplied.
        """
        if columns <= 0:
            return [(c, c.row, c.column) for c in self.controls]
        out, row, col = [], 0, 0
        last_logical = self.controls[0].row if self.controls else 0
        for c in self.controls:
            # Start a fresh grid row wherever the panel starts a new physical
            # row, so the Control surface keeps its destination / layer /
            # transport groups visually separate.
            if c.row != last_logical:
                last_logical = c.row
                if col:
                    row, col = row + 1, 0
            out.append((c, row, col))
            col += 1
            if col >= columns:
                row, col = row + 1, 0
        return out

    def shape(self, columns: int) -> tuple[int, int]:
        placed = self.wrap(columns)
        return (max(r for _, r, _ in placed) + 1,
                max(c for _, _, c in placed) + 1)

    def manifest(self, bitmaps: bool = False, columns: int = 8,
                 leds: bool = True) -> dict:
        return {
            "stylePresets": style_presets(bitmaps, leds),
            "controls": {
                c.id: {"row": r, "column": col,
                       **({} if c.preset == "default" else {"stylePreset": c.preset})}
                for c, r, col in self.wrap(columns)
            },
        }


def _assign_row(row: int) -> Surface:
    controls = [
        Control(f"key/{col}", 0, col,
                button=P.ASSIGN_INDEX[row][col], cell=P.ASSIGN[row][col])
        for col in range(12)
    ]
    # The mode key beside each row. Barco prints "Preset" and the page number
    # here; put a Companion page-number button on it and it does the same.
    controls.append(Control("label", 0, 12,
                            button=_idx(f"ASSIGN_{row}_LABEL"),
                            cell=_cell(f"ASSIGN_{row}_LABEL")))
    return Surface(
        key=f"row{row + 1}",
        name=f"EC-50 Assign Row {row + 1}",
        controls=controls,
        page_up=_idx(f"ASSIGN_{row}_UP"),
        page_down=_idx(f"ASSIGN_{row}_DOWN"),
    )


def _destinations() -> Surface:
    """The 12 Destination keys. Its arrows page it, like an Assign row."""
    controls = [Control(f"dest/{col}", 0, col, button=_idx(f"DEST_{col}"))
                for col in range(12)]
    # Cell 9 sits beside the page arrows, not on a key. Barco files it under
    # DEST_11, which is an internal association rather than a physical one, so
    # it is display-only here - put a page-number button on it and it says what
    # Barco's own software would.
    controls.append(Control("dest/page", 0, 12, cell=9))
    return Surface(
        key="dest",
        name="EC-50 Destinations",
        controls=controls,
        page_up=_idx("DEST_UP"),
        page_down=_idx("DEST_DOWN"),
    )


def _layers() -> Surface:
    """BG plus the eight Layer keys, paged by its own arrows."""
    controls = [Control(f"layer/{col}", 0, col, button=_idx(f"LAYER_{col}"))
                for col in range(9)]
    controls.append(Control("layer/page", 0, 9, cell=10))
    return Surface(
        key="layer",
        name="EC-50 Layers",
        controls=controls,
        page_up=_idx("LAYER_UP"),
        page_down=_idx("LAYER_DOWN"),
    )


def _control_surface() -> Surface:
    """Everything that is not a paged row: the transition group and the four
    Show Config keys, plus the T-bar.

    Grouped by the physical clusters they form on the panel, and `wrap` starts
    a fresh grid row at each one, so they stay separate on a Companion page.
    """
    groups = [
        # The coloured pairs beside the Layer row.
        [("freeze/pgm", "FREEZE_PGM"), ("freeze/pvw", "FREEZE_PVW"),
         ("trans/layer", "LAYER_TRANS"), ("cut/layer", "LAYER_CUT"),
         ("arm", "ARM"), ("match", "MATCH")],
        # The red pair under the Show Config block.
        [("cut", "CUT"), ("trans/all", "ALL_TRANS")],
    ]
    controls: list[Control] = []
    for row, group in enumerate(groups):
        for col, (cid, name) in enumerate(group):
            controls.append(Control(cid, row, col, button=_idx(name)))
    # The Show Config keys, which do have displays.
    for i, name in enumerate(("SHOW_CFG_0_0", "SHOW_CFG_0_1",
                              "SHOW_CFG_1_0", "SHOW_CFG_1_1")):
        cid = "cfg/" + name[len("SHOW_CFG_"):].replace("_", "-")
        controls.append(Control(cid, 2, i, button=_idx(name), cell=_cell(name)))

    return Surface(
        key="control",
        name="EC-50 Control",
        controls=controls,
        variables=[{
            "id": "tbar",
            "type": "input",
            "name": "T-bar",
            "description": "Position of the EC-50's T-bar, 0-100",
        }],
    )


def build() -> list[Surface]:
    return [_assign_row(0), _assign_row(1), _assign_row(2),
            _destinations(), _layers(), _control_surface()]


def check(surfaces: list[Surface]) -> list[str]:
    """Assert the mapping covers the hardware exactly once. Returns problems."""
    problems = []
    buttons, cells = [], []
    for s in surfaces:
        for c in s.controls:
            if c.button is not None:
                buttons.append(c.button)
            if c.cell is not None:
                cells.append(c.cell)
        for nav in (s.page_up, s.page_down):
            if nav is not None:
                buttons.append(nav)

    for label, used, total in (("button", buttons, set(P.BUTTON_INFO)),
                               ("cell", cells, set(range(P.NUM_CELLS)))):
        missing = sorted(total - set(used))
        dupes = sorted({v for v in used if used.count(v) > 1})
        if missing:
            problems.append(f"unmapped {label}s: {missing}")
        if dupes:
            problems.append(f"duplicated {label}s: {dupes}")
    return problems
