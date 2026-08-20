"""A virtual EC-50: the panel's own side of the wire.

Speaks the same MPSSE byte stream the real hardware does, so the driver above
it cannot tell the difference. Feed it whatever `Transport.write` would have
sent and it returns whatever `Transport.read` would have got back.

    dev = VirtualEC50()
    reply = dev.feed(mpsse_bytes)

Everything the panel gets wrong, this gets wrong the same way. The quirks are
individually switchable so you can watch what each one actually does:

    latch        writes are ignored until 0x3828 commits them
    header       a framebuffer whose first four bytes are zero is discarded
    reply_lag    a read returns the *previous* transaction's data
    skew         the display's right half is driven one row lower
    banks        the display is double buffered, so one commit is not enough
    exclusive    a second host is refused (enforced by the server)

See docs/PROTOCOL.md. Every behaviour here is one of the claims in that file.
"""

from __future__ import annotations

import threading
from collections import deque

from .. import protocol as P

# MPSSE opcodes the Toolset and this driver actually use.
_CLOCK_OUT = 0x11           # clock bytes out, MSB first, -ve edge
_CLOCK_IO = 0x31            # clock bytes out and in
_CLOCK_IN = 0x20            # clock bytes in
_SET_ADBUS = 0x80
_SET_ACBUS = 0x82
_SEND_IMMEDIATE = 0x87
_NO_ARG = {0x8A, 0x8B, 0x8C, 0x8D, 0x85, 0x84, 0x96, 0x97}
_TWO_ARG = {0x86, 0x9E}

_CS_BIT = 1 << 3            # ADBUS3, active low

QUIRKS = ("latch", "header", "reply_lag", "skew", "banks")


