"""Barco EC-50 control surface driver.

Works on Windows, Linux and macOS. The protocol is plain FTDI MPSSE and is
platform-neutral; only the USB transport differs, and that is chosen for you.

    from ec50 import EC50, Colour, Led

    with EC50.open() as panel:
        panel.clear()
        panel.text(0, 0, "CAM 1", Colour.GREEN)
        panel.flush()
        for ev in panel.events():
            print(ev, panel.tbar)

The surface: 36 Assign keys in a 3 x 12 grid, each with a 64 x 32 1bpp display
and a red/green LED; 82 buttons in total; a 16-bit T-bar.
"""

from .panel import EC50, Event
from .protocol import ASSIGN, ASSIGN_INDEX, BUTTONS, BUTTON_INFO, Colour, Led, colour
from .transport import TransportError, open_transport

__all__ = [
    "EC50", "Event", "Colour", "Led", "colour",
    "ASSIGN", "ASSIGN_INDEX", "BUTTONS", "BUTTON_INFO",
    "TransportError", "open_transport",
]
__version__ = "1.0"
