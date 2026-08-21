# The EC-series family

Notes toward supporting the EC-30 and EC-40, gathered without either panel in
hand. Everything here is derived from Barco's own installed files and the
Event Master manual (R5905948 /12) — nothing is guessed, and the two things
that **are** still unknown are called out at the bottom.

Short version: the protocol looks identical across the three consoles, and the
differences are all "this model does not populate that button". Supporting the
others should mean new tables, not new protocol.

## The models

| | Buttons | Displays | Assign rows | Show Config keys |
|---|---|---|---|---|
| EC-30 | 65 | 30 | 2 | 2 (`SHOW_CFG_1_0`, `SHOW_CFG_1_1`) |
| EC-40 | 78 | 41 | 3 | none |
| EC-50 | 82 | 45 | 3 | 4 |

The **EC-40** does not appear in the manual at all — it exists only in the
toolset's data files and string tables. Treat its details as less certain than
the other two.

## Why the protocol is almost certainly shared

**One console USB id.** `drivers/ftdibus.inf` binds eleven `VID_0600` PIDs, and
exactly one of them is a console:

| PID | Device |
|---|---|
| 0300 / 0301 | System Board / S3 Mother Board |
| 0310 / 0315 | DVI Input / Output Board |
| 0320 / 0325 | SDI Input / Output Board |
| 0330 / 0335 | HDMI-DP Input / Output Board |
| **0336** | **Show Console Board** |
| 0340 / 0345 | VPU Board / Link Board |

Everything else is a video-processor board. So all three consoles present as
`0600:0336 "Show Console Board"`, and `open_transport()` should find an EC-30
with no change at all.

**One button map for all three.** `bin/showConsoleMap.csv` is a single file
covering every console, with a trailing column:

```
console type(0=all, 1=ec50, 2=ec30&ec50, 3=ec40&ec50)
```

Every row carries the same button index, LED register, byte shift, position
shift and LCD index regardless of model. A model does not renumber anything —
it just omits rows. That is strong evidence the register protocol, the LED
banks and the framebuffer cell numbering are common to all three.

**One firmware.** `firmware/firmware_manifest.ini` has a single `[SHOW_BOARD]`
entry (`EC_kbd_20150929_1017.jam`), not one per console.

## What each model drops

**EC-30** loses Assign row 0 and the upper pair of Show Config keys — 17
buttons, 15 displays:

```
ASSIGN_0_0 … ASSIGN_0_11, ASSIGN_0_UP, ASSIGN_0_DOWN, ASSIGN_0_LABEL
SHOW_CFG_0_0, SHOW_CFG_0_1
```

Displays absent: cells `0 1 2 11 12 13 18 19 20 27 28 29 36 37 38`.

**EC-40** keeps all three Assign rows but has no Show Config keys at all — 4
buttons, 4 displays (`SHOW_CFG_0_0`, `SHOW_CFG_0_1`, `SHOW_CFG_1_0`,
`SHOW_CFG_1_1`; cells 12, 13, 15, 16).

Note the consequence for `surfaces.py`: an EC-30 wants **five** Companion
surfaces (two Assign rows, Destinations, Layers, Control) and an EC-40 wants
six with an emptier Control surface.

## Physical layout differences

Destinations, Layers, the T-bar and the Cut / All Trans pair are laid out
identically on the EC-30 and EC-50. Two things are not.

**The Show Config block.** Four keys in a 2 × 2 on the EC-50; two side by side
on the EC-30.

**The coloured pairs are swapped.** Verified by rendering both manual figures
at 1200 dpi — the label line counts settle it even where the text is soft
(EC-50's green top label is two lines, the EC-30's is one):

| Position | EC-50 | EC-30 |
|---|---|---|
| left (blue) | Freeze PGM / Freeze PVW | Freeze PGM / Freeze PVW |
| middle (green) | **Layer Trans / Layer Cut** | **Arm / Match PGM** |
| right (red) | **Arm PVW / Match PGM** | **Layer Trans / Layer Cut** |

The button indices and LED registers are identical, so this changes nothing in
the driver — a press of `LAYER_TRANS` is `LAYER_TRANS` on both. It only matters
for anything that draws the panel, such as `docs/` diagrams and the Companion
map artifact.

## What the code would need

* `protocol.py` — `ASSIGN` / `ASSIGN_INDEX` for two rows, and a `BUTTON_INFO`
  subset. Best driven from the console-type column rather than hand-copied, so
  EC-40 falls out for free.
* `satellite/surfaces.py` — build only the Assign rows and config keys the
  model has. `check()` needs the expected button and cell counts to come from
  the model rather than being hardcoded to 82 and 45.
* Nothing else. Transport, MPSSE framing, the SPI register protocol, the key
  FIFO, the T-bar, the LED banks, the font and the image pipeline are all
  model-independent as far as this evidence goes.

## Open questions — these need hardware

**1. Is the framebuffer still 11,962 bytes?**

The EC-50 writes 45 cells of 256 bytes plus headers and the colour tables. An
EC-30 has 30 populated cells. Same board and shared cell numbering suggest the
payload is unchanged with 15 cells simply going nowhere, but a short write to a
panel expecting a long one is exactly the failure that looks like "nothing
happened" — this cost a day on the EC-50. Confirm before assuming.

**2. How does the toolset know which console it is?**

`bin/EventMaster.exe` contains the enum

```
SHOW_CONSOLE_TYPE_EC30
SHOW_CONSOLE_TYPE_EC40
SHOW_CONSOLE_TYPE_EC50
SHOW_CONSOLE_TYPE_UNKNOWN
```

with `Controller::getShowConsoleType()` and
`ControllerHandler::getConsoleIdFromMap(BaseController*)`. So it asks the panel
rather than the user, presumably via a register this project has not identified.
Candidates: an unread register near the init block (`0x3920`–`0x3938`), or
something in the FTDI EEPROM description. Until it is found, a `--console
ec30|ec40|ec50` flag defaulting to EC-50 is the honest fallback.

## Verification checklist, when a panel turns up

1. `python -m ec50 info` — confirm it enumerates as `0600:0336` and note the
   serial and description strings. Do they differ from the EC-50's
   `"Show Console Board"` / `"EC Show Board"`?
2. Capture the toolset's opening exchange (USBPcap, as in `docs/PROTOCOL.md`)
   and diff the register writes against the EC-50's. Any read the EC-50 does
   not do is the model-detection candidate.
3. Check the length of the big framebuffer write. 11,962 answers question 1.
4. `python -m ec50 watch` — press every key and confirm the indices match
   `showConsoleMap.csv`, in particular that the surviving Assign rows report as
   `ASSIGN_1_*` and `ASSIGN_2_*` rather than being renumbered from zero.
5. `python -m ec50 grid` — see which of the 45 cells light up. That confirms
   the absent-cell list above and whether writing to a missing cell is
   harmless.

## Sources

* `showConsoleMap.csv`, `ftdibus.inf`, `firmware_manifest.ini` and
  `EventMaster.exe` from an Event Master Toolset Rev 9.2 (Build 68800) install
  — `bin/`, `drivers/` and `firmware/` under the install root.
* Event Master manual R5905948 /12: §3.3 (feature comparison), §10.2 (EC-30
  front panel), §10.4 (EC-50 front panel).
