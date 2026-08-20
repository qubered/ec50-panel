#!/usr/bin/env python3
"""Command line front end. Works on Windows, Linux and macOS.

    python -m ec50 info                     which backend, what it found
    python -m ec50 clear                    blank everything
    python -m ec50 text CAM1 CAM2 WIDE      label keys left to right
    python -m ec50 grid                     label R1C1 .. R3C12
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

from . import protocol as P
from .panel import EC50
from .protocol import Colour, Led
from .transport import TransportError, default_backend


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


def cmd_satellite(panel, args):
    from .satellite.service import SatelliteService
    SatelliteService(args.host, args.port, panel=panel, init=False).run()


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
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("labels", nargs="*", help="labels for the `text` command")
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
    args = ap.parse_args()

    try:
        panel = EC50.open(args.backend, args.index, skew=0 if args.no_skew else None)
    except TransportError as e:
        sys.exit(f"error: {e}")

    with panel:
        if args.init:
            panel.init_controllers()
        COMMANDS[args.command](panel, args)
