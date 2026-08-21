#!/usr/bin/env python3
"""Command line front end. Works on Windows, Linux and macOS.

    python -m ec50 info                     which backend, what it found
    python -m ec50 clear                    blank everything
    python -m ec50 text CAM1 CAM2 WIDE      label keys left to right
    python -m ec50 grid                     label R1C1 .. R3C12
    python -m ec50 image logo.png           a picture across the Assign grid
    python -m ec50 watch                    key events and T-bar
    python -m ec50 test                     press a key, it lights up
    python -m ec50 vegas                    colour and LED light show
    python -m ec50 satellite --host IP      serve the panel to Companion

Close the Event Master Toolset first - the panel takes one host at a time.
Add --init after a power cycle. Add --backend d2xx|pyftdi to force one.
"""

import argparse
import math
import sys
import time

from . import image as img, protocol as P
from .panel import EC50
from .protocol import Colour, Led
from .transport import TransportError, default_backend


# Backlight for a key Companion has nothing on.
BLANK_STYLES = {"dim": Colour.DIM, "off": Colour.OFF, "white": Colour.WHITE}


def short_label(name):
    if name.startswith("ASSIGN_"):
        parts = name.split("_")
        if parts[2].isdigit():
            return f"R{int(parts[1]) + 1}C{int(parts[2]) + 1}"
        return parts[2][:5]
    return name.replace("_", "")[:5]


def cmd_info(panel, args):
    print(f"backend        : {panel.backend}  (default here: {default_backend()})")
    print(f"platform       : {sys.platform}")
    print(f"cells          : {P.NUM_CELLS} x {P.CELL_W}x{P.CELL_H} 1bpp")
    print(f"buttons        : {len(P.BUTTON_INFO)}")
    reply = panel.read_reg(P.REG_KEYS)
    print(f"key register   : {reply.hex(' ')}")
    panel.poll()
    print(f"T-bar          : 0x{panel.tbar_raw:04X}  {panel.tbar * 100:.1f}%")


def cmd_clear(panel, args):
    panel.clear()
    panel.flush()
    print("panel cleared.")


