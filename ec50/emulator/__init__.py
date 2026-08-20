"""A software EC-50: the same wire protocol, the same front panel, in a browser.

    python -m ec50 emulate                      then open http://127.0.0.1:8050/
    python -m ec50 grid --controller 127.0.0.1:16650

The emulator implements the *hardware*, not Barco's application: 45 displays,
a backlight byte each, 82 buttons with LED bits, a key FIFO and a 16-bit ADC on
the T-bar. Pressing a key queues an event and does nothing else - there are no
destinations, layers or pages in here, only the primitives in docs/PROTOCOL.md.

For tests, `loopback()` hands you a driver wired straight to a virtual panel
with no sockets in between:

    panel, dev = loopback()
    panel.text(0, 0, "CAM 1", Colour.GREEN)
    panel.flush()
    assert dev.cell_colour(protocol.ASSIGN[0][0]) == Colour.GREEN
"""

from ..transport import Transport
from .device import QUIRKS, VirtualEC50
from .layout import LAYOUT, build as build_layout
from .server import DEFAULT_DEVICE_PORT, DEFAULT_WEB_PORT, Emulator

__all__ = ["VirtualEC50", "Emulator", "LoopbackTransport", "loopback",
           "LAYOUT", "build_layout", "QUIRKS",
           "DEFAULT_DEVICE_PORT", "DEFAULT_WEB_PORT"]


class LoopbackTransport(Transport):
    """A Transport that hands bytes to a VirtualEC50 in the same process."""

    name = "loopback"

    def __init__(self, device: "VirtualEC50 | None" = None):
        self.device = device or VirtualEC50()
        self._rx = bytearray()

    def enter_mpsse(self):
        pass

    def write(self, data: bytes) -> None:
        self._rx += self.device.feed(bytes(data))

    def read(self, count: int) -> bytes:
        out = bytes(self._rx[:count])
        del self._rx[:count]
        return out

    def flush_input(self) -> None:
        self._rx.clear()

    def close(self) -> None:
        self._rx.clear()


def loopback(device=None, skew=None):
    """(EC50, VirtualEC50) joined directly, for tests and demos."""
    from ..panel import EC50
    io = LoopbackTransport(device)
    return EC50(io, skew=skew), io.device
