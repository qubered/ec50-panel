"""The emulator process: a virtual panel, a socket for the driver, a GUI.

    python -m ec50 emulate

Two listeners:

  device   raw MPSSE, exactly what the FT232H would carry. One host at a
           time, because that is all the real panel accepts. Point the driver
           at it with `--controller HOST:PORT`.

  web      the front panel, in a browser. Buttons, LEDs, the 45 displays and
           the T-bar, rendered from the panel's own framebuffer.

Nothing above the wire is simulated. The emulator knows about pixels, backlight
bytes, LED bits, a key FIFO and an ADC; it has never heard of destinations or
layers, and pressing a key does nothing but queue an event.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import protocol as P
from . import wsserver
from .device import QUIRKS, VirtualEC50
from .layout import LAYOUT

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
DEFAULT_DEVICE_PORT = 16650
DEFAULT_WEB_PORT = 8050
FRAME_INTERVAL = 1 / 30

_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
          ".css": "text/css", ".json": "application/json",
          ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Emulator:
    def __init__(self, device_port=DEFAULT_DEVICE_PORT, web_port=DEFAULT_WEB_PORT,
                 host="127.0.0.1", quirks=None, verbose=False):
        self.dev = VirtualEC50(quirks)
        self.host = host
        self.device_port = device_port
        self.web_port = web_port
        self.verbose = verbose

        self.clients: set[wsserver.WebSocket] = set()
        self.clients_lock = threading.Lock()
        self.link: dict = {"connected": False, "peer": None, "since": None}
        self._last: dict = {}
        self._log_sent = 0
        self._seen_glass = -1
        self._stop = threading.Event()

    # ======================================================================
    # the device socket - one host, as on the real panel
    # ======================================================================

    def _serve_device(self):
        emu = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock = self.request
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                peer = f"{self.client_address[0]}:{self.client_address[1]}"
                if emu.link["connected"]:
                    # D2XX access to the real panel is exclusive; so is this.
                    # The transport reads the immediate EOF as a busy panel.
                    emu.dev.note(f"refused a second host from {peer}")
                    sock.close()
                    return
                emu.link.update(connected=True, peer=peer, since=time.time())
                emu.dev.note(f"host attached from {peer}")
                emu.dev.touch_io()
                try:
                    while True:
                        data = sock.recv(65536)
                        if not data:
                            break
                        reply = emu.dev.feed(data)
                        if reply:
                            sock.sendall(reply)
                except OSError:
                    pass
                finally:
                    emu.link.update(connected=False, peer=None, since=None)
                    emu.dev.note("host detached")
                    emu.dev.touch_io()

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def handle_error(self, request, client_address):
                exc = sys.exc_info()[1]
                if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                                    ConnectionAbortedError)):
                    return
                super().handle_error(request, client_address)

        self.device_server = Server((self.host, self.device_port), Handler)
        threading.Thread(target=self.device_server.serve_forever,
                         daemon=True, name="ec50-device").start()

    # ======================================================================
    # the web side
    # ======================================================================

    def _serve_web(self):
        emu = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                if emu.verbose:
                    print("web:", fmt % args)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/ws":
                    ws = wsserver.handshake(self)
                    if ws:
                        emu._run_client(ws)
                    return
                if path == "/layout.json":
                    return self._body(json.dumps(LAYOUT).encode(), "application/json")
                name = "index.html" if path in ("/", "") else path.lstrip("/")
                full = os.path.normpath(os.path.join(WEB_DIR, name))
                if not full.startswith(WEB_DIR) or not os.path.isfile(full):
                    self.send_error(404)
                    return
                ext = os.path.splitext(full)[1]
                with open(full, "rb") as fh:
                    self._body(fh.read(), _TYPES.get(ext, "application/octet-stream"))

            def _body(self, data, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

        class WebServer(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                # Browsers close tabs mid-request; that is not a server fault.
                exc = sys.exc_info()[1]
                if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                                    ConnectionAbortedError, TimeoutError)):
                    return
                super().handle_error(request, client_address)

        self.web_server = WebServer((self.host, self.web_port), Handler)
        threading.Thread(target=self.web_server.serve_forever,
                         daemon=True, name="ec50-web").start()

    def _run_client(self, ws: wsserver.WebSocket):
        with self.clients_lock:
            self.clients.add(ws)
        try:
            ws.send_json({"t": "hello", "layout": LAYOUT,
                          "buttons": {str(i): n for i, n in P.BUTTONS.items()},
                          "quirks": self.dev.quirks,
                          "state": self._snapshot(full=True)})
            while True:
                msg = ws.recv()
                if msg is None:
                    break
                try:
                    self._on_message(json.loads(msg))
                except (ValueError, KeyError, TypeError) as e:
                    self.dev.note(f"bad message from the GUI: {e}")
        finally:
            with self.clients_lock:
                self.clients.discard(ws)
            ws.close()

    def _on_message(self, msg: dict):
        kind = msg.get("t")
        if kind == "key":
            index = int(msg["index"])
            if msg.get("down"):
                self.dev.press(index)
            else:
                self.dev.release(index)
            self.dev.touch_io()
        elif kind == "tbar":
            self.dev.set_tbar_raw(int(msg["raw"]))
        elif kind == "quirk":
            name = msg["name"]
            if name in QUIRKS:
                self.dev.quirks[name] = bool(msg["on"])
                self.dev.note(f"quirk {name} {'on' if msg['on'] else 'off'}")
                self.dev.generation += 1
                self.dev.touch_io()
        elif kind == "power":
            self.dev.power_cycle()

    # ======================================================================
    # state out
    # ======================================================================

    def _snapshot(self, full=False) -> dict:
        dev = self.dev
        with dev.lock:
            cells: dict[str, str] = {}
            colours: dict[str, int] = {}
            leds: dict[str, int] = {}
            # Re-encoding 45 cells is the expensive part, so only walk the glass
            # when something has actually committed to it.
            if full or dev.generation != self._seen_glass:
                self._seen_glass = dev.generation
                for cell in range(P.NUM_CELLS):
                    bits = base64.b64encode(bytes(dev.cell_pixels(cell))).decode()
                    if full or self._last.get(("c", cell)) != bits:
                        cells[str(cell)] = bits
                        self._last[("c", cell)] = bits
                    col = dev.cell_colour(cell)
                    if full or self._last.get(("k", cell)) != col:
                        colours[str(cell)] = col
                        self._last[("k", cell)] = col
                for index in P.BUTTON_INFO:
                    state = dev.led(index)
                    if full or self._last.get(("l", index)) != state:
                        leds[str(index)] = state
                        self._last[("l", index)] = state

            log = list(dev.log)
            new_log = log if full else log[self._log_sent:]
            self._log_sent = len(log)

            return {
                "cells": cells, "colours": colours, "leds": leds,
                "tbar": dev.tbar_raw, "held": sorted(dev.held),
                "fifo": len(dev.fifo),
                "link": dict(self.link),
                "initialised": dev.initialised,
                "banks_agree": dev.banks_agree,
                "quirks": dict(dev.quirks),
                "stats": dict(dev.stats),
                "log": new_log,
            }

    def _broadcast(self):
        seen = None
        while not self._stop.wait(FRAME_INTERVAL):
            with self.clients_lock:
                clients = list(self.clients)
            if not clients:
                continue
            dev = self.dev
            now = (dev.generation, dev.io_generation, len(dev.log))
            if now == seen:
                continue
            seen = now
            state = self._snapshot()
            payload = json.dumps({"t": "state", "state": state},
                                 separators=(",", ":"))
            for ws in clients:
                ws.send(payload)

    # ======================================================================

    def start(self):
        self._serve_device()
        self._serve_web()
        threading.Thread(target=self._broadcast, daemon=True,
                         name="ec50-broadcast").start()
        return self

    def stop(self):
        self._stop.set()
        for server in (getattr(self, "device_server", None),
                       getattr(self, "web_server", None)):
            if server:
                server.shutdown()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.web_port}/"

    @property
    def controller(self) -> str:
        return f"{self.host}:{self.device_port}"
