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

## No hardware? Run the emulator

A software EC-50 with the front panel in a browser — same buttons, same 45
displays, same LEDs, same T-bar — answering the same wire protocol, so the
driver cannot tell the difference.

```bash
python -m ec50 emulate                                  # http://127.0.0.1:8050/
python -m ec50 grid --controller 127.0.0.1:16650        # in another terminal
```

It implements the hardware primitives only: pixels, backlight bytes, LED bits,
a key FIFO and the T-bar's ADC. Every documented trap — the latch, the double
buffer, the row skew, the header bytes, the one-transaction read lag — is in
there, with a switch for each so you can see what it was doing.
[docs/EMULATOR.md](docs/EMULATOR.md).

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
python -m ec50 emulate   # the whole panel in a browser, no hardware needed
```

Add `--init` after a power cycle. Add `--backend d2xx|pyftdi|net` to force one.
Add `--controller HOST:PORT` (or set `EC50_CONTROLLER`) to reach a panel — or
an emulator — over the network instead of USB.

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
| `EC50.open(backend=None, index=None, skew=None, controller=None)` | open the panel |
| `panel.clear()` | blank every display, darken every backlight, LEDs off |
| `panel.text(row, col, s, colour)` | label an Assign key, rows 0–2, cols 0–11 |
| `panel.set_cell_text(cell, s, colour)` | label any of the 45 cells |
| `panel.set_bitmap(cell, data)` | raw 256-byte 64×32 1bpp image |
| `panel.set_colour(cell, value)` | backlight colour |
| `panel.led_at(row, col, state)` / `set_led(index, state)` | key LED |
| `panel.flush()` | push and latch |
| `panel.poll()` → `[Event]` | drain key events, refresh T-bar |
| `panel.events()` | blocking generator of events |
| `panel.tbar` / `tbar_raw` / `held` | 0.0–1.0, raw 16-bit, held key set |

`controller` is a `"host:port"` for a panel reached over TCP, which is how you
talk to the emulator; it selects the network backend on its own.

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

## Emulator

`python -m ec50 emulate` is a complete software EC-50 — 82 buttons, 45 displays,
72 LEDs and the T-bar, laid out as they are on the panel — speaking the real
protocol over TCP. Useful for building against the panel without one on the
desk, and for seeing what the hardware's quirks actually do.
[docs/EMULATOR.md](docs/EMULATOR.md).

```python
from ec50.emulator import loopback

panel, dev = loopback()          # driver and virtual panel, no sockets
panel.text(0, 0, "CAM 1", Colour.GREEN)
panel.flush()
assert dev.cell_colour(0) == Colour.GREEN
```

## Tests

```bash
python tests/test_emulator.py        # or: python -m pytest tests/ -q
```

Twenty-five checks against the emulator, so they need no hardware and no
dependencies. They pin both sides to [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Companion

Design for the Bitfocus Companion Satellite integration is in
[docs/COMPANION.md](docs/COMPANION.md) — four surfaces, per-row pages, the T-bar
as a Companion variable. Not yet implemented.

## Status

Working and confirmed on hardware: displays, colour, LEDs, all 82 buttons,
T-bar, on Windows via D2XX. The Linux and macOS transport is implemented but
has not yet been run against a panel. The emulator covers everything the
protocol document describes and is exercised by the test suite.

Two things remain undecoded, neither blocking: a second 45×4 colour table in
the framebuffer that Barco's own software never writes, and whether the
framebuffer accepts an address other than `0x0000` for partial updates.

## Licence

MIT — see [LICENSE](LICENSE).

The protocol was reverse engineered for interoperability. This repository
contains no Barco software, files or firmware; only our own code and factual
descriptions of how the hardware behaves. It is not affiliated with or endorsed
by Barco.
