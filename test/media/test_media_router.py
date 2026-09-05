import sys
from unittest.mock import MagicMock

import jukebox.plugs as plugin


allow_direct_imports = plugin.ALLOW_DIRECT_IMPORTS
plugin.ALLOW_DIRECT_IMPORTS = True
sys.modules['jukebox.publishing'] = MagicMock()

from components.media import MediaRouter  # noqa: E402
from components.media import MediaSource  # noqa: E402

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
