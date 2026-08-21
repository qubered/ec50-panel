# ec50-panel

Driver for the Barco EC-50 Event Controller's USB control surface, with no
Barco software required. Windows, Linux and macOS.

The surface gives you **36 Assign keys** in a 3×12 grid — each with its own
64×32 display and a red/green LED — **82 buttons** in total, and a **16-bit
T-bar**. Enough to drive it as a Bitfocus Companion Satellite surface, a
custom control panel, or anything else.

The protocol was reverse engineered from USB captures of Barco's Event Master
Toolset and confirmed on hardware. See [docs/PROTOCOL.md](docs/PROTOCOL.md) for
the full specification.

## Install

```bash
pip install ftd2xx      # Windows
pip install pyftdi      # Linux and macOS
```

On Linux, install the udev rule so libusb can reach the device, then re-plug:

```bash
sudo cp udev/71-ec50.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Try it

Close the Event Master Toolset first — the panel accepts one host at a time.

```bash
python -m ec50 info      # which backend, what it found
python -m ec50 grid      # label every key R1C1 .. R3C12
python -m ec50 test      # press a key, it lights up
python -m ec50 watch     # key events and T-bar
python -m ec50 vegas     # colour and LED light show
python -m ec50 image pic.png --preview   # dither a picture, no panel needed
python -m ec50 image pic.png             # ... and put it on the Assign grid
```

Add `--init` after a power cycle. Add `--backend d2xx|pyftdi` to force one.

## Use it

```python
from ec50 import EC50, Colour, Led

with EC50.open() as panel:
    panel.clear()
    panel.text(0, 0, "CAM 1", Colour.GREEN)
    panel.led_at(0, 0, Led.GREEN)
    panel.flush()

    for ev in panel.events():
        print(ev, panel.tbar)
