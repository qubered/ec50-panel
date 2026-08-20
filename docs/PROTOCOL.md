# Barco EC-50 control surface protocol

Reverse engineered from USB captures of Barco's Event Master Toolset, then
confirmed on hardware. Every claim below was verified by driving a real EC-50
with no Barco software running.

## Device

```
USB\VID_0600&PID_0336        "Show Console Board", manufacturer Barco
Class FF / SubClass FF / Prot FF     vendor specific, not HID
Bulk endpoints  0x02 OUT, 0x81 IN
```

The chip is an **FT232H** in MPSSE mode. Barco reprograms the FTDI EEPROM, so
the stock `0403:6014` becomes `0600:0336` — their driver INF leaves the original
line commented out directly above their own list. The EEPROM product string is
`EC Show Board`; `Show Console Board` is the Windows PnP description.

The EC-50 contains an internal hub. Alongside this board sits a separate
composite HID device carrying the touchscreen — unrelated to the control
surface, and untouched by this driver.

### Drivers

| Platform | Driver | Notes |
|---|---|---|
| Windows | FTDI CDM, `ftdibus.sys` + D2XX | Installed by the Toolset with a re-badged INF (`Provider="Barco"`). **Never run Zadig on this device** — replacing the driver stops the Toolset seeing the panel. |
| Linux | libusb | VID `0x0600` is absent from `ftdi_sio`'s table, so no kernel driver claims it. Needs a udev rule only. |
| macOS | libusb | May require unloading `AppleUSBFTDI`. |

Barco's own build reflects this split: `libipconsole/src/FtdiIoWin.cpp` on
Windows, `FtdiIoLinux.cpp` on Linux.

## Transport

MPSSE at 15 MHz, chip select on **ADBUS3, active low**. Setup, taken verbatim
from captures:

```
8A 97 8D 86 01 00 85      div-by-5 off, adaptive off, 3-phase off, divisor 1, loopback off
80 18 1B                  ADBUS value 0x18, direction 0x1B
9E 00 04  82 04 04        open-drain, ACBUS
```

Every transaction is wrapped:

```
80 90 1B  x5              CS low (repeated as a setup delay)
11 <len-1 lo> <hi>        clock bytes out            (writes)
31 <len-1 lo> <hi>        clock out and in           (reads)
20 <len-1 lo> <hi>        clock in                   (reads)
80 98 1B  x5              CS high
```

### Register format

```
<addr_hi> <addr_lo> <flags> <len-1>  [data ...]  AA BB
```

`flags` bit 7 set means read. Data is little-endian. Reads return the value in
the tail of the reply.

```
39 38 00 03  3F 00 00 00  AA BB      write 0x0000003F to 0x3938
38 10 80 03  AA BB                   read 4 bytes from 0x3810
```

> **Replies lag by one transaction.** The first read after switching address
> returns the *previous* register's data. Either poll a single address, or
> prime each read with a throwaway.

## Committing — `0x3828`

Nothing takes effect when written. `0x3828` is a **latch mask**:

| Write | Effect |
|---|---|
| `0x3828 = 1` | latch the LED registers |
| `0x3828 = 2` | latch the framebuffer |

Writes without the latch are accepted and silently ignored. This is the single
easiest mistake to make with this panel.

The display is double buffered, so the Toolset writes the framebuffer and
commits **twice** — one pass updates only one bank.

## Framebuffer — address `0x0000`

One transaction of **11,962 bytes**:

| Offset | Length | Contents |
|---|---|---|
| 0 | 4 | header: address `0x0000` + length field |
| 0 | 11,520 | 45 cells × 256 bytes |
| 11,520 | 4 | padding, zero |
| 11,524 | 180 | colour table, 45 × 4 bytes |
| 11,704 | 76 | zero |
| 11,780 | 180 | second 45 × 4 table, uniformly `0xFF`, purpose unknown |
| 11,960 | 2 | `AA BB` terminator |

> The 4-byte header **overlays cell 0's first scanline**. Zeroing it makes the
> panel discard the entire write.

### Cells

Cell *N* begins at *N* × 256, where *N* is the `lcd index` column of Barco's
`showConsoleMap.csv`. Each is **64 × 32 pixels, 1 bit per pixel**, row-major,
8 bytes per scanline.

