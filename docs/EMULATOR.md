# EC-50 emulator

A software EC-50 with a front panel in a browser. It answers the same MPSSE
byte stream the real hardware does, so nothing above the transport can tell the
difference — the driver, the CLI and anything built on top all work against it
unchanged.

```bash
python -m ec50 emulate
```

```
EC-50 emulator
  front panel : http://127.0.0.1:8050/
  controller  : 127.0.0.1:16650   (45 cells, 82 buttons, 16-bit T-bar)

  drive it    : python -m ec50 grid --controller 127.0.0.1:16650
                export EC50_CONTROLLER=127.0.0.1:16650
```

Then in another terminal:

```bash
python -m ec50 grid  --controller 127.0.0.1:16650
python -m ec50 vegas --controller 127.0.0.1:16650
python -m ec50 test  --controller 127.0.0.1:16650    # click keys in the browser
```

`--bind`, `--web-port` and `--device-port` move the listeners; `--no-browser`
stops it opening a window.

## Scope

The emulator implements the **hardware primitives and nothing else**: 45 cells
of 64 × 32 monochrome pixels, one backlight byte each, 82 buttons with two LED
bits apiece, a key FIFO and a 16-bit ADC. It has never heard of destinations,
layers or pages. Pressing a key queues a key event; that is the whole of it.

The silkscreen still says `Destinations` and `Layers`, because that is what is
printed on the panel — but no behaviour hangs off those names.

## Reaching it

Three ways, all equivalent:

| | |
|---|---|
| `--controller HOST:PORT` | on any CLI command |
| `EC50_CONTROLLER=host:port` | environment, so nothing needs a flag |
| `EC50.open(controller="host:port")` | from Python |

Naming a controller selects the `net` backend on its own. Without one the
driver looks for real hardware first and only then the default emulator
address, so an emulator left running never quietly stands in for a panel that
is actually plugged in.

The emulator accepts **one host at a time**, like the real panel. A second
connection is refused with the same shape of error D2XX gives.

## What the browser shows

Everything the panel would show, read back out of its own framebuffer:

- **45 displays** — the pixels the panel is scanning out, not what the driver
  meant to write. Skew, header bytes and unlatched writes all appear exactly as
  they would on the glass.
- **Backlight colour** — the `RRGGBBII` byte decoded the way the hardware
  decodes it, white fallback included.
- **72 LEDs** — two bits each, red, green or off.
- **T-bar** — drag it, or focus it and use the arrow keys. 16-bit, over the
  measured `0x0040`–`0xFC10` travel.
- **82 keys** — click, hold, or multi-touch several at once. Every press and
  release goes through the FIFO the driver reads.

Panel geometry is taken from the front panel diagram in Barco's EC-50 quick
start guide: twelve columns on a 54-unit pitch carrying 52-unit keys, rows that
butt up against each other, each Assign row's display sitting directly on its
own keycap.

## Quirks

Every documented hardware trap is implemented, and each one has a switch in the
GUI so you can see what it was doing.

| Quirk | What it does when on |
|---|---|
| `latch` | Writes are accepted and silently ignored until `0x3828` commits them |
| `banks` | The display is double buffered, so one commit only updates one bank |
| `skew` | Content at x ≥ 32 is driven one row lower than the buffer says |
| `header` | A framebuffer whose first four bytes are zero is discarded whole |
| `reply_lag` | A read returns the previously addressed register's data |

They default to on. Turning `latch` off makes writes appear immediately, which
is a fast way to find out whether a missing label is a latch bug or a rendering
one.

Two more behaviours are always on, because they are not really traps:

- the four header bytes overlay cell 0's first scanline, so cell 0 — Assign
  R1C4 — carries a few stray pixels on its top row, exactly as on hardware;
- `0x3938` sets the `LCD controllers` line in the GUI, so you can see whether
  `--init` has been sent.

**Power cycle** clears the framebuffer, the LEDs, the FIFO and the controller
setup, and is the way to check that your code copes with a panel that has just
been plugged in.

## From Python

`loopback()` wires the driver straight to a virtual panel with no sockets in
between — the basis of the test suite, and useful in yours:

```python
from ec50.emulator import loopback
from ec50 import Colour, protocol as P

panel, dev = loopback()
panel.clear()
panel.text(0, 0, "CAM 1", Colour.GREEN)
panel.flush()

assert dev.cell_colour(P.ASSIGN[0][0]) == Colour.GREEN
dev.press(P.ASSIGN_INDEX[0][0])
assert panel.poll()[0].name == "ASSIGN_0_0"
```

| | |
|---|---|
| `loopback(device=None, skew=None)` | `(EC50, VirtualEC50)` joined directly |
| `dev.cell_pixels(cell)` | the 256 bytes on the glass, skew undone |
| `dev.cell_colour(cell)` | the backlight byte the panel is obeying |
| `dev.led(index)` | `Led.OFF` / `RED` / `GREEN` |
| `dev.press(index)` / `release(index)` | queue a key edge |
| `dev.set_tbar(0.0–1.0)` / `set_tbar_raw(n)` | move the fader |
| `dev.power_cycle()` | forget everything, including the controller setup |
| `dev.banks_agree` | False if the framebuffer was only committed once |
| `dev.quirks[name] = False` | switch one hardware behaviour off |
| `dev.stats`, `dev.log` | transaction counters and a running commentary |

`Emulator(device_port=…, web_port=…, host=…).start()` runs the whole thing —
device socket, web server and GUI — inside your own process.

## Tests

```bash
python tests/test_emulator.py        # or: python -m pytest tests/ -q
```

Twenty-five checks, no hardware and no dependencies. They assert the emulator
against the claims in [PROTOCOL.md](PROTOCOL.md), which means they equally
assert that the *driver* still satisfies them: text and colour reaching the
glass, a line staying straight across x = 32, unlatched writes going nowhere,
one commit leaving the banks out of step, all 72 LEDs addressable without
bleeding into each other, key edges arriving in order, and the T-bar covering
its full travel.

## Layout

```
ec50/emulator/
    device.py     the panel as a state machine: MPSSE, registers, FIFO, ADC
    layout.py     where every key and display physically sits
    server.py     device socket, web server, state broadcast
    wsserver.py   just enough RFC 6455 to talk to a browser
    web/          the front panel itself
```

No third-party packages anywhere in it.