```

Output is buffered — `set_*` calls mutate a local framebuffer and nothing
reaches the panel until `flush()`, which handles both commit paths. Input is a
queue: each `poll()` drains the key FIFO, so events are already edges and never
need debouncing.

### API

| | |
|---|---|
| `EC50.open(backend=None, index=None, skew=None)` | open the panel |
| `panel.clear()` | blank every display, darken every backlight, LEDs off |
| `panel.text(row, col, s, colour)` | label an Assign key, rows 0–2, cols 0–11 |
| `panel.set_cell_text(cell, s, colour)` | label any of the 45 cells |
| `panel.set_bitmap(cell, data)` | raw 256-byte 64×32 1bpp image |
| `image.load(path)` → `(w, h, luma)` | read a PNG or binary PGM/PPM |
| `image.to_cell(luma, w, h, ...)` | dither a picture to a 256-byte cell |
| `panel.set_colour(cell, value)` | backlight colour |
| `panel.led_at(row, col, state)` / `set_led(index, state)` | key LED |
| `panel.flush()` | push and latch |
| `panel.poll()` → `[Event]` | drain key events, refresh T-bar |
| `panel.events()` | blocking generator of events |
| `panel.tbar` / `tbar_raw` / `held` | 0.0–1.0, raw 16-bit, held key set |

Colours are `Colour.RED/GREEN/BLUE/YELLOW/CYAN/MAGENTA/ORANGE/PINK/WHITE/DIM/OFF`,
or `colour(r, g, b)` with each channel 0–3 — 64 in total. LEDs are
`Led.OFF/RED/GREEN`; there is no amber.

Text is arbitrary UTF-8. The variable-width font covers printable ASCII, plus
degree, currency and arrows, plus **24 monochrome icons** for the emoji
Companion buttons actually use — play, pause, stop, record, check, cross, star,
heart, warning, speaker, mute, lock, clock, bulb, mic, camera, fire, thumbs-up.

Nothing is ever dropped. Accents are stripped (`CAMÉRA` → `CAMERA`), around 90
emoji fold onto an icon, and anything left becomes a small filled square:

```
"🔴 REC"  → "● REC"          "✅ CAM 1" → "✓ CAM 1"
"🟢🔴🔵"   → "●●●"             "🦄"       → "▪"
```

The coloured discs all fold together because on a 1-bit display that is all any
of them can be — the *backlight* carries colour, not the pixels. Pair them:
`panel.text(0, 0, "● REC", Colour.RED)`.

`font.render("text")` ASCII-arts a string so you can check it without hardware.

## Things that will bite you

Documented properly in [docs/PROTOCOL.md](docs/PROTOCOL.md), but the short list:

- **Nothing displays until it is latched.** `0x3828` is a mask — bit 0 commits
  LEDs, bit 1 commits the framebuffer. Unlatched writes are accepted and
  silently ignored.
- **Bit 0 of each byte is the leftmost pixel**, not bit 7.
- **The display's right half is skewed one row.** Compensated here; disable
  with `EC50.open(skew=0)`.
- **Never run Zadig on this device.** On Windows the panel uses Barco's own
  FTDI driver binding; replacing it stops the Toolset seeing the panel.
- **The panel's CPLD is field-programmable over the same link.** Nothing here
  goes near that path, and nothing should.

## Pictures

The cells are one bit deep, so a picture has to be reduced to ink or nothing.
`ec50.image` does it in four steps — luma, box-filtered resample, sharpen, then
a threshold. Two of those choices are made per picture, because there is no
single right answer.

**Which threshold.** Flat artwork — a logo, an icon, a screen of text — wants
one hard cut, placed by Otsu's method: flat areas stay flat and shapes stay
crisp. A photograph wants a threshold that varies across the picture, so detail
survives at both ends of the range. `--dither auto` (the default) reads the
histogram and picks between them: it measures how cleanly a single cut would
separate the tones, and how many grey levels the picture actually occupies. A
logo scores two tones and near-perfect separation; a photograph spreads across
most of the range. The `atkinson`, `floyd` and `bayer` modes keep grey as
texture, which suits continuous tone and wrecks anything flat.

**Which way round.** A set pixel is dark on a lit backlight. Companion draws its
buttons light-on-dark, so most artwork wants inverting — white text becomes dark
text on a lit key, matching the font. A photograph does not, and inverting one
turns it into a negative. `--polarity auto` (the default) takes whichever way
leaves less ink, so the majority tone stays lit.

Two details do the rest. The picture is scaled on its own and only then dropped
into the cell, so letterbox padding never reaches the threshold: mix the two and
a local threshold sees bright padding beside the picture's edge, decides the edge
is dark by comparison, and draws a hard line down the join. And the unsharp mask
runs before the threshold, because box-filtering a 72 × 72 button down to a
64 × 32 cell averages a one-pixel stroke into mid grey that the cut then drops.

```bash
python -m ec50 image logo.png                      # across the whole Assign grid
python -m ec50 image logo.png --cell 5             # into one cell
python -m ec50 image logo.png --preview            # to the terminal instead
python -m ec50 image photo.png --fit cover --levels
```

`--dither auto|otsu|adaptive|atkinson|floyd|bayer|none`,
`--polarity auto|dark|light`, `--fit contain|cover|stretch`, `--levels` to
stretch contrast first, `--sharpen N` to override the unsharp amount.

`--fit cover` is worth trying on a portrait: the cell is 2:1 and a square photo
letterboxed into it only uses half the width.

Spanning the grid thresholds one frame and then cuts it into cells, rather than
doing each cell separately — otherwise every cell picks its own threshold and
the joins show as a grid of hard edges.

Reading a PNG needs nothing but the standard library; Pillow is a heavy
dependency for turning one logo into 2048 dots.

## Companion

```bash
python -m ec50 satellite --host <companion-ip>
```

Registers four surfaces — the three Assign rows separately so Companion can page
them independently, plus a Control surface for destinations, layers and
transport. Button text renders on the LCDs, presses reach Companion, the row
page arrows change that row's page, and the T-bar arrives as a Companion
variable.

The panel is wider than a Companion page, so controls wrap onto the page grid —
`--columns 8` (the default) puts an Assign row's first 8 keys on one grid row
and the remaining 4 plus its label on the next. Match it to Settings → Grid
size.

See [docs/COMPANION.md](docs/COMPANION.md) for the mapping and setup.

### Key LEDs

72 keys have a lamp that does red, green or off.

`--leds auto` (the default) uses the button's background colour for keys with
no display — they have nowhere else to show it — and the pressed state for keys
that do.

`--leds text` uses the button's **text** colour on every key instead. The panel
renders text as ink and ignores its colour, so that is a per-key colour channel
going spare: put a feedback on the text colour and it drives the lamp and
nothing else. Black reads as off, so make black the resting state. This is the
only way to drive the lamp of a key that already has an LCD, whose background is
committed to the backlight.

`--leds gauge` asks for a Gauge style layer's colour. **No Companion release
supports this** — the field exists only on `main`, and in 5.0.x the "Gauge" in
the style editor is a graphics element drawn onto the button image, not
something reported to satellite. The rejection is detected and retried without,
so trying it costs nothing.

Companion's green "actions running" triangle is not sent over satellite. For
that exact behaviour, add a Gauge layer to the button and drive it from a
feedback on `b_actions_running_<page>_<row>_<column>`.

## Status

Working and confirmed on hardware: displays, colour, LEDs, all 82 buttons,
T-bar, on Windows via D2XX. The Linux and macOS transport is implemented but
has not yet been run against a panel.

Two things remain undecoded, neither blocking: a second 45×4 colour table in
the framebuffer that Barco's own software never writes, and whether the
framebuffer accepts an address other than `0x0000` for partial updates.

## Licence

MIT — see [LICENSE](LICENSE).

The protocol was reverse engineered for interoperability. This repository
contains no Barco software, files or firmware; only our own code and factual
descriptions of how the hardware behaves. It is not affiliated with or endorsed
by Barco.
