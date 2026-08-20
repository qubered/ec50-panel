"""Companion Satellite line protocol: encoding, decoding, and constants.

Messages are one line each, `\\n` or `\\r\\n` terminated, of the form:

    COMMAND-NAME ARG1=VAL1 ARG2=true ARG3="VAL3 with spaces"

Responses carry a status word between the command and the arguments:

    ADD-DEVICE OK DEVICEID=ec50-abc-row1

Verified against companion/lib/Service/Satellite/SatelliteApi.ts at API 1.10.0.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

DEFAULT_PORT = 16622

# CHANGE-PAGE and CONFIG_FIELDS arrived in 1.10.0; the page arrows depend on it.
MIN_API_VERSION = (1, 10, 0)


@dataclass
class Message:
    command: str
    status: str | None = None          # OK / ERROR on responses
    args: dict[str, str | bool] = field(default_factory=dict)

    def get(self, key, default=None):
        return self.args.get(key, default)

    def b64(self, key, default=""):
        """Decode a base64 argument. TEXT, VARIABLES and LEDS all use it."""
        raw = self.args.get(key)
        if not isinstance(raw, str) or not raw:
            return default
        try:
            return base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            return default

    def flag(self, key) -> bool:
        v = self.args.get(key)
        return v is True or (isinstance(v, str) and v.lower() in ("1", "true", "yes"))


def _tokenise(line: str):
    """Split on spaces, respecting double quotes."""
    out, cur, quoted = [], [], False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch == " " and not quoted:
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def decode(line: str) -> Message | None:
    line = line.strip("\r\n").strip()
    if not line:
        return None
    tokens = _tokenise(line)
    if not tokens:
        return None

    command = tokens[0].upper()
    status = None
    args: dict[str, str | bool] = {}
    for tok in tokens[1:]:
        if "=" in tok:
            key, _, value = tok.partition("=")
            args[key.upper()] = value
        elif not args:
            # A bare word before any key=value is the status (OK / ERROR).
            status = tok.upper()
    return Message(command, status, args)


def encode(command: str, **params) -> bytes:
    parts = [command]
    for key, value in params.items():
        if value is None:
            continue
        if value is True:
            value = "true"
        elif value is False:
            value = "false"
        text = str(value)
        if " " in text or text == "":
            text = f'"{text}"'
        parts.append(f"{key.upper().replace('__', '_')}={text}")
    return (" ".join(parts) + "\n").encode("utf-8")


def b64_json(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def b64_text(text: str) -> str:
    return base64.b64encode(str(text).encode("utf-8")).decode("ascii")


def parse_colour(value) -> tuple[int, int, int] | None:
    """Companion sends `#rrggbb` or `rgb(r,g,b)` depending on the COLORS mode."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.startswith("#"):
        text = text[1:]
        if len(text) == 3:
            text = "".join(c * 2 for c in text)
        if len(text) != 6:
            return None
        try:
            return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
        except ValueError:
            return None
    if text.lower().startswith("rgb"):
        inner = text[text.find("(") + 1: text.rfind(")")]
        try:
            parts = [int(float(p)) for p in inner.split(",")[:3]]
        except ValueError:
            return None
        if len(parts) == 3:
            return tuple(max(0, min(255, p)) for p in parts)
    return None
