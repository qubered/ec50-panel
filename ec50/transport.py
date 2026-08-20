"""USB transport for the EC-50, with a backend per platform.

The panel is an FT232H in MPSSE mode. Two ways to reach it:

  d2xx    FTDI's own driver, via the `ftd2xx` package. The right choice on
          Windows, because Barco's installer already binds ftdibus.sys to
          VID 0x0600 - using D2XX means coexisting with their stack rather
          than replacing it (never run Zadig on this device).

  pyftdi  libusb, via the `pyftdi` package. The right choice on Linux and
          macOS. On Linux nothing in the kernel claims VID 0x0600 - it is
          absent from ftdi_sio's table - so libusb gets the device directly
          once udev grants access. Install 71-barcoipusb.rules for that.

Everything above this layer is plain byte manipulation and platform-neutral.
"""

from __future__ import annotations

import sys
import time

VID = 0x0600
PID = 0x0336
PRODUCT_STRINGS = ("show console", "ec show")   # D2XX reports the EEPROM string


class TransportError(RuntimeError):
    pass


class Transport:
    """Minimal contract the protocol layer needs."""

    name = "?"

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, count: int) -> bytes:
        raise NotImplementedError

    def flush_input(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------


class D2xxTransport(Transport):
    name = "d2xx"

    def __init__(self, index=None):
        try:
            import ftd2xx
        except ImportError:
            raise TransportError("pip install ftd2xx")
        self._m = ftd2xx

        n = ftd2xx.createDeviceInfoList()
        if n == 0:
            raise TransportError("no FTDI devices found; is the EC-50 powered and connected?")

        target = index
        found = []
        for i in range(n):
            info = ftd2xx.getDeviceInfoDetail(i)
            desc = info.get("description", b"")
            if isinstance(desc, bytes):
                desc = desc.decode("latin-1", "replace")
            found.append(desc)
            if target is None and any(s in desc.lower() for s in PRODUCT_STRINGS):
                target = i
        if target is None:
            raise TransportError(
                "could not identify the console board among: " + ", ".join(repr(d) for d in found))

        try:
            self.dev = ftd2xx.open(target)
        except Exception as e:
            raise TransportError(
                f"could not open the device ({e}). Close the Event Master Toolset - "
                "D2XX access is exclusive.")

        self.dev.setTimeouts(2000, 2000)
        self.dev.setLatencyTimer(1)
        self.dev.purge()

    def enter_mpsse(self):
        self.dev.setBitMode(0x00, 0x00)
        self.dev.setBitMode(0x00, 0x02)

    def write(self, data: bytes) -> None:
        self.dev.write(bytes(data))

    def read(self, count: int) -> bytes:
        return bytes(self.dev.read(count))

    def flush_input(self) -> None:
        self.dev.purge(1)

    def close(self) -> None:
        try:
            self.dev.setBitMode(0x00, 0x00)
        finally:
            self.dev.close()


# ---------------------------------------------------------------------------


class PyFtdiTransport(Transport):
    name = "pyftdi"

    def __init__(self, index=None):
        try:
            from pyftdi.ftdi import Ftdi
        except ImportError:
            raise TransportError("pip install pyftdi")
        self._Ftdi = Ftdi

        # pyftdi only recognises FTDI's own IDs unless told otherwise.
        Ftdi.add_custom_vendor(VID, "barco")
        Ftdi.add_custom_product(VID, PID, "ec50")

        self.dev = Ftdi()
        try:
            self.dev.open(vendor=VID, product=PID, index=index or 0)
        except Exception as e:
            raise TransportError(
                f"could not open {VID:04x}:{PID:04x} ({e}).\n"
                "On Linux, install the udev rule and re-plug:\n"
                "  sudo cp 71-barcoipusb.rules /etc/udev/rules.d/\n"
                "  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
                "On macOS you may need to unload Apple's FTDI driver first.")
        self.dev.set_latency_timer(1)
        self.dev.purge_buffers()

    def enter_mpsse(self):
        self.dev.set_bitmode(0x00, self._Ftdi.BitMode.RESET)
        self.dev.set_bitmode(0x00, self._Ftdi.BitMode.MPSSE)

    def write(self, data: bytes) -> None:
        self.dev.write_data(bytes(data))

    def read(self, count: int) -> bytes:
        # read_data_bytes returns what is available; loop until we have enough.
        out = bytearray()
        deadline = time.time() + 2.0
        while len(out) < count and time.time() < deadline:
            chunk = self.dev.read_data_bytes(count - len(out), attempt=4)
            if chunk:
                out += chunk
        return bytes(out)

    def flush_input(self) -> None:
        self.dev.purge_rx_buffer()

    def close(self) -> None:
        try:
            self.dev.set_bitmode(0x00, self._Ftdi.BitMode.RESET)
        finally:
            self.dev.close()


# ---------------------------------------------------------------------------

BACKENDS = {"d2xx": D2xxTransport, "pyftdi": PyFtdiTransport}


def default_backend() -> str:
    """D2XX on Windows so Barco's driver keeps working; libusb elsewhere."""
    return "d2xx" if sys.platform.startswith("win") else "pyftdi"


def open_transport(backend: str | None = None, index: int | None = None) -> Transport:
    """Open the panel, falling back to the other backend if the first is absent."""
    order = [backend] if backend else [default_backend()]
    if not backend:
        order += [b for b in BACKENDS if b not in order]

    errors = []
    for name in order:
        cls = BACKENDS.get(name)
        if cls is None:
            raise TransportError(f"unknown backend {name!r}; choose from {list(BACKENDS)}")
        try:
            t = cls(index)
            t.enter_mpsse()
            return t
        except TransportError as e:
            errors.append(f"{name}: {e}")
    raise TransportError("could not open the EC-50.\n  " + "\n  ".join(errors))
