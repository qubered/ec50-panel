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
| Assign Row 1 | `ec50-<serial>-row1` | 13 | 13 × 1 |
| Assign Row 2 | `ec50-<serial>-row2` | 13 | 13 × 1 |
| Assign Row 3 | `ec50-<serial>-row3` | 13 | 13 × 1 |
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
| 12 | `label` | `ASSIGN_n_LABEL` | cell 11 / 14 / 17 |

**The page arrows are not Companion controls.** `ADD-DEVICE` declares
`CAN_CHANGE_PAGE`, and pressing `ASSIGN_n_UP` / `_DOWN` sends
`CHANGE-PAGE DEVICEID=… DIRECTION=…`, which pages that surface directly. That is
what those keys are for physically, and it keeps two columns free.

The row's `label` control drives the LCD beside the arrows. Put a Companion
**page-number button** there and Companion reports `TYPE=PAGENUM`, so the row
shows its live page — row 1 reads `Pg 70`. That is exactly what Barco's own
software puts on those displays (`Preset` / `Pg 2` in the captures).

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
2. **No text** → dither the bitmap down to 64 × 32 1-bit. Bitmaps are **opt-in**
   via `--bitmaps`: requesting them costs roughly 8 KB per key, some 360 KB for
   a full 45-cell refresh, for pixels that are usually discarded in favour of
   the text.
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

1. **Turn off the surface PIN lock**, or unlock these surfaces. While locked,
   Companion sends a padlock as the text of every key and nothing useful
   reaches the panel. Settings → Surfaces.
2. **Increase the page grid.** It defaults to 8 columns; these surfaces are 15
   wide. Settings → Grid size.
3. Add the Satellite connection if not already listening on 16622.
4. Assign each Assign row surface to its page.
5. Tick "Let the panel's page arrows change page" in each row surface's
   settings; `CHANGE-PAGE` is ignored until you do.

## Modules

```
ec50/satellite/
    protocol.py   line encode/decode, base64 fields, quoting
    client.py     TCP client, reconnect, ADD-DEVICE registration
    surfaces.py   the four surface definitions and their button/cell/LED maps
    service.py    the event loop
```

CLI: `python -m ec50 satellite --host 127.0.0.1 [--port 16622]`

## Verified against the source

Checked against `companion/lib/Service/Satellite/` at API **1.10.0** rather than
taken from the docs:

- **`TEXT` is populated.** `SatelliteRenderUtil.ts` sets it from
  `drawStyle?.text?.text`, base64 encoded, so the text-first rendering path is
  sound. `COLOR` / `TEXTCOLOR` follow the requested `hex` or `rgb` format and
  `FONT_SIZE` comes with `TEXT_STYLE`.
- **Style preset fields** are `bitmap: {w, h}`, `text`, `textStyle`,
  `colors: "hex"|"rgb"` and `leds`, per `satellite-surface.schema.json`. A
  preset named `default` is required.
- **Control ids** must match `^[a-zA-Z0-9\-/]+$`.
- **Advanced mode uses `CONTROLID=`**, not `KEY=`.
- **`TYPE`** is a property of the Companion button's own style — `PAGEUP`,
  `PAGEDOWN`, `PAGENUM` or `BUTTON` — reported to us, not declared by us.
- **`CHANGE-PAGE` needs API 1.10.0** and the user must tick the checkbox that
  `CAN_CHANGE_PAGE` creates in the surface's settings; its string is the label.
- The server opens with `BEGIN CompanionVersion=… ApiVersion=…`.
- **The client must send `PING <payload>` about every 2 seconds.** Companion
  closes connections it considers idle; answering its pings is not enough.
  Companion replies `PONG <payload>`. This is the single most likely cause of a
  connection that registers cleanly and then drops.
- Nothing may be sent for a device before its `ADD-DEVICE OK` arrives, or
  Companion answers `ERROR … Device not found`.

## Implementation

```
ec50/satellite/
    protocol.py   line encode/decode, base64 fields, colour parsing
    surfaces.py   the four surface definitions, plus check() for the mapping
    client.py     non-blocking TCP client, reconnect, registration
    service.py    the event loop
```

`surfaces.check()` asserts that the map covers all 82 buttons and all 45
displays exactly once; the service refuses to start if it does not.