> **Bit 0 of each byte is the leftmost pixel**, not bit 7.

The pixels are monochrome; colour comes from the backlight, one value per cell.
So an emoji can only ever render as a monochrome icon, and every coloured
variant of a shape collapses to the same glyph.

> **The right half is skewed.** Content at x ≥ 32 must be stored **one row
> higher** than content on the left — two 32px driver chips with a one-row
> addressing offset. Barco compensates in software; without it, text straddling
> x = 32 shows a visible 1px step.

### Colour

Byte 3 of each cell's entry in the table at 11,524. Layout `RRGGBBII`, two bits
per channel. The low two bits must both be set or the panel ignores the colour
and shows white. 64 colours.

| Byte | R G B | Result |
|---|---|---|
| `0x03` | 0 0 0 | off |
| `0x57` | 1 1 1 | dim white — the Toolset's blank-button value |
| `0xFF` | 3 3 3 | bright white — its labelled-button value |
| `0xC3` | 3 0 0 | red |
| `0x33` | 0 3 0 | green |
| `0x0F` | 0 0 3 | blue |
| `0xF3` | 3 3 0 | yellow |
| `0xD3` | 3 1 0 | orange |

## Key LEDs — `0x3950`–`0x3964`

Six registers, **two bits per button**, positioned at `byte shift + position
shift` from `showConsoleMap.csv`. Latch with `0x3828 = 1`.

| Code | LED |
|---|---|
| 0 | off |
| 1 | red |
| 2 | green |
| 3 | off — unused, there is no amber |

> **Row 0's bits run backwards.** In the Assign grid, row 0 descends within each
> byte (30, 28, 26, 24) while rows 1 and 2 ascend (24, 26, 28, 30). Generating
> these with a loop silently mirrors row 0 in groups of four.

## Input — read `0x3810`

One read returns keys and the T-bar together.

### Keys — byte 6

An **event FIFO**. Reading pops one event.

| Value | Meaning |
|---|---|
| `0xFF` | queue empty |
| `0x80 \| index` | key **down** |
| `index` | key **up** |

`index` is the `button index` column of `showConsoleMap.csv` — 82 buttons, of
which 45 have a display and 72 have an LED. Simultaneous presses queue and drain
in order; nothing is lost. Because each read is already an edge, a driver never
needs to debounce — but it must not skip polls.

### T-bar — bytes 5:4

A **16-bit ADC**, byte 5 high, byte 4 low. Full travel measured across two runs:
`0x0040` to `0xFC10`, about 64,500 counts — essentially the whole 16-bit range,
so no per-unit calibration is needed.

> Byte 4 alone is misleading. At rest it dithers ±1 and during motion it looks
> random; both are just what a low byte does. Read alone it appears to span
> `0x45`–`0xDC`, which is the average of a value cycling through its full range.

## LCD controller setup

Issued once by the Toolset at startup. The panel retains it across a host
disconnect, so it is only needed after a power cycle.

```
0x3938 = 0x0000003F      enable six LCD controllers
0x3920 = 0x00760131
0x3924 = 0x00760131
0x3928 = 0x00760131
0x392C = 0x00180131
0x3930 = 0x00AF0131
0x3934 = 0x00AF0131
```

## Related boards

The same driver INF binds twelve Barco PIDs, so an E2 or S3 exposes its internal
boards over the same mechanism:

| PID | Board | PID | Board |
|---|---|---|---|
| `0300` | System | `0330` | HDMI-DP Input |
| `0301` | S3 Mother | `0335` | HDMI-DP Output |
| `0310` | DVI Input | `0336` | **Show Console** |
| `0315` | DVI Output | `0340` | VPU |
| `0320` | SDI Input | `0345` | Link |
| `0325` | SDI Output | | |

## Still unknown

- The **second colour table** at 11,780. Uniformly `0xFF`; the Toolset never
  wrote it in any capture.
- What the `II` bits do beyond gating colour on.
- Whether the framebuffer address field accepts values other than `0x0000`,
  which would allow partial updates instead of a full 11,962-byte push.

## Caution

The panel's CPLD is field-programmable over this same link — the Toolset ships
an Altera JAM/STAPL file (`EC_kbd_*.jam`) and a player. Nothing in this driver
goes near that path, and nothing here should. Keep a copy of that file; it is
the recovery image.
