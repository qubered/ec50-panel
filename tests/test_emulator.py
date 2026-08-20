"""The emulator, checked against every claim in docs/PROTOCOL.md.

Needs no hardware and no packages beyond the standard library:

    python -m pytest tests/ -q          or      python tests/test_emulator.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ec50 import EC50, Colour, Led, protocol as P          # noqa: E402
from ec50.emulator import LAYOUT, Emulator, loopback       # noqa: E402
from ec50.transport import TransportError, parse_controller  # noqa: E402


def pixels(dev, cell):
    """A cell as a set of lit (x, y), read off the emulator's glass."""
    data = dev.cell_pixels(cell)
    return {(x, y)
            for y in range(P.CELL_H) for x in range(P.CELL_W)
            if (data[y * P.CELL_STRIDE + (x >> 3)] >> (x & 7)) & 1}


# -- the display -------------------------------------------------------------

def test_text_and_colour_reach_the_glass():
    panel, dev = loopback()
    panel.clear()
    panel.text(0, 0, "CAM 1", Colour.GREEN)
    panel.flush()
    cell = P.ASSIGN[0][0]
    assert dev.cell_colour(cell) == Colour.GREEN
    assert len(pixels(dev, cell)) > 50
    assert dev.cell_colour(P.ASSIGN[2][11]) == Colour.OFF


