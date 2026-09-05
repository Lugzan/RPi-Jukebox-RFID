"""PN532 reader connected in HSU mode through a USB serial adapter."""

import glob
import logging
import os
import time

import nfc
from nfc.clf import RemoteTarget

from components.rfid import ReaderBaseClass
import jukebox.cfghandler
import misc.inputminus as pyil
from misc.simplecolors import Colors

from .description import DESCRIPTION


logger = logging.getLogger('jb.rfid.pn532_uart')
cfg = jukebox.cfghandler.get_handler('rfid')
RECONNECT_DELAY = 1.0


def uid_to_card_id(uid) -> str:
    """Convert a PN532 UID to Phoniebox's canonical decimal card ID.

    This deliberately mirrors ``pn532_i2c_py532``: UID bytes are treated as
    one unsigned, big-endian number. Keeping this conversion here means cards
    assigned with the existing PN532 I2C reader do not need to be reassigned.
    """
    try:
        uid_bytes = bytes(uid)
    except (TypeError, ValueError):
        return ''
    if not uid_bytes:
        return ''
    return str(int.from_bytes(uid_bytes, byteorder='big', signed=False))


def device_to_nfcpy_path(device: str) -> str:
    """Return nfcpy's HSU path for a configured Linux serial-device path.

    nfcpy accepts ``tty:USB0:pn532`` rather than a literal ``/dev`` path.
    Resolve a stable ``/dev/serial/by-id`` symlink *each time* a connection is
    opened, so it remains the configuration source even when its ttyUSB number
    changes after a reconnect.
    """
    if not isinstance(device, str) or not device.strip():
        raise ValueError("A non-empty serial device path is required")

    device = device.strip()
    if device.startswith('tty:'):
        path_parts = device.split(':')
        if len(path_parts) == 3 and path_parts[2] == 'pn532':
            return device
        raise ValueError("nfcpy device paths must use the PN532 driver (tty:<port>:pn532)")

    resolved_device = os.path.realpath(device)
    prefix = '/dev/tty'
    if not resolved_device.startswith(prefix):
        raise ValueError(f"Serial device '{device}' does not resolve to /dev/tty*")
    port = resolved_device[len(prefix):]
    if not port or '/' in port:
        raise ValueError(f"Serial device '{device}' does not resolve to a supported tty device")
    return f'tty:{port}:pn532'


def _serial_device_candidates() -> list[str]:
    """Return candidates in the order safest for a persistent configuration."""
    by_id = sorted(glob.glob('/dev/serial/by-id/*'))
    if by_id:
        return by_id
    return sorted(glob.glob('/dev/ttyUSB[0-9]*') + glob.glob('/dev/ttyACM[0-9]*'))


def query_customization() -> dict:
    """Ask the user to explicitly select the USB serial adapter to use."""
    candidates = _serial_device_candidates()
    print("\nPN532 USB/UART reader (HSU mode)\n")
    print("Select the serial device connected to the PN532. Entries below /dev/serial/by-id are preferred "
          "because ttyUSB numbers can change after a reboot or reconnect.\n")

    if not candidates:
        logger.error("No USB serial device found. Connect the CH340 adapter and run reader registration again.")
        return {'device': None, 'log_all_cards': False}

    for index, candidate in enumerate(candidates):
        print(f" {Colors.lightgreen}{index:2d}{Colors.reset}: {Colors.lightcyan}{candidate}{Colors.reset}")
    selected = pyil.input_int("Serial device number?", min=0, max=len(candidates) - 1,
                              prompt_color=Colors.lightgreen, prompt_hint=True)
    return {'device': candidates[selected], 'log_all_cards': False}


class ReaderClass(ReaderBaseClass):
    """Read ISO14443A card UIDs from a PN532 operating in HSU/UART mode."""

    def __init__(self, reader_cfg_key):
        self._logger = logging.getLogger(f'jb.rfid.pn532_uart({reader_cfg_key})')
        super().__init__(reader_cfg_key=reader_cfg_key, description=DESCRIPTION, logger=self._logger)

        with cfg:
            config = cfg.setndefault('rfid', 'readers', reader_cfg_key, 'config', value={})
            self.device = config.setdefault('device', None)
            self.log_all_cards = config.setdefault('log_all_cards', False)

        if not self.device:
            self._logger.error("Missing PN532 UART configuration value 'device'.")
        self.clf = None
        self._keep_running = True
        self._next_reconnect = 0.0

    def _disconnect(self):
        if self.clf is not None:
            try:
                self.clf.close()
            except (OSError, getattr(nfc.clf, 'Error', OSError)) as error:
                self._logger.debug(f"Error while closing PN532 UART reader: {error}")
            finally:
                self.clf = None

    def _connect(self) -> bool:
        if self.clf is not None:
            return True
        if not self.device or time.monotonic() < self._next_reconnect:
            return False

        self._next_reconnect = time.monotonic() + RECONNECT_DELAY
        clf = None
        try:
            nfcpy_path = device_to_nfcpy_path(self.device)
            clf = nfc.ContactlessFrontend()
            if not clf.open(nfcpy_path):
                self._logger.warning(f"Could not open PN532 UART reader at '{self.device}'. Will retry.")
                clf.close()
                return False
            self.clf = clf
            self._logger.info(f"Connected PN532 UART reader at '{self.device}' ({nfcpy_path}).")
            return True
        except (OSError, ValueError, getattr(nfc.clf, 'Error', OSError)) as error:
            if clf is not None:
                try:
                    clf.close()
                except (OSError, getattr(nfc.clf, 'Error', OSError)):
                    pass
            self._logger.warning(f"Could not open PN532 UART reader at '{self.device}': {error}. Will retry.")
            return False

    def cleanup(self):
        self._disconnect()

    def stop(self):
        self._keep_running = False

    def read_card(self) -> str:
        if not self._keep_running or not self._connect():
            return ''

        try:
            target = self.clf.sense(RemoteTarget('106A'), interval=0.1, iterations=1)
            if not target:
                return ''
            tag = nfc.tag.activate(self.clf, target)
            if not tag:
                return ''
            card_id = uid_to_card_id(tag.identifier)
            if self.log_all_cards and card_id:
                self._logger.debug(f"Card detected with ID = '{card_id}'")
            return card_id
        except (OSError, getattr(nfc.clf, 'Error', OSError)) as error:
            self._logger.warning(f"PN532 UART communication error: {error}. Reconnecting.")
            self._disconnect()
            return ''
