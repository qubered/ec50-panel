"""A very small WebSocket server, so the emulator needs nothing but stdlib.

Enough of RFC 6455 to talk to a browser: the upgrade handshake, masked text
frames in, unmasked text frames out, ping/pong, close. Binary frames and
extensions are not implemented because nothing here sends them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

CONT, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(key: str) -> str:
    digest = hashlib.sha1((key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class WebSocket:
    """One connected browser. `send` is safe to call from any thread."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._tx = threading.Lock()
        self.closed = False

    # -- framing ------------------------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("websocket closed")
            out += chunk
        return bytes(out)

    def recv(self) -> str | None:
        """Next text message, or None once the peer has gone away."""
        payload = bytearray()
        opcode = None
        while True:
            try:
                b1, b2 = self._recv_exact(2)
            except (ConnectionError, OSError):
                return None
            fin = b1 & 0x80
            op = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            try:
                if length == 126:
                    length = struct.unpack(">H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._recv_exact(8))[0]
                mask = self._recv_exact(4) if masked else b""
                data = self._recv_exact(length) if length else b""
            except (ConnectionError, OSError):
                return None
            if mask:
                data = bytes(c ^ mask[i & 3] for i, c in enumerate(data))

            if op == CLOSE:
                self.close()
                return None
            if op == PING:
                self._send_frame(PONG, data)
                continue
            if op == PONG:
                continue
            if op != CONT:
                opcode = op
            payload += data
            if fin:
                if opcode == TEXT:
                    return payload.decode("utf-8", "replace")
                payload = bytearray()
                opcode = None

    def _send_frame(self, opcode: int, data: bytes) -> None:
        n = len(data)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, n)
        elif n < (1 << 16):
            head = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        with self._tx:
            if self.closed:
                return
            try:
                self.sock.sendall(head + data)
            except OSError:
                self.closed = True

    def send(self, text: str) -> None:
        self._send_frame(TEXT, text.encode("utf-8"))

    def send_json(self, obj) -> None:
        self.send(json.dumps(obj, separators=(",", ":")))

    def close(self) -> None:
        if self.closed:
            return
        self._send_frame(CLOSE, b"")
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def handshake(handler) -> WebSocket | None:
    """Upgrade a BaseHTTPRequestHandler's connection. None if it is not a WS request."""
    if handler.headers.get("Upgrade", "").lower() != "websocket":
        return None
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        return None
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(key))
    handler.end_headers()
    handler.wfile.flush()
    return WebSocket(handler.connection)