def test_every_cell_is_addressable_and_independent():
    panel, dev = loopback()
    panel.clear()
    for cell in range(P.NUM_CELLS):
        panel.set_colour(cell, P.colour(cell % 4, (cell // 4) % 4, (cell // 8) % 4))
    panel.flush()
    for cell in range(P.NUM_CELLS):
        assert dev.cell_colour(cell) == P.colour(
            cell % 4, (cell // 4) % 4, (cell // 8) % 4), cell


def test_low_bits_gate_colour_on():
    """RRGGBBII - clear either I bit and the panel falls back to white."""
    panel, dev = loopback()
    panel.clear()
    panel.set_colour(3, 0xC0)               # red, but II = 00
    panel.flush()
    assert dev.cell_colour(3) == Colour.WHITE


def test_bit_zero_is_the_leftmost_pixel():
    panel, dev = loopback()
    panel.clear()
    panel.set_pixel(5, 0, 3)                # x = 0 is the left edge
    panel.flush()
    assert (0, 3) in pixels(dev, 5)
    assert (7, 3) not in pixels(dev, 5)


def test_the_driver_cancels_the_right_half_skew():
    """A straight line across x=32 has to come out straight."""
    panel, dev = loopback()
    panel.clear()
    for x in range(P.CELL_W):
        panel.set_pixel(7, x, 16)
    panel.flush()
    lit = pixels(dev, 7)
    assert all((x, 16) in lit for x in range(P.CELL_W)), "the line stepped"


def test_without_compensation_the_right_half_steps_down():
    panel, dev = loopback(skew=0)            # write raw, as Barco's chips see it
    panel.clear()
    for x in range(P.CELL_W):
        panel.set_pixel(7, x, 16)
    panel.flush()
    lit = pixels(dev, 7)
    assert (0, 16) in lit and (63, 17) in lit and (63, 16) not in lit


def test_the_header_overlays_cell_zero(self=None):
    """The four transport header bytes land on cell 0's first scanline."""
    panel, dev = loopback()
    panel.clear()
    panel.flush()
    first_row = {(x, y) for (x, y) in pixels(dev, 0) if y == 0}
    assert first_row, "cell 0's top row should carry the header bytes"


# -- latching ----------------------------------------------------------------

def test_nothing_shows_until_it_is_latched():
    panel, dev = loopback()
    panel.clear()
    panel.flush()
    before = pixels(dev, P.ASSIGN[0][0])

    panel.text(0, 0, "NOPE", Colour.RED)
    panel.io.write(panel._frame(bytes(panel._buf)))     # write, do not commit
    assert pixels(dev, P.ASSIGN[0][0]) == before

    panel.write_reg(P.REG_COMMIT, P.COMMIT_LCD)
    assert pixels(dev, P.ASSIGN[0][0]) != before


def test_leds_need_their_own_latch_bit():
    panel, dev = loopback()
    index = P.ASSIGN_INDEX[0][0]
    panel.set_led(index, Led.GREEN)
    panel.flush(display=False, leds=False)
    for reg, value in panel._leds.items():
        panel.write_reg(reg, value)
    assert dev.led(index) == Led.OFF, "LED registers are not live until latched"
    panel.write_reg(P.REG_COMMIT, P.COMMIT_LEDS)
    assert dev.led(index) == Led.GREEN


def test_one_commit_leaves_the_banks_out_of_step():
    panel, dev = loopback()
    panel.clear()
    panel.text(1, 1, "A", Colour.WHITE)
    panel.io.write(panel._frame(bytes(panel._buf)))
    panel.write_reg(P.REG_COMMIT, P.COMMIT_LCD)
    assert not dev.banks_agree
    panel.flush()                            # flush() commits twice
    assert dev.banks_agree


def test_a_zeroed_header_discards_the_whole_write():
    panel, dev = loopback()
    panel.clear()
    panel.flush()
    frames = dev.stats["frames"]
    panel.text(0, 1, "GONE", Colour.WHITE)
    panel._buf[0:4] = b"\0\0\0\0"
    panel.flush()
    assert dev.stats["ignored"] == 2 and dev.stats["frames"] == frames + 2
    assert not pixels(dev, P.ASSIGN[0][1])


# -- LEDs --------------------------------------------------------------------

def test_every_led_is_reachable_and_distinct():
    panel, dev = loopback()
    with_leds = [i for i, v in P.BUTTON_INFO.items() if v[2] != 0xFFFF]
    assert len(with_leds) == 72
    for index in with_leds:
        panel.set_led(index, Led.RED)
    panel.flush()
    assert all(dev.led(i) == Led.RED for i in with_leds)

    for index in with_leds:                  # one at a time, nothing bleeds
        for reg in panel._leds:
            panel._leds[reg] = 0
        panel.set_led(index, Led.GREEN)
        panel.flush(display=False)
        lit = [i for i in with_leds if dev.led(i) != Led.OFF]
        assert lit == [index], f"{P.BUTTONS[index]} lit {lit}"


def test_row_zero_led_bits_run_backwards():
    """Generating these with a loop mirrors row 0 in groups of four."""
    row0 = [P.BUTTON_INFO[i][3] for i in P.ASSIGN_INDEX[0][:4]]
    row1 = [P.BUTTON_INFO[i][3] for i in P.ASSIGN_INDEX[1][:4]]
    assert row0 == sorted(row0, reverse=True)
    assert row1 == sorted(row1)


# -- input -------------------------------------------------------------------

def test_key_events_are_edges_in_order_and_nothing_is_dropped():
    panel, dev = loopback()
    order = [48, 95, 12, 80]
    for index in order:
        dev.press(index)
    for index in reversed(order):
        dev.release(index)

    seen = []
    for _ in range(20):
        seen += panel.poll()
    assert [e.index for e in seen] == order + list(reversed(order))
    assert [e.pressed for e in seen] == [True] * 4 + [False] * 4
    assert panel.held == set()


def test_assign_events_carry_their_grid_position():
    panel, dev = loopback()
    dev.press(P.ASSIGN_INDEX[2][7])
    events = []
    while not events:
        events = panel.poll()
    ev = events[0]
    assert (ev.row, ev.col) == (2, 7) and ev.is_assign
    assert str(ev).endswith("(R3C8)")


def test_a_read_returns_the_previous_transaction():
    panel, dev = loopback()
    dev.press(48)
    first = panel.read_reg(P.REG_KEYS)
    second = panel.read_reg(P.REG_KEYS)
    assert first[P.KEY_BYTE] == P.KEY_IDLE, "the first read should be stale"
    assert second[P.KEY_BYTE] == 0x80 | 48


def test_reply_lag_can_be_switched_off():
    panel, dev = loopback()
    dev.quirks["reply_lag"] = False
    dev.press(48)
    assert panel.read_reg(P.REG_KEYS)[P.KEY_BYTE] == 0x80 | 48


def test_the_tbar_spans_its_full_travel():
    panel, dev = loopback()
    for want in (0.0, 0.25, 0.5, 1.0):
        dev.set_tbar(want)
        for _ in range(40):                  # let the driver's smoothing settle
            panel.poll()
        assert abs(panel.tbar - want) < 0.01, (want, panel.tbar)
    assert P.tbar_percent(P.TBAR_MIN) == 0.0
    assert P.tbar_percent(P.TBAR_MAX) == 100.0


def test_the_tbar_lives_in_the_same_read_as_the_keys():
    panel, dev = loopback()
    dev.set_tbar(1.0)
    dev.press(80)
    panel.poll()
    reply = panel.read_reg(P.REG_KEYS)
    assert (reply[P.TBAR_HI] << 8 | reply[P.TBAR_LO]) == P.TBAR_MAX


# -- the panel as a whole ----------------------------------------------------

def test_power_cycle_forgets_the_controller_setup():
    panel, dev = loopback()
    panel.init_controllers()
    assert dev.initialised
    dev.power_cycle()
    assert not dev.initialised and dev.banks_agree
    panel.init_controllers()
    assert dev.initialised


def test_the_layout_places_every_button_and_every_cell_once():
    indices = [k["index"] for k in LAYOUT["keys"]]
    assert sorted(indices) == sorted(P.BUTTON_INFO)
    cells = [k["cell"] for k in LAYOUT["keys"] if k["cell"] is not None]
    cells += [d["cell"] for d in LAYOUT["displays"]]
    assert sorted(cells) == list(range(P.NUM_CELLS))
    assert len(indices) == 82


def test_unknown_mpsse_opcodes_are_answered_like_the_chip_does():
    _, dev = loopback()
    assert dev.feed(bytes([0x13])) == bytes([0xFA, 0x13])


# -- the network transport ---------------------------------------------------

def test_controller_addresses_parse():
    assert parse_controller("10.0.0.5:9999") == ("10.0.0.5", 9999)
    assert parse_controller("10.0.0.5") == ("10.0.0.5", 16650)
    assert parse_controller(":123") == ("127.0.0.1", 123)
    assert parse_controller(None) == ("127.0.0.1", 16650)


def test_the_panel_accepts_one_host_at_a_time():
    emu = Emulator(device_port=16991, web_port=8991).start()
    try:
        time.sleep(0.2)
        first = EC50.open(controller="127.0.0.1:16991")
        try:
            EC50.open(controller="127.0.0.1:16991")
            raise AssertionError("a second host was let in")
        except TransportError as e:
            assert "one at a time" in str(e)
        first.close()
        time.sleep(0.2)
        EC50.open(controller="127.0.0.1:16991").close()   # free again
    finally:
        emu.stop()


def test_the_wire_is_the_same_over_tcp_as_in_process():
    emu = Emulator(device_port=16992, web_port=8992).start()
    try:
        time.sleep(0.2)
        panel = EC50.open(controller="127.0.0.1:16992")
        panel.clear()
        panel.text(1, 2, "NET", Colour.CYAN)
        panel.led_at(1, 2, Led.GREEN)
        panel.flush()
        time.sleep(0.2)
        cell = P.ASSIGN[1][2]
        assert emu.dev.cell_colour(cell) == Colour.CYAN
        assert emu.dev.led(P.ASSIGN_INDEX[1][2]) == Led.GREEN
        assert len(pixels(emu.dev, cell)) > 30
        panel.close()
    finally:
        emu.stop()


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
