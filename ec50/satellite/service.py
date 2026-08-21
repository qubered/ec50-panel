"""The runtime: pumps events between the panel and Companion.

Single threaded on purpose. The USB layer is blocking and D2XX handles are not
obviously thread safe, so one loop interleaves a non-blocking socket read with a
panel poll and a timed flush.

Two properties make that safe:

  * Keys are a FIFO, so presses during a ~25ms framebuffer flush queue up and
    drain on the next poll rather than being missed.
  * Display writes are buffered locally, so a page change - which arrives as a
    dozen KEY-STATE messages at once - coalesces into a single flush.
"""

from __future__ import annotations

import time

from .. import image as img, protocol as P
from ..panel import EC50
from . import protocol as proto
from . import surfaces as S
from .client import SatelliteClient

FLUSH_INTERVAL = 0.025      # a framebuffer push is ~24 KB over both banks
TBAR_INTERVAL = 0.05        # 20 Hz
TBAR_DEADBAND = 0.005       # 0.5% of travel


def backlight(rgb, has_content: bool, blank: int = P.Colour.DIM) -> int:
    """Choose a backlight from Companion's button colour.

    Not a literal mapping. Companion's default button background is #000000,
    and taking that at face value leaves every key dark and unreadable - the
    panel's pixels are dark-on-lit, so the backlight IS the legibility. A key
    carrying content therefore lights white when its colour would come out
    black, and an empty key falls back to `blank`.

    Barco's own software works the same way: dim white for a blank button,
    bright white for one with a label.
    """
    r, g, b = ((v * 3 + 127) // 255 for v in (rgb or (0, 0, 0)))
    if (r, g, b) == (0, 0, 0):
        return P.Colour.WHITE if has_content else blank
    return P.colour(r, g, b)


class SatelliteService:
    def __init__(self, host, port=proto.DEFAULT_PORT, panel=None,
                 backend=None, logger=print, init=False, debug=False,
                 bitmaps=False, blank=P.Colour.DIM, columns=8,
                 dither="atkinson", prefer_bitmaps=False):
        self.log = logger
        self.panel: EC50 = panel or EC50.open(backend)
        if init:
            self.panel.init_controllers()
        self.serial = self._serial()
        self.surfaces = S.build()
        problems = S.check(self.surfaces)
        if problems:
            raise RuntimeError("surface map is inconsistent: " + "; ".join(problems))

        self.client = SatelliteClient(host, port, logger, debug=debug)
        self.bitmaps = bitmaps
        self.blank = blank
        self.columns = columns
        self.dither = dither
        self.prefer_bitmaps = prefer_bitmaps
        self._warned: set[str] = set()

        self.device_ids = {s.key: f"ec50-{self.serial}-{s.key}" for s in self.surfaces}
        self.by_device = {self.device_ids[s.key]: s for s in self.surfaces}
        # Which surface owns each panel button, and its control.
        self.routes = {}
        for s in self.surfaces:
            for btn, control in s.by_button.items():
                self.routes[btn] = (s, control)
            for btn, forward in ((s.page_up, False), (s.page_down, True)):
                if btn is not None:
                    self.routes[btn] = (s, forward)

        self._dirty_display = False
        self._dirty_leds = False
        self._last_flush = 0.0
        self._last_tbar = -1.0
        self._last_tbar_at = 0.0

    def _serial(self) -> str:
        try:
            info = self.panel.io.dev.getDeviceInfo()
            raw = info.get("serial", b"")
            text = raw.decode() if isinstance(raw, bytes) else str(raw)
            if text:
                return text.strip().replace(" ", "-")
        except Exception:
            pass
        return "panel"

    # -- outward: Companion -> panel ---------------------------------------

    def _apply_key_state(self, msg):
        surface = self.by_device.get(str(msg.get("DEVICEID")))
        if surface is None:
            return
        control = surface.by_id.get(str(msg.get("CONTROLID")))
        if control is None:
            return

        if control.has_display:
            text = msg.b64("TEXT")
            rgb = proto.parse_colour(msg.get("COLOR"))
            if text == "\U0001f512":
                # Companion draws a lock onto every control of a locked surface
                # when the client has not declared PINCODE_LOCK, so a padlock on
                # everything means locked rather than a button that says "lock".
                self._warn_once("locked",
                                "every key is a padlock, so these surfaces are "
                                "locked in Companion. Unlock them there to see "
                                "content.")
            bitmap = msg.get("BITMAP")
            drew = (bitmap and (self.prefer_bitmaps or not text)
                    and self._draw_bitmap(control.cell, msg))
            if not drew:
                if text:
                    # Newlines are the font's job: it wraps and picks a scale
                    # to suit, and a 64x32 cell holds four lines at scale 1.
                    self.panel.set_cell_text(control.cell, text)
                else:
                    self.panel.clear_cell(control.cell)
                    if self.bitmaps and not bitmap:
                        self._warn_once("nobitmap",
                            "a display control arrived with neither TEXT nor "
                            f"BITMAP (fields: {sorted(msg.args)}). Bitmaps were "
                            "requested, so if none ever arrive this Companion "
                            "may not accept BITMAP_FORMAT=rgb - check the caps "
                            "line above.")
            has_content = bool(text) or bool(msg.get("BITMAP")) or bool(rgb and max(rgb))
            self.panel.set_colour(control.cell,
                                  backlight(rgb, has_content, self.blank))
            self._dirty_display = True

        if control.button is not None:
            self.panel.set_led(control.button,
                               P.Led.GREEN if msg.flag("PRESSED") else P.Led.OFF)
            self._dirty_leds = True

    def _warn_once(self, key: str, message: str) -> None:
        """Say something the first time only. The panel loop runs at 300 Hz."""
        if key not in self._warned:
            self._warned.add(key)
            self.log(f"note: {message}")

    def _draw_bitmap(self, cell, msg) -> bool:
        """Dither a Companion bitmap onto a cell. False if it could not be used.

        Inverted, because Companion draws buttons light-on-dark and the panel
        is the other way round - so a bitmap comes out looking like the text
        the cell would otherwise be showing, not a photographic negative of it.
        """
        import base64
        payload = msg.get("BITMAP")
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            self._warn_once("b64", "BITMAP is not plain base64 - it starts "
                            f"{str(payload)[:24]!r}. A data: URL means Companion "
                            "did not accept BITMAP_FORMAT=rgb.")
            return False
        dims = img.guess_dims(len(raw))
        if dims is None:
            self._warn_once("dims", f"BITMAP is {len(raw)} bytes ({len(raw) // 3} "
                            f"pixels), which is neither the {P.CELL_W}x{P.CELL_H} "
                            "asked for nor square; skipping rather than shearing it.")
            return False
        sw, sh = dims
        self._warn_once("ok", f"bitmaps are arriving, {sw}x{sh}, "
                              f"{self.dither} dithering")
        self.panel.set_bitmap(cell, img.to_cell(
            img.luma_from_rgb(raw), sw, sh,
            dither=self.dither, fit="cover", invert=True, levels=True))
        return True

    # -- inward: panel -> Companion ----------------------------------------

    def _handle_panel(self):
        for ev in self.panel.poll():
            route = self.routes.get(ev.index)
            if route is None:
                continue
            surface, target = route
            device_id = self.device_ids[surface.key]
            if device_id not in self.client.registered:
                continue          # Companion rejects anything before ADD-DEVICE OK
            if isinstance(target, bool):
                if ev.pressed:                      # page arrows fire on press
                    self.client.change_page(device_id, target)
            else:
                self.client.key_press(device_id, target.id, ev.pressed)

        now = time.monotonic()
        control_id = self.device_ids["control"]
        if (now - self._last_tbar_at >= TBAR_INTERVAL
                and control_id in self.client.registered):
            value = self.panel.tbar
            if abs(value - self._last_tbar) >= TBAR_DEADBAND:
                self.client.set_variable(control_id, "tbar", f"{value * 100:.1f}")
                self._last_tbar = value
            self._last_tbar_at = now

    # -- loop --------------------------------------------------------------

    def _register(self):
        for surface in self.surfaces:
            self.client.add_device(surface, self.device_ids[surface.key],
                                   self.serial, bitmaps=self.bitmaps,
                                   columns=self.columns)
            rows, cols = surface.shape(self.columns)
            self.log(f"registered {surface.name} "
                     f"({len(surface.controls)} controls, {cols}x{rows} grid)")

    def run(self):
        self.log(f"panel serial {self.serial} on {self.panel.backend}")
        self.panel.clear()
        self.panel.flush()
        try:
            while True:
                if not self.client.connected:
                    if not self.client.connect():
                        time.sleep(0.2)
                        continue

                for msg in self.client.pump():
                    if msg.command == "KEY-STATE":
                        self._apply_key_state(msg)
                    elif msg.command == "BEGIN":
                        # Companion greets first; register only after that.
                        self._register()
                    elif msg.status == "ERROR" or msg.command == "ERROR":
                        self.log(f"!! Companion rejected {msg.command}: "
                                 f"{msg.get('MESSAGE', msg.args)}")
                    elif msg.command == "ADD-DEVICE" and msg.status == "OK":
                        self.client.registered.add(str(msg.get("DEVICEID")))
                    elif msg.command == "LOCKED-STATE":
                        # Only sent to clients declaring PINCODE_LOCK, which we
                        # do not; harmless, but do not log it as unexpected.
                        pass
                    elif msg.status == "OK":
                        pass          # routine acknowledgement
                    elif msg.command == "CAPS":
                        # Advertises what this Companion supports, including
                        # the bitmap formats it will actually encode.
                        self.log(f"   caps: {dict(msg.args)}")
                    elif msg.command in ("BRIGHTNESS", "KEYS-CLEAR"):
                        pass
                    elif msg.command not in ("PONG", "KEY-STATE"):
                        self.log(f"   unhandled: {msg.command} {msg.status or ''} "
                                 f"{dict(list(msg.args.items())[:4])}")

                if self.client.connected:
                    self.client.keepalive()
                    self._handle_panel()

                now = time.monotonic()
                if (self._dirty_display or self._dirty_leds) and \
                        now - self._last_flush >= FLUSH_INTERVAL:
                    self.panel.flush(display=self._dirty_display,
                                     leds=self._dirty_leds)
                    self._dirty_display = self._dirty_leds = False
                    self._last_flush = now

                time.sleep(0.002)
        except KeyboardInterrupt:
            self.log("\nstopping")
        finally:
            self.panel.clear()
            self.panel.flush()
            self.client.disconnect()
