# PN532 via USB/UART

This reader module supports a PN532 V2.0 configured for **HSU/UART** and connected through a USB-to-UART adapter such as CH340E:

```
NFC card → PN532 (HSU/UART) → CH340E USB-to-UART → Raspberry Pi USB
```

It uses [nfcpy](https://nfcpy.readthedocs.io/) and supports ISO14443A cards. It does not use GPIO UART, I²C, SPI, `/dev/serial0`, or Bluetooth. Choose **PN532 via USB/UART (CH340 and compatible adapters)** in the RFID reader configuration tool; **PN532 reader via I²C using py532 library** remains the separate GPIO I²C option.

## Setup

1. Set the PN532 board to HSU/UART mode according to its board documentation and wire its UART TX/RX/GND to the USB-UART adapter.
2. Connect the adapter to USB and run the RFID reader configuration tool. It installs `nfcpy` and adds the Jukebox user to the standard `dialout` group. Log out and back in (or reboot) after that group change.
3. Select the presented serial device. A `/dev/serial/by-id/...` entry is preferred. It is a stable udev symlink; `/dev/ttyUSB0` is supported but its number can change on reboot or reconnect.

The resulting `shared/settings/rfid.yaml` has the normal reader configuration; only the hardware-specific `device` value is needed:

```yaml
rfid:
  readers:
    read_00:
      module: pn532_uart
      config:
        device: /dev/serial/by-id/usb-1a86_USB2.0-Serial-if00-port0
```

The module resolves the stable symlink again on every reconnect before forming nfcpy's HSU path (for example `tty:USB0:pn532`). Temporary serial errors and device removal cause a retry instead of terminating the Jukebox daemon. Card IDs use the same unsigned big-endian decimal UID representation as the existing PN532 I²C reader, so card assignments are shared.
