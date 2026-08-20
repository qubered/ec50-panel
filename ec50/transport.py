"""Transport for the EC-50, with a backend per platform.

The panel is an FT232H in MPSSE mode. Three ways to reach it:

  d2xx    FTDI's own driver, via the `ftd2xx` package. The right choice on
          Windows, because Barco's installer already binds ftdibus.sys to
          VID 0x0600 - using D2XX means coexisting with their stack rather
          than replacing it (never run Zadig on this device).

  pyftdi  libusb, via the `pyftdi` package. The right choice on Linux and
          macOS. On Linux nothing in the kernel claims VID 0x0600 - it is
          absent from ftdi_sio's table - so libusb gets the device directly
          once udev grants access. Install 71-barcoipusb.rules for that.

  net     the same MPSSE byte stream over TCP, which is how you reach the
          emulator (`python -m ec50 emulate`) or a panel shared from another
          machine. Say which one with `controller="host:port"`, the
          --controller argument, or $EC50_CONTROLLER.

Everything above this layer is plain byte manipulation and platform-neutral.
"""

from __future__ import annotations

import os
import socket
import sys
import time

VID = 0x0600
PID = 0x0336
PRODUCT_STRINGS = ("show console", "ec show")   # D2XX reports the EEPROM string

DEFAULT_CONTROLLER = "127.0.0.1:16650"
CONTROLLER_ENV = "EC50_CONTROLLER"


def parse_controller(spec: str | None) -> tuple[str, int]:
    """`host:port`, `host`, `:port` or None -> (host, port).

    None falls back to $EC50_CONTROLLER and then to the emulator's own default.
    """
    spec = spec or os.environ.get(CONTROLLER_ENV) or DEFAULT_CONTROLLER
    spec = spec.strip()
    if spec.startswith("["):                    # [::1]:16650
        host, _, rest = spec[1:].partition("]")
        port = rest.lstrip(":")
    elif spec.count(":") == 1:
        host, _, port = spec.partition(":")
    else:
        host, port = spec, ""
    default_host, default_port = DEFAULT_CONTROLLER.split(":")
    try:
        port = int(port) if port else int(default_port)
    except ValueError:
        raise TransportError(f"not a controller address: {spec!r}")
    return host or default_host, port


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

    def __init__(self, index=None, controller=None):
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

    def __init__(self, index=None, controller=None):
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


class NetTransport(Transport):
    """The MPSSE stream over TCP. Talks to `python -m ec50 emulate`.

    The wire is identical to USB, so nothing above this layer can tell the
    difference - which is the whole point of the emulator.
    """

    name = "net"
    timeout = 2.0

    def __init__(self, index=None, controller=None):
        self.host, self.port = parse_controller(controller)
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=5.0)
        except OSError as e:
            raise TransportError(
                f"could not reach a controller at {self.host}:{self.port} ({e}).\n"
                "Start the emulator with `python -m ec50 emulate`, or point "
                "--controller at the right host and port.")
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(self.timeout)
        self._rx = bytearray()

        # A panel takes one host at a time; a busy one hangs up immediately.
        self.sock.settimeout(0.25)
        try:
            if self.sock.recv(1, socket.MSG_PEEK) == b"":
                raise TransportError(
                    f"{self.host}:{self.port} already has a host attached - "
                    "the panel accepts one at a time.")
        except socket.timeout:
            pass
        except OSError:
            pass
        self.sock.settimeout(self.timeout)

    def enter_mpsse(self):
        pass                    # there is no bit-bang mode to leave

    def write(self, data: bytes) -> None:
        try:
            self.sock.sendall(bytes(data))
        except OSError as e:
            raise TransportError(f"controller link lost ({e})")

    def read(self, count: int) -> bytes:
        deadline = time.time() + self.timeout
        while len(self._rx) < count and time.time() < deadline:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                break
            except OSError as e:
                raise TransportError(f"controller link lost ({e})")
            if not chunk:
                raise TransportError("controller closed the connection")
            self._rx += chunk
        out = bytes(self._rx[:count])
        del self._rx[:count]
        return out

    def flush_input(self) -> None:
        self._rx.clear()
        self.sock.setblocking(False)
        try:
            while self.sock.recv(65536):
                pass
        except (BlockingIOError, socket.timeout):
            pass
        except OSError:
            pass
        finally:
            self.sock.setblocking(True)
            self.sock.settimeout(self.timeout)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------

BACKENDS = {"d2xx": D2xxTransport, "pyftdi": PyFtdiTransport,
            "net": NetTransport}
BACKENDS["emulator"] = NetTransport      # the name most people will reach for


def default_backend() -> str:
    """D2XX on Windows so Barco's driver keeps working; libusb elsewhere."""
    return "d2xx" if sys.platform.startswith("win") else "pyftdi"


def open_transport(backend: str | None = None, index: int | None = None,
                   controller: str | None = None) -> Transport:
    """Open the panel, falling back to the other backend if the first is absent.

    A `controller` address implies the network backend, so pointing at an
    emulator needs nothing else.
    """
    if backend is None and (controller or os.environ.get(CONTROLLER_ENV)):
        backend = "net"
    if backend:
        order = [backend]
    else:
        # USB first, then the network, so an emulator left running never
        # silently stands in for a panel that is plugged in.
        order = [default_backend()]
        order += [b for b in ("d2xx", "pyftdi", "net") if b not in order]

    errors = []
    for name in order:
        cls = BACKENDS.get(name)
        if cls is None:
            raise TransportError(f"unknown backend {name!r}; choose from {sorted(BACKENDS)}")
        try:
            t = cls(index, controller)
            t.enter_mpsse()
            return t
        except TransportError as e:
            if len(order) == 1:
                raise                   # only one candidate, so say what it said
            errors.append(f"{name}: {e}")
    raise TransportError("could not open the EC-50.\n  " + "\n  ".join(errors))
