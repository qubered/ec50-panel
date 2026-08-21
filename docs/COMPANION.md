# Companion Satellite integration — design

Turns the EC-50 into surfaces in [Bitfocus Companion](https://bitfocus.io/companion)
over the [Satellite protocol](https://companion.free/for-developers/Satellite-API/),
TCP port 16622.

## Why four surfaces

A Companion surface sits on exactly one page. Independent pages per Assign row
therefore means one `ADD-DEVICE` per row — three registrations over a single TCP
connection, each appearing separately in Companion's Surfaces table. Everything
else shares a fourth.

| Surface | Device ID | Controls | Grid at `--columns 8` |
|---|---|---|---|
| Assign Row 1 | `ec50-<serial>-row1` | 13 | 8 × 2 |
| Assign Row 2 | `ec50-<serial>-row2` | 13 | 8 × 2 |
| Assign Row 3 | `ec50-<serial>-row3` | 13 | 8 × 2 |
| Control | `ec50-<serial>-control` | 39 | 8 × 6 |

`<serial>` comes from the FTDI EEPROM (e.g. `PE4662-029`), so surfaces persist
across restarts and two panels on one host don't collide.

The four surfaces account for **all 82 buttons and all 45 displays**, with
nothing orphaned and nothing invented.

## Wrapping onto the page grid

The panel is wider than any Companion page: 12 Assign keys against a default
grid of 8 columns. Controls are therefore **wrapped** in declaration order onto
a grid `--columns` wide, so at 8 an Assign row fills the top row and spills its
last four keys plus the label onto the next:

```
key/0  key/1  key/2  key/3  key/4  key/5  key/6  key/7
key/8  key/9  key/10 key/11 label
```

A new physical row on the panel always starts a new grid row, so the Control
surface keeps its destination, layer and transport groups visually separate
rather than running together.

Set `--columns` to match Companion's Settings → Grid size. **Companion does not
expose the page grid size** — not over the satellite protocol, whose `gridSize`
is computed from the client's own manifest, and not through the HTTP API — so it
has to be supplied. The default of 8 matches Companion's own default.
`--columns 0` disables wrapping and declares each surface at its physical width.

## Control layout

Declared in advanced mode via a base64 `LAYOUT_MANIFEST`. The tables below give
declaration order; actual row/column comes from the wrap above.

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
2. **No text** → reduce the bitmap to 64 × 32 1-bit through `ec50.image`: luma,
   box-filtered resample, contrast stretch, unsharp, then a threshold chosen
   per picture. A button that is flat colour with a logo on it gets one hard
   cut; a photograph gets a local one. Error diffusion is not the default
   because it turns flat colour into noise, which is most of what a Companion
   button is. It is
   **inverted when that leaves less ink**. Companion draws buttons light-on-dark
   and the panel is dark-on-lit, so most artwork wants inverting: white text
   becomes dark text on a lit key, matching what the font would have drawn. A
   photograph is the exception — inverting one turns it into a negative — so
   the polarity is picked per picture rather than fixed.

   Bitmaps are **opt-in** via `--bitmaps`: requesting them costs roughly 8 KB
   per key, some 360 KB for a full 45-cell refresh, for pixels that are usually
   discarded in favour of the text. At the 64 × 32 we ask for, a 12-key page
   change costs about 14 ms — inside the 25 ms flush window. `--dither bayer`
   is roughly 3× cheaper if a slow host stutters.

   `--prefer-bitmaps` draws the bitmap even when the button also carries text.
   Text wins by default, which is right for a label and wrong for artwork.

   The payload size is taken from its own length rather than trusted: Companion
   is asked for a cell-shaped bitmap but does not promise one, so a length that
   matches neither the request nor a square is skipped rather than rendered
   sheared.
3. **Backlight** ← `COLOR`, quantised to the panel's `RRGGBBII` (two bits per
   channel, 64 colours) — but **not literally**. Companion's default button
   background is `#000000`, and the panel's pixels are dark-on-lit, so the
   backlight is what makes a key legible at all. A key carrying content lights
   **white** when its colour would come out black; an empty key falls back to
   dim (`--blank off|dim|white`). Barco's own software does the same: `0x57`
   dim white for a blank button, `0xFF` bright white for a labelled one.
4. **Key LED** — 72 of the 82 keys have a lamp, two bits wide: red, green or
   off. Three sources, in order (`--leds auto|gauge|colour|pressed|off`):

   1. **A Gauge style layer.** Set a button style layer's usage to Gauge and
      Companion sends its colour as `LEDS`, raw RGB per segment, base64. This
      is the one that is really a feedback: put a feedback on that layer and
      the lamp follows it. The manifest asks for one segment, `mode: simple`,
      on every control that has a lamp.

      `leds` is a recent addition to the satellite layout schema. A Companion
      that does not know it rejects `ADD-DEVICE` outright, so a rejection is
      taken as a version signal: the request is dropped and every surface
      registers again without it, which leaves sources 2 and 3 working.
   2. **The button's background `COLOR`**, for keys with no display. They have
      nowhere else to show it, so it drives the lamp instead: predominantly
      red is red, anything else bright enough is green. The panel has no blue
      lamp, so a blue button reads as simply lit.
   3. **`PRESSED`**, for keys that do have a display — their colour is already
      on the backlight — and for any key the first two had nothing to say
      about. This reflects Companion's view, so remotely triggered presses
      light up too.

   Companion also tracks `action_running`, the green triangle it draws on a
   button while its actions execute, but **does not send it over satellite**.
   `PRESSED` is `pushed`, which is the closest thing the protocol carries. To
   get the real one, put a Gauge layer on the button and drive it from a
   feedback on the internal variable
   `b_actions_running_<page>_<row>_<column>`.

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
2. **Match `--columns` to your page grid.** Settings → Grid size. Nothing needs
   widening — controls wrap to fit — but the width must agree or they will land
   in the wrong places.
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
