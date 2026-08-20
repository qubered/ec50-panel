/* The EC-50 front panel, drawn from whatever the virtual hardware is showing.
 *
 * There is no application logic in here on purpose. A key press sends a key
 * index; an LCD draws the 256 bytes the panel's framebuffer holds for that
 * cell; an LED shows two bits. Nothing knows what a destination is.
 */

const $ = (id) => document.getElementById(id);

const QUIRK_TEXT = {
  latch: ["Latch before anything shows",
    "Writes are accepted and silently ignored until 0x3828 commits them - " +
    "bit 0 latches the LEDs, bit 1 the framebuffer."],
  banks: ["Double buffered display",
    "One commit fills one bank. Commit twice, as the Toolset does, or half " +
    "the panel keeps the old frame."],
  skew: ["Right half sits a row lower",
    "Two 32px driver chips with a one-row addressing offset, so content at " +
    "x ≥ 32 must be stored a row higher to come out straight."],
  header: ["Framebuffer header guard",
    "The four header bytes overlay cell 0's first scanline. Zero them and the " +
    "panel discards the entire 11,962-byte write."],
  reply_lag: ["Reads lag one transaction",
    "The panel latches at the end of a transaction and shifts it out during " +
    "the next, so the first read after switching address is stale."],
};
const QUIRK_ORDER = ["latch", "banks", "skew", "header", "reply_lag"];

const LEVELS = [0, 85, 170, 255];
const UNLIT_GLASS = "#17191c";
const UNLIT_PIXEL = "#0e1012";

let layout = null;
let ws = null;
const cells = new Map();      // cell number -> {canvas, ctx, bits, colour, ratio}
const keys = new Map();       // button index -> {el, led}
let tbar = { el: null, lever: null, slot: null, raw: 0, dragging: false };
let scale = 1;

/* ------------------------------------------------------------------ colour */

function backlight(byte) {
  // RRGGBBII. Both low bits must be set or the panel ignores the colour.
  if ((byte & 3) !== 3) return "rgb(255,255,255)";
  const r = LEVELS[(byte >> 6) & 3];
  const g = LEVELS[(byte >> 4) & 3];
  const b = LEVELS[(byte >> 2) & 3];
  if (!r && !g && !b) return null;            // backlight off
  return `rgb(${r},${g},${b})`;
}

/* --------------------------------------------------------------------- LCD */

function drawCell(cell) {
  const c = cells.get(cell);
  if (!c) return;
  const ratio = (window.devicePixelRatio || 1) * scale;
  const w = Math.max(1, Math.round(layout.cell.pw * ratio));
  const h = Math.max(1, Math.round(layout.cell.ph * ratio));
  if (c.canvas.width !== w || c.canvas.height !== h) {
    c.canvas.width = w;
    c.canvas.height = h;
  }
  const ctx = c.ctx;
  const px = w / layout.cell.w;
  const py = h / layout.cell.h;
  const lit = backlight(c.colour);

  ctx.fillStyle = lit || UNLIT_GLASS;
  ctx.fillRect(0, 0, w, h);

  // A dot-matrix gap only once there are enough device pixels to show one.
  const gap = px >= 3 ? Math.max(1, Math.round(px * 0.12)) : 0;
  ctx.fillStyle = lit ? "rgba(0,0,0,0.86)" : UNLIT_PIXEL;
  const bits = c.bits;
  if (!bits) return;
  for (let y = 0; y < layout.cell.h; y++) {
    const row = y * 8;
    for (let xb = 0; xb < 8; xb++) {
      const byte = bits[row + xb];
      if (!byte) continue;
      for (let b = 0; b < 8; b++) {
        // bit 0 of each byte is the LEFTMOST pixel, not bit 7
        if (!(byte & (1 << b))) continue;
        const x = xb * 8 + b;
        ctx.fillRect(Math.round(x * px), Math.round(y * py),
                     Math.max(1, Math.round(px) - gap),
                     Math.max(1, Math.round(py) - gap));
      }
    }
  }
}

