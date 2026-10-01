"""Failure traces and bounded logging for real-device diagnosis."""
import logging
import sys
from unittest.mock import MagicMock

import pytest
import jukebox.plugs as plugin

allow_direct_imports = plugin.ALLOW_DIRECT_IMPORTS
plugin.ALLOW_DIRECT_IMPORTS = True
sys.modules['jukebox.publishing'] = MagicMock()
from components.media import MediaRouter, MediaSource  # noqa: E402
from components.media.airplay import AirPlayMediaSource  # noqa: E402
from components.media.bluetooth import BluezMediaSource, BluezMediaMonitor  # noqa: E402
plugin.ALLOW_DIRECT_IMPORTS = allow_direct_imports


class FailingSource(MediaSource):
    def __init__(self):
        super().__init__('failing', 'Failing', ('toggle',))

    def invoke(self, command):
        raise OSError('transport disconnected')

    def get_status(self):
        return {'state': 'playing'}


def test_dispatch_failure_keeps_traceback_context_and_original_exception(caplog):
    caplog.set_level(logging.DEBUG, logger='jb.media')
    router = MediaRouter()
    router.register_source(FailingSource())
    with pytest.raises(OSError, match='transport disconnected'):
        router.toggle()
    failure = next(record for record in caplog.records if record.exc_info)
    assert 'source=failing command=toggle' in failure.getMessage()
    assert 'elapsed_ms=' in failure.getMessage()
    assert failure.exc_info[0] is OSError
    assert router.get_active_source() == 'failing'


def test_airplay_polling_logs_changes_without_repeating_identical_snapshots(caplog):
    caplog.set_level(logging.INFO, logger='jb.media.airplay')
    source = AirPlayMediaSource()
    remote = {'protocol': 'AirPlay', 'available': True, 'can_command': True,
              'player_state': 'Playing', 'client': 'private-client-address'}
    for _ in range(100):
        source.update(':1.1', True, remote=remote)
    source.update(':1.1', False, remote={**remote, 'player_state': 'Paused'})
    observations = [record for record in caplog.records if 'AirPlay observation:' in record.getMessage()]
    assert len(observations) == 2
    assert 'state=paused session=True' in observations[-1].getMessage()
    assert 'private-client-address' not in caplog.text


def test_bluetooth_logs_native_property_shape_without_track_metadata(caplog):
    from dbus_next import Variant
    caplog.set_level(logging.INFO, logger='jb.media.bluetooth')
    source = BluezMediaSource()
    source.update_player('/player', {'Status': Variant('s', 'playing'),
                                     'Track': Variant('a{sv}', {'Title': Variant('s', 'Private title')})})
    for position in range(100):
        source.update_player('/player', {'Position': Variant('u', position)})
    assert len(caplog.records) == 1
    assert 'Variant' in caplog.text
    assert 'Private title' not in caplog.text


def test_background_dbus_failure_goes_to_application_logger(caplog):
    try:
        raise RuntimeError('player disappeared during watch setup')
    except RuntimeError as error:
        BluezMediaMonitor._log_async_error(None, {'message': 'Task exception was never retrieved', 'exception': error})
    assert caplog.records[-1].exc_info[0] is RuntimeError
    assert 'player disappeared during watch setup' in caplog.text