class VirtualEC50:
    """The panel as a state machine. Thread safe; the server pokes it from two."""

    def __init__(self, quirks: dict | None = None):
        self.lock = threading.RLock()
        self.quirks = {name: True for name in QUIRKS}
        if quirks:
            self.quirks.update(quirks)

        # -- output state --------------------------------------------------
        # `pending` is what the host has written; `banks` are what the panel
        # will actually scan out, and only a commit moves one to the other.
        self.pending = P.new_buffer()
        self.banks = [P.new_buffer(), P.new_buffer()]
        self.bank = 0                    # the bank the next commit fills
        self.scanout = 0                 # the bank being displayed
        self.leds_pending = {reg: 0 for reg in P.LED_REGS}
        self.leds_visible = {reg: 0 for reg in P.LED_REGS}

        # -- input state -----------------------------------------------------
        self.fifo: deque[int] = deque()
        self.held: set[int] = set()
        self.tbar_raw = P.TBAR_MIN

        # -- link state ------------------------------------------------------
        self.initialised = False         # has the LCD controller setup been sent
        self.mpsse = False
        self.registers: dict[int, int] = {}
        self.stats = {"writes": 0, "reads": 0, "frames": 0, "commits": 0,
                      "ignored": 0, "bytes_in": 0}
        self.log: deque[str] = deque(maxlen=200)
        self.generation = 0              # bumped when the glass changes
        self.io_generation = 0           # bumped when keys or the link change

        # -- MPSSE parser ----------------------------------------------------
        self._in = bytearray()           # undecoded command bytes
        self._cs = True                  # idle high
        self._tx = bytearray()           # payload clocked out this transaction
        self._pos = 0                    # total bytes clocked this transaction
        self._is_read = False
        self._primed = False
        self._latch = bytes([self.tbar_raw & 0xFF, self.tbar_raw >> 8,
                             P.KEY_IDLE, 0x00])

    # -- notes --------------------------------------------------------------

    def note(self, text: str) -> None:
        self.log.append(text)

    def _touch(self) -> None:
        self.generation += 1

    def touch_io(self) -> None:
        self.io_generation += 1

    # ======================================================================
    # the wire
    # ======================================================================

    def feed(self, data: bytes) -> bytes:
        """Consume host bytes, return whatever the FTDI read buffer would hold."""
        with self.lock:
            self.stats["bytes_in"] += len(data)
            self._in += data
            return bytes(self._parse())

    def _parse(self) -> bytearray:
        out = bytearray()
        buf = self._in
        i = 0
        n = len(buf)
        while i < n:
            op = buf[i]
            if op in (_CLOCK_OUT, _CLOCK_IO):
                if i + 3 > n - 1:
                    break
                count = (buf[i + 1] | (buf[i + 2] << 8)) + 1
                if i + 3 + count > n:
                    break
                chunk = bytes(buf[i + 3:i + 3 + count])
                i += 3 + count
                out += self._clock(chunk, readback=(op == _CLOCK_IO))
            elif op == _CLOCK_IN:
                if i + 3 > n:
                    break
                count = (buf[i + 1] | (buf[i + 2] << 8)) + 1
                i += 3
                out += self._clock(b"", readback=True, blanks=count)
            elif op in (_SET_ADBUS, _SET_ACBUS):
                if i + 3 > n:
                    break
                if op == _SET_ADBUS:
                    self._set_cs(bool(buf[i + 1] & _CS_BIT))
                i += 3
            elif op in _TWO_ARG:
                if i + 3 > n:
                    break
                i += 3
            elif op in _NO_ARG or op == _SEND_IMMEDIATE:
                self.mpsse = True
                i += 1
            else:
                # Real MPSSE answers an unknown opcode with 0xFA and the byte.
                out += bytes([0xFA, op])
                self.note(f"bad MPSSE opcode 0x{op:02X}")
                i += 1
        del buf[:i]
        return out

    def _set_cs(self, high: bool) -> None:
        if high == self._cs:
            return
        self._cs = high
        if high:
            self._end_transaction()
        else:
            self._tx = bytearray()
            self._pos = 0
            self._is_read = False
            self._primed = False

    def _clock(self, data: bytes, readback: bool, blanks: int = 0) -> bytes:
        """Shift `data` out (and/or `blanks` idle bytes), returning MISO."""
        count = len(data) or blanks
        start = self._pos
        if data:
            self._tx += data
            if len(self._tx) >= 3:
                self._is_read = bool(self._tx[2] & 0x80)
            self._prime()
        self._pos += count
        if not readback:
            return b""

        # The panel starts shifting the addressed register out immediately
        # after the four header bytes, so the value lands at reply[4:].
        out = bytearray(count)
        if self._is_read:
            for k in range(count):
                j = start + k - P.HEADER_LEN
                if 0 <= j < len(self._latch):
                    out[k] = self._latch[j]
        return bytes(out)

    # ======================================================================
    # transactions
    # ======================================================================

    def _end_transaction(self) -> None:
        tx = bytes(self._tx)
        self._tx = bytearray()
        if len(tx) < 4:
            return
        if tx[-2:] != b"\xaa\xbb":
            self.note(f"transaction without an AA BB terminator ({len(tx)} bytes)")
            return

        if len(tx) == P.PAYLOAD_LEN and tx[0] == 0 and tx[1] == 0:
            self._write_framebuffer(tx)
            return

        addr = (tx[0] << 8) | tx[1]
        flags = tx[2]
        count = tx[3] + 1
        if flags & 0x80:
            self._read_register(addr, count)
        else:
            body = tx[4:4 + count]
            value = int.from_bytes(body.ljust(4, b"\x00")[:4], "little")
            self._write_register(addr, value)

    # -- reads ---------------------------------------------------------------

    def _read_register(self, addr: int, count: int) -> None:
        self.stats["reads"] += 1
        if self.quirks["reply_lag"]:
            # The panel latches at the end of a transaction and shifts that out
            # during the *next* one, so the first read after switching address
            # returns the previous register's data. With the quirk off the
            # sample was already taken mid-transaction by _prime().
            self._latch = self._sample(addr)

    def _sample(self, addr: int) -> bytes:
        if addr == P.REG_KEYS:
            code = self.fifo.popleft() if self.fifo else P.KEY_IDLE
            return bytes([self.tbar_raw & 0xFF, (self.tbar_raw >> 8) & 0xFF,
                          code, 0x00])
        return self.registers.get(addr, 0).to_bytes(4, "little")

    def _prime(self) -> None:
        """With reply_lag off, answer this transaction instead of the last one.

        `_clock` shifts out of `_latch`, so the sample has to be taken the
        moment the header names a register - not at the end like the panel does.
        """
        if self._primed or self.quirks["reply_lag"]:
            return
        if len(self._tx) >= 4 and self._is_read:
            self._primed = True
            self._latch = self._sample((self._tx[0] << 8) | self._tx[1])

    # -- writes --------------------------------------------------------------

    def _write_register(self, addr: int, value: int) -> None:
        self.stats["writes"] += 1
        self.registers[addr] = value
        if addr == P.REG_COMMIT:
            self._commit(value)
        elif addr in P.LED_REGS:
            self.leds_pending[addr] = value
            if not self.quirks["latch"]:
                self.leds_visible[addr] = value
                self._touch()
        elif addr == 0x3938:
            self.initialised = bool(value)
            self.note("LCD controllers enabled" if value else "LCD controllers off")

    def _write_framebuffer(self, tx: bytes) -> None:
        self.stats["frames"] += 1
        if self.quirks["header"] and not any(tx[:P.HEADER_LEN]):
            self.stats["ignored"] += 1
            self.note("framebuffer discarded: the four header bytes were zero")
            return
        self.pending = bytearray(tx)
        if not self.quirks["latch"]:
            self.banks[0][:] = self.pending
            self.banks[1][:] = self.pending
            self._touch()

    def _commit(self, mask: int) -> None:
        self.stats["commits"] += 1
        if mask & P.COMMIT_LEDS:
            self.leds_visible = dict(self.leds_pending)
            self._touch()
        if mask & P.COMMIT_LCD:
            if self.quirks["banks"]:
                # Each commit fills one bank and scans it out; the other keeps
                # whatever it had. One commit therefore leaves the panel half
                # updated, which is why the Toolset always commits twice.
                self.banks[self.bank][:] = self.pending
                self.scanout = self.bank
                self.bank ^= 1
            else:
                self.banks[0][:] = self.pending
                self.banks[1][:] = self.pending
                self.scanout = 0
            self._touch()

    @property
    def banks_agree(self) -> bool:
        return self.banks[0] == self.banks[1]

    # ======================================================================
    # the front panel
    # ======================================================================

    def press(self, index: int) -> bool:
        with self.lock:
            if index not in P.BUTTON_INFO or index in self.held:
                return False
            self.held.add(index)
            self.fifo.append(0x80 | index)
            self.io_generation += 1
            return True

    def release(self, index: int) -> bool:
        with self.lock:
            if index not in self.held:
                return False
            self.held.discard(index)
            self.fifo.append(index)
            self.io_generation += 1
            return True

    def set_tbar_raw(self, raw: int) -> None:
        with self.lock:
            self.tbar_raw = max(0, min(0xFFFF, int(raw)))
            self.io_generation += 1

    def set_tbar(self, fraction: float) -> None:
        f = max(0.0, min(1.0, float(fraction)))
        self.set_tbar_raw(round(P.TBAR_MIN + f * (P.TBAR_MAX - P.TBAR_MIN)))

    def power_cycle(self) -> None:
        """Everything the panel forgets when it loses power."""
        with self.lock:
            self.pending = P.new_buffer()
            self.banks = [P.new_buffer(), P.new_buffer()]
            self.bank = self.scanout = 0
            self.leds_pending = {reg: 0 for reg in P.LED_REGS}
            self.leds_visible = dict(self.leds_pending)
            self.fifo.clear()
            self.held.clear()
            self.initialised = False
            self.registers.clear()
            self.note("power cycled - the LCD controllers need setting up again")
            self._touch()

    # ======================================================================
    # what the operator sees
    # ======================================================================

    def cell_pixels(self, cell: int) -> bytearray:
        """The 256 bytes actually on the glass, skew undone.

        The panel drives x >= 32 one row lower than the buffer says, so a
        faithful reader has to put those bytes back where they land.
        """
        src = self.banks[self.scanout]
        base = cell * P.CELL_SIZE
        raw = bytearray(src[base:base + P.CELL_SIZE])
        if not self.quirks["skew"]:
            return raw
        out = bytearray(P.CELL_SIZE)
        half = P.CELL_STRIDE // 2
        for r in range(P.CELL_H):
            row = r * P.CELL_STRIDE
            out[row:row + half] = raw[row:row + half]
            src_row = r - P.SKEW_ROWS
            if src_row >= 0:
                s = src_row * P.CELL_STRIDE
                out[row + half:row + P.CELL_STRIDE] = raw[s + half:s + P.CELL_STRIDE]
        return out

    def cell_colour(self, cell: int) -> int:
        """The backlight byte the panel is obeying, white-fallback included."""
        value = self.banks[self.scanout][P.COLOUR_BASE + cell * 4 + 3]
        if (value & 0x03) != 0x03:
            return P.Colour.WHITE       # the II bits gate colour on
        return value

    def led(self, index: int) -> int:
        entry = P.BUTTON_INFO.get(index)
        if not entry:
            return P.Led.OFF
        _, _, reg, bit = entry
        if reg == 0xFFFF:
            return P.Led.OFF
        state = (self.leds_visible.get(reg, 0) >> bit) & 0x3
        return P.Led.OFF if state == 3 else state      # 3 is off, there is no amber