def cmd_text(panel, args):
    panel.clear()
    labels = args.labels or []
    for i, label in enumerate(labels[:36]):
        panel.text(i // 12, i % 12, label, Colour.WHITE)
    panel.flush()
    print(f"wrote {len(labels[:36])} labels.")


def cmd_grid(panel, args):
    panel.clear()
    for r in range(3):
        for c in range(12):
            panel.text(r, c, f"R{r + 1}C{c + 1}", Colour.WHITE)
    panel.flush()
    print("labelled R1C1 .. R3C12.")


def cmd_watch(panel, args):
    print("Watching. Press keys and move the T-bar; Ctrl+C to stop.\n")
    last = None
    try:
        while True:
            for ev in panel.poll():
                held = " ".join(sorted(P.BUTTONS.get(i, str(i)) for i in panel.held)) or "-"
                print(f"{ev}   held: {held}")
            pct = panel.tbar * 100
            if last is None or abs(pct - last) >= 0.5:
                bar = "#" * int(pct / 4)
                print(f"T-BAR  0x{panel.tbar_raw:04X}  {pct:5.1f}%  |{bar:<25}|")
                last = pct
            time.sleep(0.003)
    except KeyboardInterrupt:
        print("\nstopped.")


def cmd_test(panel, args):
    """Loopback: blank the panel, then light each key while it is held."""
    panel.clear()
    panel.flush()
    print("Panel cleared. Press keys - each lights its own display and LED.\n")
    try:
        while True:
            if panel.poll():
                panel.clear()
                for idx in panel.held:
                    name, lcd, reg, _ = P.BUTTON_INFO.get(idx, (None, -1, 0xFFFF, 0))
                    if lcd >= 0:
                        panel.set_cell_text(lcd, short_label(name), Colour.WHITE)
                    panel.set_led(idx, Led.GREEN)
                panel.flush()
                held = " ".join(sorted(P.BUTTONS.get(i, str(i)) for i in panel.held)) or "-"
                print(f"held: {held}")
            time.sleep(0.003)
    except KeyboardInterrupt:
        panel.clear()
        panel.flush()
        print("\npanel cleared. stopped.")


def cmd_image(panel, args):
    """Dither a picture onto the Assign grid, or onto one cell."""
    if not args.labels:
        sys.exit("error: give me a PNG or PGM/PPM to draw")
    path = args.labels[0]
    try:
        sw, sh, luma = img.load(path)
    except (OSError, ValueError) as e:
        sys.exit(f"error: {e}")

    cells = [(args.cell, 0, 0)] if args.cell is not None else [
        (P.ASSIGN[r][c], c, r) for r in range(3) for c in range(12)]
    across = 12 if args.cell is None else 1
    down = 3 if args.cell is None else 1
    fw, fh = P.CELL_W * across, P.CELL_H * down

    # One frame for every cell it will land on, thresholded once. Per cell
    # would restart the error diffusion at each seam and pick a different
    # threshold for each, so the joins would show as a grid of hard edges.
    # The picture is scaled on its own and dropped in afterwards, so letterbox
    # padding never reaches the threshold and cannot become a false border.
    ox, oy, tw, th = img.fit_box(sw, sh, fw, fh, args.fit)
    plane = img.resample(luma, sw, sh, tw, th)
    if args.levels:
        plane = img.autolevel(plane)
    amount = (args.sharpen if args.sharpen is not None
              else (1.0 if args.dither in img.THRESHOLDS + ("auto",) else 0.0))
    if amount:
        plane = img.sharpen(plane, tw, th, amount)
    dither = img.pick_dither(plane) if args.dither == "auto" else args.dither
    bits = img.to_bits(plane, tw, th, dither, args.threshold,
                       args.polarity == "light")
    if args.polarity == "auto" and sum(bits) * 2 > len(bits):
        bits = img.to_bits(plane, tw, th, dither, args.threshold, True)

    if args.preview:
        framed = bytearray(fw * fh)
        for y in range(max(0, oy), min(fh, oy + th)):
            for x in range(max(0, ox), min(fw, ox + tw)):
                framed[y * fw + x] = bits[(y - oy) * tw + (x - ox)]
        print(f"{path}  {sw}x{sh} -> {tw}x{th} in {fw}x{fh}  {dither}"
              f", {args.polarity} polarity\n")
        print(img.preview(framed, fw, fh))
        return

    colour = BLANK_STYLES[args.blank] if args.blank != "dim" else Colour.WHITE
    panel.clear()
    for cell, cx, cy in cells:
        panel.set_bitmap(cell, img.pack_at(bits, tw, th,
                                           ox - cx * P.CELL_W, oy - cy * P.CELL_H))
        panel.set_colour(cell, colour)
    panel.flush()
    print(f"drew {path} ({sw}x{sh}) over {len(cells)} cell"
          f"{'s' if len(cells) != 1 else ''}, {dither}.")


def cmd_satellite(panel, args):
    from .satellite.service import SatelliteService
    SatelliteService(args.host, args.port, panel=panel, init=False,
                     debug=args.debug, bitmaps=args.bitmaps,
                     blank=BLANK_STYLES[args.blank], columns=args.columns,
                     dither=args.dither, fit=args.fit, polarity=args.polarity,
                     prefer_bitmaps=args.prefer_bitmaps, leds=args.leds).run()


def cmd_vegas(panel, args):
    words = ["VEGAS", "EC50", "BARCO", "LIVE"]
    seq = (Led.RED, Led.GREEN, Led.OFF, Led.OFF)
    print("Vegas mode. Ctrl+C to stop.\n")
    frame = 0
    try:
        while True:
            word = words[(frame // 16) % len(words)]
            for r in range(3):
                for c in range(12):
                    t = frame * 0.25 - c * 0.5 - r * 0.9
                    rgb = (round(1.5 + 1.5 * math.sin(t)),
                           round(1.5 + 1.5 * math.sin(t + 2.094)),
                           round(1.5 + 1.5 * math.sin(t + 4.188)))
                    panel.text(r, c, word[(c + frame) % len(word)], P.colour(*rgb))
                    panel.led_at(r, c, seq[(c + frame) % len(seq)])
            panel.flush()
            frame += 1
            time.sleep(1.0 / args.fps)
    except KeyboardInterrupt:
        panel.clear()
        panel.flush()
        print("\npanel cleared. stopped.")


COMMANDS = {
    "satellite": cmd_satellite,
    "info": cmd_info, "clear": cmd_clear, "text": cmd_text, "grid": cmd_grid,
    "watch": cmd_watch, "test": cmd_test, "vegas": cmd_vegas,
    "image": cmd_image,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("labels", nargs="*",
                    help="labels for `text`, or an image file for `image`")
    ap.add_argument("--backend", choices=("d2xx", "pyftdi"),
                    help="force a USB backend instead of the platform default")
    ap.add_argument("--index", type=int, help="device index if several are attached")
    ap.add_argument("--init", action="store_true",
                    help="send LCD controller setup (needed after a power cycle)")
    ap.add_argument("--no-skew", action="store_true",
                    help="do not compensate the panel's right-half row skew")
    ap.add_argument("--fps", type=int, default=8, help="vegas frame rate")
    ap.add_argument("--host", default="127.0.0.1", help="Companion host for `satellite`")
    ap.add_argument("--port", type=int, default=16622, help="Companion satellite port")
    ap.add_argument("--debug", action="store_true", help="log raw satellite traffic")
    ap.add_argument("--columns", type=int, default=8, metavar="N",
                    help="width of Companion's page grid; controls wrap onto it "
                         "(default 8, Companion's own default). 0 disables wrapping.")
    ap.add_argument("--blank", choices=sorted(BLANK_STYLES), default="dim",
                    help="backlight for keys with nothing on them (default dim)")
    ap.add_argument("--bitmaps", action="store_true",
                    help="also request button bitmaps as a fallback for keys with no text")
    ap.add_argument("--leds",
                    choices=("auto", "text", "colour", "gauge", "pressed", "off"),
                    default="auto", metavar="MODE",
                    help="what the key lamps report. Default auto: the button's "
                         "background colour for keys with no display, pressed "
                         "state for keys that have one. `text` uses the button's "
                         "TEXT colour on every key - the panel renders text as "
                         "ink and ignores its colour, so a feedback that sets it "
                         "reaches the lamp and nothing else. `colour` uses the "
                         "background everywhere. `gauge` asks for a Gauge style "
                         "layer, which no Companion release supports yet. Also "
                         "pressed, off.")
    ap.add_argument("--prefer-bitmaps", action="store_true",
                    help="draw the bitmap even when the button also has text "
                         "(implies --bitmaps); use it for buttons whose style "
                         "is an image layer")
    ap.add_argument("--dither", choices=img.DITHERS, default=None, metavar="MODE",
                    help="how to reduce greys to ink: " + ", ".join(img.DITHERS)
                         + ". Default auto, which reads the histogram: a hard "
                           "cut for flat artwork, a local one for photographs.")
    ap.add_argument("--fit", choices=img.FITS, default="contain",
                    help="how a picture fills its frame (default contain, which "
                         "letterboxes rather than cropping)")
    ap.add_argument("--sharpen", type=float, default=None, metavar="N",
                    help="unsharp amount before a threshold; puts back the thin "
                         "strokes the downscale averaged away (default 1.0 for "
                         "threshold modes, 0 for dithers)")
    ap.add_argument("--cell", type=int, metavar="N",
                    help="draw into one cell 0-44 instead of the whole Assign grid")
    ap.add_argument("--polarity", choices=img.POLARITIES, default="auto",
                    help="which tone becomes ink: dark inks the dark parts (a "
                         "photograph), light inks the light parts (artwork drawn "
                         "light-on-dark). Default auto, whichever leaves less "
                         "ink, since the majority tone should stay lit.")
    ap.add_argument("--invert", action="store_true",
                    help="shorthand for --polarity light")
    ap.add_argument("--levels", action="store_true",
                    help="stretch contrast to the full range before dithering")
    ap.add_argument("--threshold", type=int, default=128, metavar="N",
                    help="cut point for --dither none (default 128)")
    ap.add_argument("--preview", action="store_true",
                    help="print `image` to the terminal instead; needs no panel")
    args = ap.parse_args()
    if args.dither is None:
        args.dither = "auto"
    if args.prefer_bitmaps:
        args.bitmaps = True
    if args.invert:
        args.polarity = "light"

    if args.command == "image" and args.preview:
        return cmd_image(None, args)          # no hardware needed to look at it

    try:
        panel = EC50.open(args.backend, args.index, skew=0 if args.no_skew else None)
    except TransportError as e:
        sys.exit(f"error: {e}")

    with panel:
        if args.init:
            panel.init_controllers()
        COMMANDS[args.command](panel, args)
