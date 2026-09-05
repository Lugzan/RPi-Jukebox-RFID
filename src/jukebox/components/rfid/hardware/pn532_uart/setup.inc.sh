#!/usr/bin/env bash

# A USB-to-UART device is normally owned by the dialout group. Do not enable
# the Raspberry Pi GPIO UART or change Bluetooth settings: this reader uses a
# USB serial adapter only.
CURRENT_USER="${SUDO_USER:-$(whoami)}"

if id -nG "$CURRENT_USER" | tr ' ' '\n' | grep -qx dialout; then
    echo "User '$CURRENT_USER' already belongs to the dialout group."
else
    echo "Adding '$CURRENT_USER' to the dialout group for USB serial access."
    sudo usermod -aG dialout "$CURRENT_USER"
    echo "Log out and back in (or reboot) before starting the Jukebox service."
fi
