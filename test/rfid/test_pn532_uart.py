"""Tests for the PN532 HSU/UART reader without NFC hardware."""

import os
import sys
import types
import unittest


# The project imports from src/jukebox when run outside its normal launcher.
sys.path.append(os.path.abspath('src/jukebox'))


class FakeNfcError(Exception):
    pass


class FakeRemoteTarget:
    def __init__(self, brty):
        self.brty = brty


class FakeFrontend:
    def __init__(self, open_result=True, sense_results=None):
        self.open_result = open_result
        self.sense_results = sense_results if sense_results is not None else []
        self.open_paths = []
        self.closed = False

    def open(self, path):
        self.open_paths.append(path)
        return self.open_result

    def sense(self, *targets, **kwargs):
        self.targets = targets
        self.sense_kwargs = kwargs
        if self.sense_results:
            result = self.sense_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return None

    def close(self):
        self.closed = True


# nfcpy is optional for the development test environment. Provide its small
# public surface before importing the reader; individual tests replace its
# frontend and activation functions with transport fakes.
fake_nfc = types.ModuleType('nfc')
fake_nfc_clf = types.ModuleType('nfc.clf')
fake_nfc_clf.RemoteTarget = FakeRemoteTarget
fake_nfc_clf.Error = FakeNfcError
fake_nfc_tag = types.ModuleType('nfc.tag')
fake_nfc.ContactlessFrontend = FakeFrontend
fake_nfc.clf = fake_nfc_clf
fake_nfc.tag = fake_nfc_tag
sys.modules['nfc'] = fake_nfc
sys.modules['nfc.clf'] = fake_nfc_clf
sys.modules['nfc.tag'] = fake_nfc_tag

from components.rfid.hardware.pn532_uart import pn532_uart  # noqa: E402


class FakeTag:
    def __init__(self, identifier):
        self.identifier = identifier


class TestPn532Uart(unittest.TestCase):
    def setUp(self):
        self.frontends = []
        self.activate_results = []

        def frontend_factory():
            frontend = FakeFrontend(sense_results=self.sense_results)
            self.frontends.append(frontend)
            return frontend

        self.sense_results = []
        pn532_uart.nfc.ContactlessFrontend = frontend_factory
        pn532_uart.nfc.tag.activate = lambda clf, target: self.activate_results.pop(0)
        pn532_uart.cfg.config_dict({'rfid': {'readers': {'read_00': {
            'config': {'device': '/dev/ttyUSB0', 'log_all_cards': False}
        }}}})

    def test_uid_conversion_matches_existing_pn532_reader(self):
        self.assertEqual(pn532_uart.uid_to_card_id(bytearray.fromhex('04A2B3C4')), '77771716')
        self.assertEqual(pn532_uart.uid_to_card_id(bytearray.fromhex('0001')), '1')
        self.assertEqual(pn532_uart.uid_to_card_id(bytearray()), '')
        self.assertEqual(pn532_uart.uid_to_card_id(None), '')

    def test_initialization_reads_serial_device_from_config(self):
        reader = pn532_uart.ReaderClass('read_00')
        self.assertEqual(reader.device, '/dev/ttyUSB0')
        self.assertTrue(reader._keep_running)
        self.assertIsNone(reader.clf)

    def test_detects_iso14443a_card_and_returns_canonical_id(self):
        self.sense_results.append(object())
        self.activate_results.append(FakeTag(bytearray.fromhex('04A2B3C4')))
        reader = pn532_uart.ReaderClass('read_00')

        self.assertEqual(reader.read_card(), '77771716')
        self.assertEqual(self.frontends[0].open_paths, ['tty:USB0:pn532'])
        self.assertEqual(self.frontends[0].targets[0].brty, '106A')
        self.assertEqual(self.frontends[0].sense_kwargs, {'interval': 0.1, 'iterations': 1})

    def test_repeated_card_is_emitted_for_common_layer_suppression(self):
        # ReaderRunner, not hardware backends, owns same_id_delay. A placed
        # card is therefore intentionally returned on each successful scan.
        self.sense_results.extend([object(), object()])
        self.activate_results.extend([FakeTag(b'\x00\x2A'), FakeTag(b'\x00\x2A')])
        reader = pn532_uart.ReaderClass('read_00')

        self.assertEqual(reader.read_card(), '42')
        self.assertEqual(reader.read_card(), '42')

    def test_stop_and_cleanup_close_open_frontend(self):
        reader = pn532_uart.ReaderClass('read_00')
        self.assertEqual(reader.read_card(), '')
        frontend = self.frontends[0]

        reader.stop()
        self.assertEqual(reader.read_card(), '')
        reader.cleanup()
        self.assertTrue(frontend.closed)
        self.assertIsNone(reader.clf)

    def test_communication_error_disconnects_and_next_read_reconnects(self):
        self.sense_results.append(OSError('USB serial adapter disconnected'))
        reader = pn532_uart.ReaderClass('read_00')
        self.assertEqual(reader.read_card(), '')
        self.assertTrue(self.frontends[0].closed)
        self.assertIsNone(reader.clf)

        reader._next_reconnect = 0
        self.sense_results.append(object())
        self.activate_results.append(FakeTag(b'\x01'))
        self.assertEqual(reader.read_card(), '1')
        self.assertEqual(len(self.frontends), 2)

    def test_open_error_is_recoverable_and_releases_frontend(self):
        frontend = FakeFrontend()

        def raise_open_error(path):
            frontend.open_paths.append(path)
            raise OSError('CH340 unavailable')

        frontend.open = raise_open_error
        pn532_uart.nfc.ContactlessFrontend = lambda: frontend
        reader = pn532_uart.ReaderClass('read_00')

        self.assertEqual(reader.read_card(), '')
        self.assertTrue(frontend.closed)
        self.assertIsNone(reader.clf)

    def test_stable_by_id_path_is_resolved_before_opening(self):
        original_realpath = pn532_uart.os.path.realpath
        try:
            pn532_uart.os.path.realpath = lambda value: '/dev/ttyUSB7'
            self.assertEqual(
                pn532_uart.device_to_nfcpy_path('/dev/serial/by-id/usb-ch340'),
                'tty:USB7:pn532'
            )
        finally:
            pn532_uart.os.path.realpath = original_realpath


if __name__ == '__main__':
    unittest.main()
