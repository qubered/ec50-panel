"""TCP client for the Companion Satellite protocol.

Non-blocking by design: `pump()` reads whatever is available and returns parsed
messages, so the caller can interleave it with polling the panel in a single
thread. That matters because the USB layer is blocking and D2XX handles are not
obviously thread safe.
"""

from __future__ import annotations

import errno
import socket
import time

from . import protocol as proto
from .protocol import Message

# Companion closes connections it considers idle. The protocol docs are explicit
# that the CLIENT must ping, not merely answer pings, and recommend every 2s.
PING_INTERVAL = 2.0


class SatelliteClient:
    def __init__(self, host: str, port: int = proto.DEFAULT_PORT, logger=print,
                 debug: bool = False):
        self.host = host
        self.port = port
        self.log = logger
        self.debug = debug
        self.sock: socket.socket | None = None
        self._buf = b""
        self._backoff = 1.0
        self._next_attempt = 0.0
        self._last_ping = 0.0
        self._ping_seq = 0
        self.api_version: str | None = None
        self.companion_version: str | None = None
        self.registered: set[str] = set()

    # -- connection --------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def connect(self) -> bool:
        """Try to connect, honouring backoff. Returns True if newly connected."""
        if self.sock is not None:
            return False
        if time.monotonic() < self._next_attempt:
            return False
        try:
            s = socket.create_connection((self.host, self.port), timeout=5)
            s.setblocking(False)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = s
            self._buf = b""
            self._last_ping = time.monotonic()
            self.registered.clear()
            self._backoff = 1.0
            self.log(f"connected to Companion at {self.host}:{self.port}")
            return True
        except OSError as e:
            self._next_attempt = time.monotonic() + self._backoff
            self._backoff = min(self._backoff * 2, 15.0)
            self.log(f"connect failed ({e}); retrying in {self._backoff:.0f}s")
            return False

    def disconnect(self, why: str = "") -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self._buf = b""
        self.registered.clear()
        self._next_attempt = time.monotonic() + 1.0
        if why:
            self.log(f"disconnected: {why}")

    # -- io ----------------------------------------------------------------

    def send_raw(self, line: str) -> None:
        """Send a line that is not key=value, such as PING with a payload."""
        if self.sock is None:
            return
        if self.debug:
            self.log(f"  >> {line}")
        try:
            self.sock.sendall((line + "\n").encode("utf-8"))
        except OSError as e:
            self.disconnect(f"send failed: {e}")

    def keepalive(self) -> None:
        """Ping on schedule. Must be called regularly from the main loop."""
        if self.sock is None:
            return
        now = time.monotonic()
        if now - self._last_ping < PING_INTERVAL:
            return
        self._last_ping = now
        self._ping_seq += 1
        self.send_raw(f"PING {self._ping_seq}")

    def send(self, command: str, **params) -> None:
        if self.sock is None:
            return
        line = proto.encode(command, **params)
        if self.debug:
            self.log(f"  >> {line.decode('utf-8', 'replace').rstrip()}")
        try:
            self.sock.sendall(line)
        except OSError as e:
            self.disconnect(f"send failed: {e}")

    def pump(self) -> list[Message]:
        """Drain everything waiting and return the parsed messages.

        Reads until the socket is empty rather than once per call: Companion
        bursts a KEY-STATE for every control on a page change, and leaving the
        rest queued while the panel flushes only lets the backlog grow.
        """
        if self.sock is None:
            return []
        for _ in range(64):
            try:
                chunk = self.sock.recv(262144)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                self.disconnect(f"read failed: {e}")
                return []
            if not chunk:
                self.disconnect("Companion closed the connection")
                return []
            self._buf += chunk
            if len(chunk) < 262144:
                break

        out = []
        while b"\n" in self._buf:
            line, _, self._buf = self._buf.partition(b"\n")
            if self.debug:
                self.log(f"  << {line.decode('utf-8', 'replace').rstrip()}")
            msg = proto.decode(line.decode("utf-8", "replace"))
            if msg is None:
                continue
            if msg.command == "BEGIN":
                self.api_version = str(msg.get("APIVERSION") or "?")
                self.companion_version = str(msg.get("COMPANIONVERSION") or "?")
                self.log(f"Companion {self.companion_version}, "
                         f"satellite API {self.api_version}")
            elif msg.command == "PING":
                # Companion echoes the payload back on PONG; do the same for it.
                self.send_raw(f"PONG {msg.status or ''}".rstrip())
                continue
            elif msg.command == "PONG":
                continue
            out.append(msg)
        return out

    # -- registration ------------------------------------------------------

    def add_device(self, surface, device_id: str, serial: str,
                   bitmaps: bool = False, columns: int = 8) -> None:
        # SERIAL must be unique per surface. Companion defaults SERIAL_IS_UNIQUE
        # to true, so four surfaces sharing the panel's serial collide; the
        # device id is already unique and stable, so use that.
        params = {
            "DEVICEID": device_id,
            "PRODUCT_NAME": surface.name,
            "SERIAL": device_id,
            "LAYOUT_MANIFEST": proto.b64_json(surface.manifest(bitmaps, columns)),
            "BITMAP_FORMAT": "rgb",
            "BRIGHTNESS": False,
        }
        if surface.can_change_page:
            # The string becomes the label of a checkbox in the surface's
            # settings; the user has to enable it before CHANGE-PAGE is obeyed.
            params["CAN_CHANGE_PAGE"] = "Let the panel's page arrows change page"
        if surface.variables:
            params["VARIABLES"] = proto.b64_json(surface.variables)
        self.send("ADD-DEVICE", **params)

    def key_press(self, device_id: str, control_id: str, pressed: bool) -> None:
        self.send("KEY-PRESS", DEVICEID=device_id, CONTROLID=control_id, PRESSED=pressed)

    def change_page(self, device_id: str, forward: bool) -> None:
        self.send("CHANGE-PAGE", DEVICEID=device_id, DIRECTION=forward)

    def set_variable(self, device_id: str, name: str, value) -> None:
        self.send("SET-VARIABLE-VALUE", DEVICEID=device_id,
                  VARIABLE=name, VALUE=proto.b64_text(value))
