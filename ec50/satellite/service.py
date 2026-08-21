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


# Below this a colour is "off" rather than a very dim something.
LED_FLOOR = 24

LED_MODES = ("auto", "text", "colour", "gauge", "pressed", "off")


def led_colour(rgb) -> int:
    """Map a Companion colour onto the panel's two-colour lamp.

    The lamp is two bits and does red, green or nothing, so this is a fold
    rather than a conversion: red when the colour is predominantly red, green
    for anything else bright enough to count as lit. Blue and white have no
    lamp of their own and come out green, which is what "the LED is on" means
    on hardware that cannot do blue.
    """
    if not rgb:
        return P.Led.OFF
    r, g, b = rgb
    if max(r, g, b) < LED_FLOOR:
        return P.Led.OFF
    return P.Led.RED if (r > g and r > b) else P.Led.GREEN


def gauge_colour(payload):
    """Average a LEDS payload down to one colour, or None if there isn't one.

    LEDS is raw RGB per segment, base64, and never follows the negotiated
    bitmap format. One segment is asked for, but average whatever arrives:
    a ring would otherwise be represented by whichever segment came first.
    """
    if not payload:
        return None
    import base64
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception:
        return None
    n = len(data) // 3
    if n == 0:
        return None
    return tuple(sum(data[i * 3 + c] for i in range(n)) // n for c in range(3))


class SatelliteService:
    def __init__(self, host, port=proto.DEFAULT_PORT, panel=None,
                 backend=None, logger=print, init=False, debug=False,
                 bitmaps=False, blank=P.Colour.DIM, columns=8,
                 dither="auto", fit="contain", polarity="auto",
                 prefer_bitmaps=False, leds="auto"):
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
        self.fit = fit
        self.polarity = polarity
        if leds not in LED_MODES:
            raise ValueError(f"leds must be one of {LED_MODES}")
        self.leds = leds
        # `leds` is not in any released Companion - 5.0.3, the newest as of
        # writing, validates the manifest against a JSON Schema with
        # additionalProperties:false, so the field is rejected outright and
        # takes the whole surface with it. Asking is therefore opt-in. A
        # Companion that sends LEDS unprompted is still obeyed below.
        self._ask_leds = leds == "gauge"
        self._retrying = False
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
            self.panel.set_led(control.button, self._led_state(control, msg))
            self._dirty_leds = True

    def _led_state(self, control, msg) -> int:
        """Decide what a key's lamp should do.

        Three sources, because no single one covers the panel. A Gauge style
        layer is the honest answer - it is a feedback, it is per key, and it is
        what Companion sends LEDS for - but no released Companion supports it
        yet, so `--leds gauge` has to ask. Failing that, a key with no display
        has nowhere else
        to show its background colour, so the colour drives the lamp. A key
        that does have a display shows its colour on the backlight already, so
        its lamp reports PRESSED instead.

        `--leds text` overrides all of it with the button's text colour, which
        is the only per-key colour a feedback can set that this panel is not
        already using for something else.

        Companion has an `action_running` flag - the green triangle on a button
        - but does not send it over satellite; PRESSED is `pushed`, which is the
        closest thing available.
        """
        if self.leds == "off":
            return P.Led.OFF
        if self.leds == "text":
            # The panel renders text as ink and throws the colour away, so
            # TEXTCOLOR is a whole per-key colour channel going spare - and
            # Companion has sent it since well before the Gauge layer existed.
            # A feedback that sets the text colour therefore reaches the lamp
            # on every key, including the ones whose background is already
            # committed to the backlight.
            state = led_colour(proto.parse_colour(msg.get("TEXTCOLOR")))
            if state != P.Led.OFF:
                return state
        if self.leds in ("auto", "gauge"):
            # Only `gauge` asks for these, but obey them wherever they appear:
            # a Companion new enough to send them unprompted should be heard.
            gauge = gauge_colour(msg.get("LEDS"))
            if gauge is not None and max(gauge) >= LED_FLOOR:
                self._warn_once("gauge", "a Gauge style layer is driving the key "
                                         "lamps")
                return led_colour(gauge)
            if self.leds == "gauge":
                return P.Led.OFF
        if self.leds == "colour" or (self.leds == "auto" and not control.has_display):
            state = led_colour(proto.parse_colour(msg.get("COLOR")))
            if state != P.Led.OFF or self.leds == "colour":
                return state
            # A key with no colour to report has nothing to lose by falling
            # through, and a dark key that still lights when pressed is better
            # than one that never lights at all.
        return P.Led.GREEN if msg.flag("PRESSED") else P.Led.OFF

    def _warn_once(self, key: str, message: str) -> None:
        """Say something the first time only. The panel loop runs at 300 Hz."""
        if key not in self._warned:
            self._warned.add(key)
            self.log(f"note: {message}")

    def _draw_bitmap(self, cell, msg) -> bool:
        """Reduce a Companion bitmap to a cell. False if it could not be used.

        Polarity is chosen per picture rather than fixed. Companion draws its
        buttons light-on-dark and the panel is dark-on-lit, so most artwork
        wants inverting - but a photograph does not, and inverting one turns it
        into a negative. Whichever way leaves less ink is the right way round:
        the majority tone is the one that should stay lit.
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
            dither=self.dither, fit=self.fit,
            polarity=self.polarity, levels=True))
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
                                   columns=self.columns, leds=self._ask_leds)
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
                        if msg.command == "ADD-DEVICE" and self._ask_leds:
                            self._ask_leds = False
                            self._retrying = True
                            self.log("note: Companion rejected the layout "
                                     f"({msg.get('MESSAGE', msg.args)}) - this "
                                     "build has no `leds` in its satellite "
                                     "schema. Registering again without it; "
                                     "key lamps fall back to button colour and "
                                     "pressed state.")
                            self._register()
                        elif not self._retrying:
                            self.log(f"!! Companion rejected {msg.command}: "
                                     f"{msg.get('MESSAGE', msg.args)}")
                    elif msg.command == "ADD-DEVICE" and msg.status == "OK":
                        self._retrying = False
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
