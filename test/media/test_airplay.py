"""AirPlay routing tests; no receiver, system bus or audio hardware required."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import jukebox.plugs as plugin

allow_direct_imports = plugin.ALLOW_DIRECT_IMPORTS
plugin.ALLOW_DIRECT_IMPORTS = True
sys.modules['jukebox.publishing'] = MagicMock()
from components.media import MediaRouter, MediaSource  # noqa: E402
from components.media.airplay import (  # noqa: E402
    AirPlayMediaSource, AirPlayMediaMonitor, ShairportDBusTransport, SERVICE, PATH,
)
plugin.ALLOW_DIRECT_IMPORTS = allow_direct_imports


class LocalSource(MediaSource):
    def __init__(self, name='mpd'):
        super().__init__(name, name, ('stop', 'next'))
        self.commands = []

    def invoke(self, command):
        self.commands.append(command)

    def get_status(self):
        return {'state': 'stopped'}


@pytest.fixture
def setup():
    router = MediaRouter()
    mpd = LocalSource()
    transport = MagicMock()
    source = AirPlayMediaSource(transport, lambda active: (
        router.claim_source('airplay') if active else router.release_source('airplay')))
    router.register_source(mpd)
    router.register_source(source)
    return router, mpd, source, transport


def test_idle_then_active_then_inactive(setup):
    router, mpd, source, transport = setup
    source.update(':1.1', False)
    assert router.get_active_source() == 'mpd'
    assert source.get_status() == {'state': 'stopped', 'has_media': False}
    source.update(':1.1', True)
    source.update(':1.1', True)
    assert mpd.commands == ['stop']
    assert router.get_status()['capabilities'] == ['stop']
    assert router.get_status()['state'] == 'playing'
    source.update(':1.1', False)
    assert router.get_active_source() == 'mpd'
    assert mpd.commands == ['stop']  # no automatic resume
    source.update(':1.1', True)
    assert mpd.commands == ['stop', 'stop']


@pytest.mark.parametrize('command', ['play', 'pause', 'toggle', 'next', 'previous'])
def test_unsupported_controls_do_not_reach_receiver_or_mpd(setup, command, caplog):
    router, mpd, source, transport = setup
    source.update(':1.1', True)
    assert source.invoke(command) is False
    router._dispatch(command)
    transport.drop_session.assert_not_called()
    assert mpd.commands == ['stop']
    assert command in caplog.text


def test_handoff_drops_airplay_and_does_not_reclaim_during_grace(setup):
    router, mpd, source, transport = setup
    source.update(':1.1', True)
    router.claim_source('mpd')
    transport.drop_session.assert_called_once_with(':1.1')
    source.update(':1.1', True)
    assert router.get_active_source() == 'mpd'
    source.update(':1.1', False)
    source.update(':1.1', True)
    assert router.get_active_source() == 'airplay'


def test_loss_releases_and_receiver_restart_claims(setup):
    router, mpd, source, transport = setup
    source.update(':1.1', True)
    source.disconnected()
    assert router.get_active_source() == 'mpd'
    assert source.get_status()['state'] == 'unavailable'
    assert not source.capabilities
    source.update(':1.2', True)
    assert router.get_active_source() == 'airplay'
    router.claim_source('mpd')
    source.update(':1.3', True)  # restart between polls
    assert router.get_active_source() == 'airplay'


def test_failed_stop_is_safe_and_does_not_report_success(setup, caplog):
    router, mpd, source, transport = setup
    source.update(':1.1', True)
    transport.drop_session.side_effect = RuntimeError('access denied')
    assert router.stop() is False
    assert router.get_active_source() == 'airplay'
    assert 'access denied' in caplog.text


def test_missing_drop_session_is_not_advertised(setup):
    router, mpd, source, transport = setup
    source.update(':1.1', True, can_stop=False)
    assert source.capabilities == frozenset()
    assert source.invoke('stop') is False
    transport.drop_session.assert_not_called()


def test_native_snapshot_unwraps_variant_and_pins_unique_owner():
    from dbus_next import Variant
    transport = ShairportDBusTransport()
    bus = MagicMock()
    bus.introspect = AsyncMock(return_value=SimpleNamespace(interfaces=[
        SimpleNamespace(name=SERVICE, methods=[SimpleNamespace(name='DropSession')])]))
    transport._connect = AsyncMock(return_value=bus)
    transport._call = AsyncMock(side_effect=[[':1.42'], [{'Active': Variant('b', True)}]])
    assert asyncio.run(transport.snapshot()) == (':1.42', True, True)
    assert transport._call.call_args.args[1:3] == (':1.42', PATH)
    transport.close()
    bus.disconnect.assert_called_once()


def test_native_missing_active_fails_closed():
    transport = ShairportDBusTransport()
    transport.bus = MagicMock()
    transport.owner = ':1.1'
    transport._call = AsyncMock(side_effect=[[':1.1'], [{}]])
    with pytest.raises(KeyError):
        asyncio.run(transport.snapshot())


def test_drop_session_uses_receiver_interface_and_closes_on_error():
    transport = ShairportDBusTransport()
    bus = MagicMock()
    transport._connect = AsyncMock(return_value=bus)
    transport._call = AsyncMock(side_effect=RuntimeError('gone'))
    with pytest.raises(RuntimeError, match='gone'):
        transport.drop_session(':1.7')
    transport._call.assert_awaited_once_with(bus, ':1.7', PATH, SERVICE, 'DropSession')
    bus.disconnect.assert_called_once()


def test_monitor_recovers_from_bus_loss_and_releases_on_shutdown(setup):
    router, mpd, source, transport = setup
    snapshots = iter([(':1.1', True, True), RuntimeError('bus lost'), (':1.2', True, True)])
    states = []

    async def snapshot():
        value = next(snapshots)
        if isinstance(value, Exception):
            raise value
        return value

    transport.snapshot = snapshot
    monitor = AirPlayMediaMonitor(source, transport)

    def wait(timeout):
        states.append(router.get_active_source())
        if len(states) == 3:
            monitor._stopped.set()

    monitor._stopped.wait = wait
    monitor._run()
    assert states == ['airplay', 'mpd', 'airplay']
    assert router.get_active_source() == 'mpd'
    assert transport.close.call_count == 2


@pytest.mark.parametrize('enabled', [False, True])
def test_initialization_is_opt_in(monkeypatch, enabled):
    import components.media as media
    import components.media.airplay as airplay
    config = MagicMock()
    config.setndefault.side_effect = lambda section, key, value: enabled if section == 'airplay_media' else False
    monitor = MagicMock()
    monkeypatch.setattr(media, 'cfg', config)
    monkeypatch.setattr(media, 'media_ctrl', None)
    monkeypatch.setattr(media, 'airplay_monitor', None)
    monkeypatch.setattr(airplay, 'AirPlayMediaMonitor', monitor)
    monkeypatch.setattr(plugin, 'register', MagicMock())
    media.initialize()
    config.setndefault.assert_any_call('airplay_media', 'enable', value=False)
    assert ('airplay' in media.media_ctrl.get_sources()) is enabled
    assert media.media_ctrl.get_active_source() == 'mpd'
    if enabled:
        monitor.return_value.start.assert_called_once()
        media.stop_airplay()
        monitor.return_value.stop.assert_called_once()
    else:
        monitor.assert_not_called()