function makeLcd(cell) {
  const canvas = document.createElement("canvas");
  canvas.className = "lcd";
  canvas.style.width = layout.cell.pw + "px";
  canvas.style.height = layout.cell.ph + "px";
  canvas.style.flex = "0 0 auto";
  cells.set(cell, {
    canvas, ctx: canvas.getContext("2d"),
    bits: new Uint8Array(256), colour: 0x03,
  });
  return canvas;
}

/* -------------------------------------------------------------------- keys */

function press(index, down) {
  const k = keys.get(index);
  if (!k) return;
  if (k.down === down) return;
  k.down = down;
  k.el.classList.toggle("down", down);
  send({ t: "key", index, down });
}

function buildKey(spec) {
  const el = document.createElement("button");
  el.className = `key ${spec.kind}`;
  if (spec.tint) el.classList.add("tint-" + spec.tint);
  el.style.cssText =
    `left:${spec.x}px;top:${spec.y}px;width:${spec.w}px;height:${spec.h}px`;
  const hasCell = spec.cell !== null && spec.cell !== undefined;
  el.title = `${spec.name}  (button ${spec.index}`
    + (hasCell ? `, cell ${spec.cell})` : ")")
    // Barco's CSV files a display against two keys that do not carry one:
    // those windows sit beside the page arrows, not on the key.
    + (!hasCell && spec.csv_cell >= 0
        ? `\ncell ${spec.csv_cell} is filed here in Barco's CSV, but the `
          + "window is the one beside the arrows"
        : "");

  // An Assign key is a display sitting on its own keycap, as on the panel.
  const cap = document.createElement("span");
  cap.className = "cap";
  if (spec.cell !== null && spec.cell !== undefined) {
    el.appendChild(makeLcd(spec.cell));
  } else if (spec.label) {
    const label = document.createElement("span");
    label.className = "caplabel";
    label.textContent = spec.label;
    cap.appendChild(label);
  }

  let led = null;
  if (spec.led) {
    led = document.createElement("span");
    led.className = "led";
    cap.appendChild(led);
  }
  el.appendChild(cap);
  keys.set(spec.index, { el, led, down: false });

  el.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    press(spec.index, true);
  });
  const up = () => press(spec.index, false);
  el.addEventListener("pointerup", up);
  el.addEventListener("pointercancel", up);
  el.addEventListener("lostpointercapture", up);
  el.addEventListener("keydown", (e) => {
    if ((e.key === " " || e.key === "Enter") && !e.repeat) {
      e.preventDefault(); press(spec.index, true);
    }
  });
  el.addEventListener("keyup", (e) => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); up(); }
  });
  el.addEventListener("blur", up);
  return el;
}

/* ------------------------------------------------------------------- T-bar */

function tbarFraction() {
  const { min, max } = layout.tbar;
  return Math.min(1, Math.max(0, (tbar.raw - min) / (max - min)));
}

function paintTbar() {
  const f = tbarFraction();
  // clientHeight is in layout pixels; the chassis transform must not enter here
  const track = tbar.slot.clientHeight;
  tbar.lever.style.top = ((1 - f) * track) + "px";
  $("s-tbar").textContent =
    `${(f * 100).toFixed(1)}%   raw 0x${tbar.raw.toString(16).toUpperCase().padStart(4, "0")}`;
  tbar.el.querySelector(".value").textContent = (f * 100).toFixed(0) + "%";
}

function setTbarFromPointer(clientY) {
  const box = tbar.slot.getBoundingClientRect();
  const f = Math.min(1, Math.max(0, 1 - (clientY - box.top) / box.height));
  const { min, max } = layout.tbar;
  tbar.raw = Math.round(min + f * (max - min));
  paintTbar();
  send({ t: "tbar", raw: tbar.raw });
}

function nudgeTbar(delta) {
  const { min, max } = layout.tbar;
  const f = Math.min(1, Math.max(0, tbarFraction() + delta));
  tbar.raw = Math.round(min + f * (max - min));
  paintTbar();
  send({ t: "tbar", raw: tbar.raw });
}

