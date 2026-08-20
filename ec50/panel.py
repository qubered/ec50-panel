"""High-level driver for the Barco EC-50 control surface.

    from ec50 import EC50, Colour, Led

    with EC50.open() as panel:
        panel.clear()
        panel.text(0, 0, "CAM 1", Colour.GREEN)
        panel.led_at(0, 0, Led.GREEN)
        panel.flush()

        while True:
            for ev in panel.poll():
                print(ev)
            print(panel.tbar)

Output is buffered: set_* calls mutate a local framebuffer and nothing reaches
the panel until flush(). Input is a queue - each poll() drains the key FIFO and
refreshes the T-bar - so events are edges and never need debouncing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from . import font, protocol as P
from .transport import Transport, open_transport

# MPSSE fragments, taken verbatim from captures of the Toolset.
_MPSSE_SETUP = bytes([0x8A, 0x97, 0x8D, 0x86, 0x01, 0x00, 0x85])
_PINS_IDLE = bytes([0x80, 0x18, 0x1B])
_PINS_EXTRA = bytes([0x9E, 0x00, 0x04, 0x82, 0x04, 0x04])
_CS_ASSERT = bytes([0x80, 0x90, 0x1B]) * 5      # ADBUS3 low, repeated for setup
_CS_RELEASE = bytes([0x80, 0x98, 0x1B]) * 5
_SEND_IMMEDIATE = bytes([0x87])


@dataclass(frozen=True)
class Event:
    """One key transition. `pressed` is False for a release."""
    index: int
    name: str
    pressed: bool
    row: int | None = None      # Assign grid position, if it is a grid key
    col: int | None = None

    @property
    def is_assign(self) -> bool:
        return self.row is not None

    def __str__(self) -> str:
        where = f" (R{self.row + 1}C{self.col + 1})" if self.is_assign else ""
        return f"{'DOWN' if self.pressed else 'up  '} {self.name}{where}"


_GRID_POS = {idx: (r, c)
             for r, row in enumerate(P.ASSIGN_INDEX)
             for c, idx in enumerate(row)}


class EC50:
    def __init__(self, transport: Transport, skew: int | None = None):
        self.io = transport
        # Compensate the right half's row skew; pass skew=0 to write raw.
        self._skew = P.SKEW_ROWS if skew is None else skew
        self._buf = P.new_buffer()
        self._leds = {reg: 0 for reg in P.LED_REGS}
        self._held: set[int] = set()
        self._tbar_raw = 0
        self._tbar_smooth: float | None = None
        self.io.write(_MPSSE_SETUP)
        self.io.write(_PINS_IDLE)
        self.io.write(_PINS_EXTRA)

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, backend: str | None = None, index: int | None = None,
             skew: int | None = None, controller: str | None = None) -> "EC50":
        """Open the panel. Backend defaults to D2XX on Windows, pyftdi elsewhere.

        `controller` is a "host:port" for a panel reached over the network -
        an emulator, usually - and selects the `net` backend on its own.
        """
        return cls(open_transport(backend, index, controller), skew=skew)

    @property
    def backend(self) -> str:
        return self.io.name

    def close(self) -> None:
        self.io.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- low level ---------------------------------------------------------

    def _frame(self, payload: bytes) -> bytes:
        n = len(payload) - 1
        return (_CS_ASSERT + bytes([0x11, n & 0xFF, (n >> 8) & 0xFF])
                + bytes(payload) + _CS_RELEASE)

    def write_reg(self, addr: int, value: int) -> None:
        payload = bytes([(addr >> 8) & 0xFF, addr & 0xFF, 0x00, 0x03,
                         value & 0xFF, (value >> 8) & 0xFF,
                         (value >> 16) & 0xFF, (value >> 24) & 0xFF]) + b"\xaa\xbb"
        self.io.write(self._frame(payload))

    def read_reg(self, addr: int, count: int = 4) -> bytes:
        """Return the raw reply. The register value is in the tail."""
        payload = bytes([(addr >> 8) & 0xFF, addr & 0xFF, 0x80, count - 1]) + b"\xaa\xbb"
        cmd = (_CS_ASSERT
               + bytes([0x31, len(payload) - 1, 0x00]) + payload
               + bytes([0x20, count - 1, 0x00])
               + _CS_RELEASE + _SEND_IMMEDIATE)
        self.io.flush_input()
        self.io.write(cmd)
        return self.io.read(len(payload) + count)

    def init_controllers(self) -> None:
        """Send the LCD controller setup. Only needed after a power cycle."""
        for addr, value in P.INIT_REGS:
            self.write_reg(addr, value)

    # -- display -----------------------------------------------------------

    def clear(self) -> None:
        """Blank every cell, darken every backlight, turn every LED off."""
        for cell in range(P.NUM_CELLS):
            self.clear_cell(cell)
            self.set_colour(cell, P.Colour.OFF)
        for reg in self._leds:
            self._leds[reg] = 0

    def clear_cell(self, cell: int) -> None:
        # The transport header overlays cell 0's first four bytes; zeroing it
        # makes the panel discard the entire write.
        start = max(cell * P.CELL_SIZE, P.HEADER_LEN)
        end = (cell + 1) * P.CELL_SIZE
        self._buf[start:end] = bytes(end - start)

    def set_pixel(self, cell: int, x: int, y: int) -> None:
        if not (0 <= x < P.CELL_W and 0 <= y < P.CELL_H):
            return
        if x >= P.SKEW_X:
            y -= self._skew          # see SKEW_ROWS in protocol.py
            if y < 0:
                return
        off = cell * P.CELL_SIZE + y * P.CELL_STRIDE + (x >> 3)
        if off < P.HEADER_LEN:
            return
        self._buf[off] |= 1 << (x & 7)      # bit 0 is the LEFTMOST pixel

    def set_bitmap(self, cell: int, data: bytes, skew: bool = True) -> None:
        """Write a raw 256-byte cell bitmap (64x32, 1bpp, LSB leftmost).

        By default the right half is shifted up a row to cancel the panel's
        skew, so `data` is a plain unskewed image. Pass skew=False to write the
        bytes through untouched.
        """
        if len(data) != P.CELL_SIZE:
            raise ValueError(f"expected {P.CELL_SIZE} bytes, got {len(data)}")
        out = bytearray(data)
        if skew and self._skew:
            half = P.CELL_STRIDE // 2
            for r in range(P.CELL_H):
                src = r + self._skew
                row = r * P.CELL_STRIDE
                if src < P.CELL_H:
                    out[row + half:row + P.CELL_STRIDE] = \
                        data[src * P.CELL_STRIDE + half:src * P.CELL_STRIDE + P.CELL_STRIDE]
                else:
                    out[row + half:row + P.CELL_STRIDE] = bytes(half)
        start = cell * P.CELL_SIZE
        keep = max(0, P.HEADER_LEN - start)
        self._buf[start + keep:start + P.CELL_SIZE] = out[keep:]

    def set_colour(self, cell: int, value: int) -> None:
        off = P.COLOUR_BASE + cell * 4
        self._buf[off:off + 4] = bytes([value & 0xFF] * 4)

    def set_cell_text(self, cell: int, text: str, colour: int | None = None,
                      scale: int | None = None, y: int | None = None) -> None:
        self.clear_cell(cell)
        for x, py in font.plot(text, P.CELL_W, P.CELL_H, scale, y):
            self.set_pixel(cell, x, py)
        if colour is not None:
            self.set_colour(cell, colour)

    def text(self, row: int, col: int, text: str, colour: int | None = None,
             **kw) -> None:
        """Label an Assign grid key. Rows 0-2 top to bottom, cols 0-11."""
        self.set_cell_text(P.ASSIGN[row][col], text, colour, **kw)

    # -- LEDs --------------------------------------------------------------

    def set_led(self, index: int, state: int) -> None:
        """Set the LED of any button, by its button index."""
        entry = P.BUTTON_INFO.get(index)
        if not entry:
            return
        _, _, reg, bit = entry
        if reg == 0xFFFF:
            return
        self._leds[reg] = (self._leds[reg] & ~(0x3 << bit)) | ((state & 0x3) << bit)

    def led_at(self, row: int, col: int, state: int) -> None:
        self.set_led(P.ASSIGN_INDEX[row][col], state)

    # -- flush -------------------------------------------------------------

    def flush(self, display: bool = True, leds: bool = True) -> None:
        """Push pending changes and latch them.

        The display is double buffered, so the framebuffer goes out twice; a
        single pass would only update one bank.
        """
        if display:
            for _ in range(2):
                self.io.write(self._frame(bytes(self._buf)))
                self.write_reg(P.REG_COMMIT, P.COMMIT_LCD)
        if leds:
            for reg, val in self._leds.items():
                self.write_reg(reg, val)
            self.write_reg(P.REG_COMMIT, P.COMMIT_LEDS)

    # -- input -------------------------------------------------------------

    def poll(self, max_events: int = 16) -> list[Event]:
        """Drain the key FIFO and refresh the T-bar. Returns any transitions."""
        events: list[Event] = []
        for _ in range(max_events):
            reply = self.read_reg(P.REG_KEYS)
            if len(reply) <= P.KEY_BYTE:
                break

            raw = (reply[P.TBAR_HI] << 8) | reply[P.TBAR_LO]
            self._tbar_raw = raw
            self._tbar_smooth = raw if self._tbar_smooth is None else \
                self._tbar_smooth * 0.5 + raw * 0.5

            code = reply[P.KEY_BYTE]
            if code == P.KEY_IDLE:
                break
            index = code & 0x7F
            pressed = bool(code & 0x80)
            if pressed:
                self._held.add(index)
            else:
                self._held.discard(index)
            row, col = _GRID_POS.get(index, (None, None))
            events.append(Event(index, P.BUTTONS.get(index, f"index {index}"),
                                pressed, row, col))
        return events

    def events(self, interval: float = 0.003) -> Iterator[Event]:
        """Block forever, yielding key events as they happen."""
        import time
        while True:
            for ev in self.poll():
                yield ev
            time.sleep(interval)

    @property
    def held(self) -> set[int]:
        return set(self._held)

    @property
    def tbar_raw(self) -> int:
        """Position as the panel reports it, 16-bit."""
        return self._tbar_raw

    @property
    def tbar(self) -> float:
        """Position as 0.0 - 1.0, on a fixed scale needing no calibration."""
        v = self._tbar_smooth if self._tbar_smooth is not None else self._tbar_raw
        return P.tbar_percent(v) / 100.0
