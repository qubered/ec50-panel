# ec50

Driver for the Barco EC-50 Event Controller's USB control surface — 36 Assign
keys in a 3×12 grid, each with a 64×32 display and a red/green LED, 82 buttons
in total, and a 16-bit T-bar. No Barco software required.

## Install

```
pip install ftd2xx     # Windows
pip install pyftdi     # Linux and macOS
```

On Linux, install the udev rule so libusb can reach the device, then re-plug:

```
sudo cp 71-barcoipusb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Use

```python
from ec50 import EC50, Colour, Led

with EC50.open() as panel:
    panel.clear()
    panel.text(0, 0, "CAM 1", Colour.GREEN)
    panel.led_at(0, 0, Led.GREEN)
    panel.flush()

    for ev in panel.events():
        print(ev, panel.tbar)
```

Output is buffered — `set_*` calls mutate a local framebuffer and nothing
reaches the panel until `flush()`. Input is a queue: each `poll()` drains the
key FIFO, so events are edges and never need debouncing.

## Platform notes

The protocol is plain FTDI MPSSE and is identical everywhere. Only the USB
transport differs, and it is chosen automatically:

| | Backend | Why |
|---|---|---|
| Windows | `d2xx` | Barco's installer binds `ftdibus.sys` to VID 0x0600. Using D2XX coexists with their stack. **Never run Zadig on this device** — it would unbind their driver and stop the Toolset seeing the panel. |
| Linux | `pyftdi` | Nothing in the kernel claims VID 0x0600 (it is absent from `ftdi_sio`), so libusb gets the device directly once udev grants access. |
| macOS | `pyftdi` | You may need to unload Apple's `AppleUSBFTDI` first. |

Force one with `EC50.open(backend="pyftdi")`.

## Quirks worth knowing

**Nothing displays until it is latched.** Register `0x3828` is a mask: bit 0
commits the LED registers, bit 1 commits the framebuffer. Writes without the
latch are accepted and silently ignored. `flush()` handles both.

**The right half of each display is skewed one row.** Two 32px-wide driver
chips with a one-row addressing offset; Barco compensates in software and so
does this package. Text straddling x=32 shows a visible 1px step without it.
Disable with `EC50.open(skew=0)`.

**Bit 0 of each byte is the leftmost pixel**, not bit 7.

**The four header bytes overlay cell 0's first scanline.** Zeroing them makes
the panel discard the entire write. Handled internally.

**LED codes are an enum, not bit flags** — 0 off, 1 red, 2 green, 3 also off.
There is no amber.