function buildTbar(spec) {
  const el = document.createElement("div");
  el.className = "tbar";
  el.tabIndex = 0;
  el.style.cssText =
    `left:${spec.x}px;top:${spec.y}px;width:${spec.w}px;height:${spec.h}px`;
  el.innerHTML =
    '<div class="head"><span class="caption">T-Bar</span>' +
    '<span class="value">0%</span></div>' +
    '<div class="body"><div class="slot"><div class="lever"></div></div>' +
    '<div class="ticks"></div></div>';

  const ticks = el.querySelector(".ticks");
  for (const pct of [100, 75, 50, 25, 0]) {
    const t = document.createElement("span");
    t.textContent = pct;
    t.style.top = (100 - pct) + "%";
    ticks.appendChild(t);
  }

  tbar.el = el;
  tbar.slot = el.querySelector(".slot");
  tbar.lever = el.querySelector(".lever");
  tbar.raw = spec.min;

  el.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    tbar.dragging = true;
    setTbarFromPointer(e.clientY);
  });
  el.addEventListener("pointermove", (e) => {
    if (tbar.dragging) setTbarFromPointer(e.clientY);
  });
  const stop = () => { tbar.dragging = false; };
  el.addEventListener("pointerup", stop);
  el.addEventListener("pointercancel", stop);
  el.addEventListener("lostpointercapture", stop);
  el.addEventListener("keydown", (e) => {
    const step = { ArrowUp: 0.01, ArrowDown: -0.01,
                   PageUp: 0.1, PageDown: -0.1 }[e.key];
    if (step !== undefined) { e.preventDefault(); nudgeTbar(step); }
    else if (e.key === "Home") { e.preventDefault(); nudgeTbar(-1); }
    else if (e.key === "End") { e.preventDefault(); nudgeTbar(1); }
  });
  return el;
}

/* ------------------------------------------------------------------ build */

function build() {
  const field = $("field");
  field.style.width = layout.width + "px";
  field.style.height = layout.height + "px";
  field.style.setProperty("--cap-inset", layout.cell.inset + "px");
  field.style.setProperty("--lcd-h", layout.cell.ph + "px");
  field.replaceChildren();

  for (const g of layout.groups) {
    const el = document.createElement("div");
    el.className = "group-label";
    el.style.cssText = `left:${g.x}px;top:${g.y}px;width:${g.w}px;height:${g.h}px`;
    el.textContent = g.label;
    field.appendChild(el);
  }

  const strip = layout.scale_strip;
  strip.labels.forEach((text, i) => {
    const el = document.createElement("div");
    el.className = "scale-num";
    el.style.cssText =
      `left:${strip.x + i * strip.pitch}px;top:${strip.y}px;width:${strip.w}px`;
    el.textContent = text;
    field.appendChild(el);
  });

  for (const d of layout.displays) {
    const el = document.createElement("div");
    el.className = "display";
    el.style.cssText =
      `left:${d.x}px;top:${d.y}px;width:${d.w}px;height:${d.h}px`;
    el.title = `cell ${d.cell} - ${d.label}, no key under it`;
    el.appendChild(makeLcd(d.cell));
    field.appendChild(el);
  }

  for (const k of layout.keys) field.appendChild(buildKey(k));
  field.appendChild(buildTbar(layout.tbar));

  buildQuirks();
  fit();
}

function buildQuirks() {
  const list = $("quirks");
  list.replaceChildren();
  for (const name of QUIRK_ORDER) {
    const [what, why] = QUIRK_TEXT[name];
    const li = document.createElement("li");
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = true;
    box.dataset.quirk = name;
    box.addEventListener("change", () =>
      send({ t: "quirk", name, on: box.checked }));
    const text = document.createElement("span");
    text.innerHTML =
      `<span class="what"></span><span class="why"></span>`;
    text.querySelector(".what").textContent = what;
    text.querySelector(".why").textContent = why;
    label.append(box, text);
    li.appendChild(label);
    list.appendChild(li);
  }
}

