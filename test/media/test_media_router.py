import sys
from unittest.mock import MagicMock

import jukebox.plugs as plugin


allow_direct_imports = plugin.ALLOW_DIRECT_IMPORTS
plugin.ALLOW_DIRECT_IMPORTS = True
sys.modules['jukebox.publishing'] = MagicMock()

from components.media import MediaRouter  # noqa: E402
from components.media import MediaSource  # noqa: E402
from components.media.bluetooth import BluezMediaMonitor  # noqa: E402
from components.media.bluetooth import BluezMediaSource  # noqa: E402

plugin.ALLOW_DIRECT_IMPORTS = allow_direct_imports


class FakeSource(MediaSource):
    def __init__(self, source_id, capabilities=('play', 'pause', 'toggle', 'next', 'previous', 'stop')):
        super().__init__(source_id, source_id.title(), capabilities)
        self.commands = []
        self.status = {'state': 'stopped', 'has_media': False}

    def invoke(self, command, *args, **kwargs):
        self.commands.append(command)

    def get_status(self):
        return self.status


def test_routes_commands_to_default_source():
    router = MediaRouter()
    mpd = FakeSource('mpd')
    router.register_source(mpd)

    router.next()
    router.prev()
    router.toggle()

    assert mpd.commands == ['next', 'previous', 'toggle']


def test_claim_stops_previous_source_and_switches_routing():
    router = MediaRouter()
    mpd = FakeSource('mpd')
    airplay = FakeSource('airplay')
    router.register_source(mpd)
    router.register_source(airplay)

    router.claim_source('airplay')
    router.next()

    assert mpd.commands == ['stop']
    assert airplay.commands == ['next']
    assert router.get_active_source() == 'airplay'


def test_status_is_normalized_and_reports_capabilities():
    router = MediaRouter()
    mpd = FakeSource('mpd', capabilities=('toggle',))
    mpd.status = {
        'state': 'play',
        'title': 'Track',
        'artist': 'Artist',
        'has_media': True,
    }
    router.register_source(mpd)

    assert router.get_status() == {
        'active_source': 'mpd',
        'active_source_name': 'Mpd',
        'state': 'playing',
        'capabilities': ['toggle'],
        'has_media': True,
        'title': 'Track',
        'artist': 'Artist',
    }


def test_unsupported_command_is_not_sent_to_source():
    router = MediaRouter()
    mpd = FakeSource('mpd', capabilities=('toggle',))
    router.register_source(mpd)

    router.next()

    assert mpd.commands == []


class FakeBluezTransport:
    def __init__(self):
        self.calls = []
        self.error = None

    def call(self, path, method):
        self.calls.append((path, method))
        if self.error is not None:
            raise self.error


class UnsupportedAvrcpMethod(Exception):
    type = 'org.bluez.Error.NotSupported'


def test_bluetooth_claims_on_playback_and_retains_a_paused_session():
    router = MediaRouter()
    mpd = FakeSource('mpd')
    bluetooth = BluezMediaSource()
    bluetooth.set_activity_callback(
        lambda active: router.claim_source('bluetooth') if active else router.release_source('bluetooth'))
    router.register_source(mpd)
    router.register_source(bluetooth)
    monitor = BluezMediaMonitor(bluetooth)

    # A discovered, but stopped, phone player must not steal the controls.
    monitor.process_managed_objects({
        '/org/bluez/hci0/dev_phone/player0': {
            'org.bluez.MediaPlayer1': {'Status': 'stopped', 'Name': 'Phone'},
        },
    })
    assert router.get_active_source() == 'mpd'

    monitor.properties_changed('/org/bluez/hci0/dev_phone/player0', 'org.bluez.MediaPlayer1', {
        'Status': 'playing',
    })
    assert router.get_active_source() == 'bluetooth'
    assert mpd.commands == ['stop']

    monitor.properties_changed('/org/bluez/hci0/dev_phone/player0', 'org.bluez.MediaPlayer1', {
        'Status': 'paused',
    })
    assert router.get_active_source() == 'bluetooth'
    assert 'toggle' in bluetooth.capabilities

    monitor.interfaces_removed('/org/bluez/hci0/dev_phone/player0', ['org.bluez.MediaPlayer1'])
    assert router.get_active_source() == 'mpd'


def test_bluetooth_transport_maps_status_metadata_and_avrcp_commands():
    transport = FakeBluezTransport()
    source = BluezMediaSource(transport)
    path = '/org/bluez/hci0/dev_phone/player0'
    source.update_player(path, {
        'Status': 'playing',
        'Position': 3250,
        'Track': {
            'Title': 'Track',
            'Artist': ['Artist'],
            'Album': 'Album',
            'Duration': 255000,
        },
    })

    source.invoke('previous')
    source.invoke('toggle')

    assert transport.calls == [(path, 'Previous'), (path, 'Pause')]
    assert source.get_status() == {
        'state': 'playing',
        'has_media': True,
        'title': 'Track',
        'artist': 'Artist',
        'album': 'Album',
        'position': 3.25,
        'duration': 255.0,
    }
    assert source.capabilities == frozenset(('play', 'pause', 'toggle', 'next', 'previous', 'stop'))


def test_unsupported_bluetooth_avrcp_method_is_suppressed_and_not_readvertised():
    transport = FakeBluezTransport()
    transport.error = UnsupportedAvrcpMethod()
    source = BluezMediaSource(transport)
    source.update_player('/player', {'Status': 'playing'})

    assert source.invoke('next') is None
    assert 'next' not in source.capabilities
