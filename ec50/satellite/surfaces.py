"""How the EC-50's hardware maps onto Companion surfaces.

Four surfaces over one connection. The three Assign rows are registered
separately because a Companion surface sits on exactly one page, and
independent pages per row is the whole point.

Between them they account for every one of the 82 buttons and all 45 displays,
exactly once. `python -m ec50 satellite --check` asserts that.

The row page arrows are deliberately NOT Companion controls. Companion's
CAN_CHANGE_PAGE / CHANGE-PAGE pair lets a surface navigate its own pages, which
is precisely what those keys are for physically - and it keeps two columns free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import protocol as P

# Style presets. Controls with no display ask for nothing but colour, so
# Companion never spends time encoding bitmaps we would throw away.
def style_presets(bitmaps: bool = False) -> dict:
    """Controls with no display ask for colour only, so Companion never spends
    time encoding pixels we would discard.

    Bitmaps are off by default. Requesting them costs about 8 KB per key - some
    360 KB for a full 45-cell refresh - and the built-in font renders better on
    a 2:1 monochrome cell than a downscaled square anyway. `--bitmaps` turns on
    the fallback for buttons that carry artwork instead of text.
    """
    lcd = {"colors": "hex", "text": True, "textStyle": True}
    if bitmaps:
        lcd["bitmap"] = {"w": P.CELL_W, "h": P.CELL_H}
    return {"default": {"colors": "hex", "text": True}, "lcd": lcd}

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

    def manifest(self, bitmaps: bool = False, columns: int = 8) -> dict:
        return {
            "stylePresets": style_presets(bitmaps),
            "controls": {
                c.id: {"row": r, "column": col,
                       **({"stylePreset": "lcd"} if c.has_display else {})}
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


def _control_surface() -> Surface:
    controls: list[Control] = []

    # Row 0 - destinations, their page arrows, and the page display beside them.
    for col in range(12):
        controls.append(Control(f"dest/{col}", 0, col, button=_idx(f"DEST_{col}")))
    controls.append(Control("dest/up", 0, 12, button=_idx("DEST_UP")))
    controls.append(Control("dest/down", 0, 13, button=_idx("DEST_DOWN")))
    # Cells 9 and 10 sit beside the page arrows, not on a key. Barco's CSV files
    # them under DEST_11 and LAYER_8, which is an internal association rather
    # than a physical one, so they are display-only here.
    controls.append(Control("dest/page", 0, 14, cell=9))

    # Row 1 - layers, their page arrows, and the freeze/arm group.
    for col in range(9):
        controls.append(Control(f"layer/{col}", 1, col, button=_idx(f"LAYER_{col}")))
    controls.append(Control("layer/up", 1, 9, button=_idx("LAYER_UP")))
    controls.append(Control("layer/down", 1, 10, button=_idx("LAYER_DOWN")))
    controls.append(Control("layer/page", 1, 11, cell=10))
    controls.append(Control("freeze/pgm", 1, 12, button=_idx("FREEZE_PGM")))
    controls.append(Control("freeze/pvw", 1, 13, button=_idx("FREEZE_PVW")))
    controls.append(Control("arm", 1, 14, button=_idx("ARM")))

    # Row 2 - transition group and the four show-config keys, which do have LCDs.
    row2 = [
        ("match", "MATCH"), ("trans/layer", "LAYER_TRANS"), ("cut/layer", "LAYER_CUT"),
        ("cut", "CUT"), ("trans/all", "ALL_TRANS"),
    ]
    for col, (cid, name) in enumerate(row2):
        controls.append(Control(cid, 2, col, button=_idx(name)))
    for i, name in enumerate(("SHOW_CFG_0_0", "SHOW_CFG_0_1",
                              "SHOW_CFG_1_0", "SHOW_CFG_1_1")):
        cid = "cfg/" + name[len("SHOW_CFG_"):].replace("_", "-")
        controls.append(Control(cid, 2, 5 + i, button=_idx(name), cell=_cell(name)))

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
    return [_assign_row(0), _assign_row(1), _assign_row(2), _control_surface()]


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