function fit() {
  const chassis = $("chassis");
  chassis.style.transform = "scale(1)";
  const natural = chassis.offsetWidth;
  const room = $("stage").clientWidth - 4;
  const next = Math.min(1, room / natural);
  if (Math.abs(next - scale) > 0.001) {
    scale = next;
    for (const cell of cells.keys()) drawCell(cell);
  }
  chassis.style.transform = `scale(${scale})`;
  $("stage").style.height = (chassis.offsetHeight * scale + 8) + "px";
  paintTbar();
}

/* ----------------------------------------------------------------- state */

function applyState(s) {
  if (s.cells) {
    for (const [cell, b64] of Object.entries(s.cells)) {
      const c = cells.get(+cell);
      if (!c) continue;
      const bin = atob(b64);
      const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      c.bits = out;
      c.dirty = true;
    }
  }
  if (s.colours) {
    for (const [cell, value] of Object.entries(s.colours)) {
      const c = cells.get(+cell);
      if (c) { c.colour = value; c.dirty = true; }
    }
  }
  for (const [cell, c] of cells) if (c.dirty) { c.dirty = false; drawCell(cell); }

  if (s.leds) {
    for (const [index, state] of Object.entries(s.leds)) {
      const k = keys.get(+index);
      if (!k || !k.led) continue;
      k.led.className = "led" + (state === 1 ? " red" : state === 2 ? " green" : "");
    }
  }

  if (s.tbar !== undefined && !tbar.dragging && s.tbar !== tbar.raw) {
    tbar.raw = s.tbar;
    paintTbar();
  }

  if (s.link) {
    const up = s.link.connected;
    $("link").classList.toggle("up", up);
    $("linktext").textContent = up
      ? `host attached from ${s.link.peer}`
      : "no host attached";
    $("s-controller").textContent = up ? s.link.peer : "waiting for a host";
  }
  if (s.initialised !== undefined) {
    $("s-init").textContent = s.initialised
      ? "set up" : "not set up - send --init";
    $("s-init").className = s.initialised ? "" : "warn";
  }
  if (s.banks_agree !== undefined) {
    $("s-banks").textContent = s.banks_agree
      ? "in step" : "out of step - commit twice";
    $("s-banks").className = s.banks_agree ? "" : "warn";
  }
  if (s.held) {
    $("s-held").textContent = s.held.length
      ? s.held.map((i) => layout.byIndex[i] || i).join(", ") : "—";
  }
  if (s.stats) {
    $("st-frames").textContent = s.stats.frames;
    $("st-commits").textContent = s.stats.commits;
    $("st-reads").textContent = s.stats.reads;
  }
  if (s.fifo !== undefined) $("st-fifo").textContent = s.fifo;
  if (s.quirks) {
    for (const box of document.querySelectorAll("[data-quirk]")) {
      box.checked = !!s.quirks[box.dataset.quirk];
    }
  }
  if (s.log && s.log.length) appendLog(s.log);
}

function appendLog(lines) {
  const log = $("log");
  for (const line of lines) {
    const li = document.createElement("li");
    li.textContent = line;
    if (/discard|refus|bad |without/.test(line)) li.className = "warn";
    log.insertBefore(li, log.firstChild);
  }
  while (log.children.length > 120) log.removeChild(log.lastChild);
}

/* ------------------------------------------------------------------ link */

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.t === "hello") {
      layout = msg.layout;
      layout.byIndex = msg.buttons;
      build();
      applyState(msg.state);
    } else if (msg.t === "state") {
      applyState(msg.state);
    }
  };
  ws.onclose = () => {
    $("link").classList.remove("up");
    $("linktext").textContent = "emulator not running";
    setTimeout(connect, 1000);
  };
  ws.onerror = () => ws.close();
}

$("power").addEventListener("click", () => send({ t: "power" }));
window.addEventListener("resize", fit);
window.addEventListener("contextmenu", (e) => {
  if (e.target.closest(".key, .tbar")) e.preventDefault();
});
connect();
