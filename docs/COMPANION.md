# Companion Satellite integration — design

Turns the EC-50 into surfaces in [Bitfocus Companion](https://bitfocus.io/companion)
over the [Satellite protocol](https://companion.free/for-developers/Satellite-API/),
TCP port 16622.

## Why four surfaces

A Companion surface sits on exactly one page. Independent pages per Assign row
therefore means one `ADD-DEVICE` per row — three registrations over a single TCP
connection, each appearing separately in Companion's Surfaces table. Everything
else shares a fourth.

| Surface | Device ID | Controls | Grid |
|---|---|---|---|
| Assign Row 1 | `ec50-<serial>-row1` | 15 | 15 × 1 |
| Assign Row 2 | `ec50-<serial>-row2` | 15 | 15 × 1 |
| Assign Row 3 | `ec50-<serial>-row3` | 15 | 15 × 1 |
| Control | `ec50-<serial>-control` | 39 | 15 × 3 |

`<serial>` comes from the FTDI EEPROM (e.g. `PE4662-029`), so surfaces persist
across restarts and two panels on one host don't collide.

The four surfaces account for **all 82 buttons and all 45 displays**, with
nothing orphaned and nothing invented.

## Control layout

Declared in advanced mode via a base64 `LAYOUT_MANIFEST`, so the grid mirrors the
panel's real geometry rather than pretending to be a Stream Deck.

### Assign row (× 3)

| Column | Control ID | Button | Display |
|---|---|---|---|
| 0–11 | `key/0` … `key/11` | `ASSIGN_n_0` … `_11` | its own cell |
| 12 | `page/up` | `ASSIGN_n_UP` | — |
| 13 | `page/down` | `ASSIGN_n_DOWN` | — |
| 14 | `page/num` | `ASSIGN_n_LABEL` | cell 11 / 14 / 17 |

Designate `page/up`, `page/down` and `page/num` as the surface's page controls in
Companion. It then pages that row natively and sends `TYPE=PAGENUM` for the
label, so **the row's LCD shows its live page number** — row 1 literally reads
`Pg 70`. That is what Barco's own software puts on those displays.

### Control surface

| Row | Columns |
|---|---|
| 0 | `dest/0`…`dest/11`, `dest/up`, `dest/down`, `dest/page`* |
| 1 | `layer/0`…`layer/8`, `layer/up`, `layer/down`, `layer/page`*, `freeze/pgm`, `freeze/pvw`, `arm` |
| 2 | `match`, `trans/layer`, `cut/layer`, `cut`, `trans/all`, `cfg/0-0`, `cfg/0-1`, `cfg/1-0`, `cfg/1-1` |

\* `dest/page` (cell 9) and `layer/page` (cell 10) are **display-only** — those
LCDs sit beside the page arrows, not on a key, so no `KEY-PRESS` is ever sent for
them. Barco's CSV files them under `DEST_11` and `LAYER_8`; that is an internal
association, not a physical one.

`cfg/*` carry cells 12, 13, 15 and 16.

## Rendering

Request `TEXT=true`, `TEXT_STYLE=true`, `COLORS=hex`, and a small bitmap.

1. **Text present** → render with the built-in font at native 64 × 32. Crisp, uses
   the full 2:1 cell, and emoji fold to the icon set.
2. **No text** → dither the bitmap down to 64 × 32 1-bit.
3. **Backlight** ← `COLOR`, quantised to the panel's `RRGGBBII` (two bits per
   channel, 64 colours).
4. **Key LED** ← `PRESSED` from `KEY-STATE`: green while Companion holds the
   button active, off otherwise. This reflects Companion's view, so remotely
   triggered presses light up too.

Controls with no display (`page/up`, `dest/0`…) ignore bitmaps entirely; their
`stylePreset` omits bitmaps so Companion never encodes them.

## T-bar

Declared on the Control surface as an input variable:

```json
[{"id": "tbar", "type": "input", "name": "T-bar",
  "description": "Fader position, 0-100"}]
```

Streamed throttled — 20 Hz, and only on a change of ≥ 0.5% — as:

```
SET-VARIABLE-VALUE DEVICEID=<control> VARIABLE="tbar" VALUE="<base64 of 0-100>"
```

It then behaves like any Companion variable in expressions, triggers and
feedbacks.

## Runtime

Single-threaded event loop. No threads, so no questions about D2XX handle
safety.

```
loop:
    select(socket, timeout=5ms)
        readable -> parse lines -> apply state -> mark dirty
    panel.poll()  -> events -> KEY-PRESS
                  -> T-bar  -> SET-VARIABLE-VALUE (throttled)
    if dirty and now - last_flush > 25ms:
        panel.flush(display=cells_dirty, leds=leds_dirty)
```

**Batching is not optional.** A framebuffer push is ~24 KB across both banks,
roughly 25 ms. A page change fires 15 `KEY-STATE` messages at once, so cells are
marked dirty and flushed on a timer, never per message. LED commits are separate
and cheap, so they can go more often.

**Nothing is lost while flushing.** Keys are a FIFO, so presses during a 25 ms
flush queue and drain on the next poll. That is what makes the single-threaded
loop safe.

Reconnection: exponential backoff, re-register all four surfaces on connect,
blank the panel while disconnected.

## Companion-side setup

1. **Increase the page grid.** It defaults to 8 columns; these surfaces are 15
   wide. Settings → Grid size.
2. Add the Satellite connection if not already listening on 16622.
3. Assign each Assign row surface to its page.
4. In each row surface's settings, set the page up / down / number controls.

## Modules

```
ec50/satellite/
    protocol.py   line encode/decode, base64 fields, quoting
    client.py     TCP client, reconnect, ADD-DEVICE registration
    surfaces.py   the four surface definitions and their button/cell/LED maps
    service.py    the event loop
```

CLI: `python -m ec50 satellite --host 127.0.0.1 [--port 16622]`

## To verify before coding

The protocol reference above is from Companion's developer docs. Two details
should be checked against the reference client in
[bitfocus/companion-satellite](https://github.com/bitfocus/companion-satellite)
rather than assumed:

- Exact `stylePreset` field names in `LAYOUT_MANIFEST` (`bitmap`, `colors`, and
  whether `text` belongs there).
- Whether `TEXT` is still populated in current Companion, or whether text is now
  only ever baked into bitmaps. The whole "text first" rendering path depends on
  it; the bitmap fallback is the safety net if it turns out to be empty.

## Build order

1. `protocol.py` + `client.py` — connect, register one dummy surface, log traffic.
2. `surfaces.py` — the maps, unit-testable without hardware.
3. Input path — `KEY-PRESS` out, verified against Companion's button feedback.
4. Output path — text rendering and colour, then LEDs.
5. T-bar variable.
6. Bitmap fallback and dithering.
7. Reconnection and edge cases.
