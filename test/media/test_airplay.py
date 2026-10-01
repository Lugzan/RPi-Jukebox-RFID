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
    AirPlayMediaSource, AirPlayMediaMonitor, ShairportDBusTransport, SERVICE, PATH, REMOTE, REMOTE_COMMANDS,
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
    assert asyncio.run(transport.snapshot()) == (':1.42', True, True, {
        'protocol': None, 'can_command': False, 'available': False,
        'player_state': None, 'client': '',
    })
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


def classic(**changes):
    properties = dict(protocol='AirPlay', available=True, can_command=True,
                      player_state='Playing', client='192.0.2.1')
    properties.update(changes)
    return properties


@pytest.mark.parametrize('command,dacp_request', [
    ('play', 'play'), ('pause', 'pause'), ('toggle', 'playpause'),
    ('next', 'nextitem'), ('previous', 'previtem'),
])
def test_classic_commands_are_routed_to_receiver(setup, command, dacp_request):
    router, mpd, source, transport = setup
    source.update(':1.1', True, remote=classic())
    assert source.capabilities == frozenset((*REMOTE_COMMANDS, 'stop'))
    assert router._dispatch(command) is True
    transport.remote_command.assert_called_once_with(':1.1', '192.0.2.1', dacp_request)
    transport.drop_session.assert_not_called()
    assert mpd.commands == ['stop']


@pytest.mark.parametrize('changes', [
    {'protocol': 'AirPlay 2'}, {'protocol': None}, {'protocol': 'Future AirPlay'},
    {'available': False}, {'available': 'true'}, {'can_command': False},
])
def test_remote_controls_are_gated_even_when_audio_is_active(setup, changes):
    router, mpd, source, transport = setup
    source.update(':1.1', True, remote=classic(**changes))
    assert source.capabilities == frozenset(('stop',))
    assert source.invoke('toggle') is False
    transport.remote_command.assert_not_called()
    assert router.get_active_source() == 'airplay'


def test_confirmed_pause_retains_routing_beyond_audio_timeout(setup):
    router, mpd, source, transport = setup
    source.update(':1.1', True, remote=classic())
    assert router.pause() is True
    assert source.get_status()['state'] == 'playing'  # acknowledgement is not state
    source.update(':1.1', True, remote=classic(player_state='Paused'))
    source.update(':1.1', False, remote=classic(player_state='Paused'))
    assert router.get_active_source() == 'airplay'
    assert source.get_status() == {'state': 'paused', 'has_media': True}
    assert router.toggle() is True
    assert transport.remote_command.call_args.args == (':1.1', '192.0.2.1', 'playpause')
    source.update(':1.1', True, remote=classic())
    assert mpd.commands == ['stop']


@pytest.mark.parametrize('player_state', ['Playing', 'Paused', 'Stopped'])
def test_idle_remote_does_not_claim_even_with_available_controls(setup, player_state):
    router, mpd, source, transport = setup
    source.update(':1.1', False, remote=classic(player_state=player_state))
    assert router.get_active_source() == 'mpd'
    assert not source.capabilities
    assert mpd.commands == []


@pytest.mark.parametrize('changes', [
    {'available': False}, {'client': '192.0.2.2'}, {'client': ''},
    {'player_state': 'Stopped'}, {'player_state': 'Not Available'}, {'protocol': 'AirPlay 2'},
])
def test_paused_session_releases_when_no_longer_resumable(setup, changes):
    router, mpd, source, transport = setup
    source.update(':1.1', True, remote=classic())
    source.update(':1.1', False, remote=classic(player_state='Paused'))
    paused = classic(player_state='Paused')
    paused.update(changes)
    source.update(':1.1', False, remote=paused)
    assert router.get_active_source() == 'mpd'
    assert mpd.commands == ['stop']
    assert not source.capabilities


def test_stop_or_handoff_from_pause_does_not_retain_stale_pause(setup):
    router, mpd, source, transport = setup
    source.update(':1.1', True, remote=classic())
    source.update(':1.1', False, remote=classic(player_state='Paused'))
    router.claim_source('mpd')
    transport.drop_session.assert_called_once_with(':1.1')
    source.update(':1.1', False, remote=classic(player_state='Paused'))
    assert router.get_active_source() == 'mpd'
    assert not source.capabilities
    source.update(':1.1', True, remote=classic())
    assert router.get_active_source() == 'airplay'


def test_remote_failure_is_logged_without_fallback_or_fabricated_state(setup, caplog):
    router, mpd, source, transport = setup
    source.update(':1.1', True, remote=classic())
    transport.remote_command.side_effect = RuntimeError('DACP reply code 501')
    assert router.pause() is False
    assert source.get_status()['state'] == 'playing'
    assert router.get_active_source() == 'airplay'
    assert mpd.commands == ['stop']
    assert '501' in caplog.text


def test_availability_and_state_changes_are_published_without_reclaim():
    activity, status = MagicMock(), MagicMock()
    source = AirPlayMediaSource(activity_callback=activity, status_callback=status)
    source.update(':1.1', True, remote=classic(available=False))
    source.update(':1.1', True, remote=classic())
    source.update(':1.1', True, remote=classic(player_state='Paused'))
    source.update(':1.1', True, remote=classic(player_state='Paused'))
    activity.assert_called_once_with(True)
    assert status.call_count == 3


def remote_bus_transport(protocol='AirPlay', available=True, client='192.0.2.1', reply=204):
    from dbus_next import Variant
    transport = ShairportDBusTransport()
    bus = MagicMock()
    transport._connect = AsyncMock(return_value=bus)
    transport._call = AsyncMock(side_effect=[
        [Variant('s', protocol)],
        [{'Available': Variant('b', available), 'Client': Variant('s', client)}],
        [reply, ''],
    ])
    return transport, bus


def test_remote_command_checks_gates_and_sender_response():
    transport, bus = remote_bus_transport()
    transport.remote_command(':1.42', '192.0.2.1', 'playpause')
    assert transport._call.call_args.args == (bus, ':1.42', PATH, SERVICE, 'RemoteCommand', 's', ['playpause'])
    bus.disconnect.assert_called_once()


@pytest.mark.parametrize('code', [0, 400, 403, 404, 490, 494, 501, None, True, '204'])
def test_remote_command_rejects_failed_or_malformed_reply(code):
    transport, bus = remote_bus_transport(reply=code)
    with pytest.raises(RuntimeError, match='reply code'):
        transport.remote_command(':1.42', '192.0.2.1', 'nextitem')
    bus.disconnect.assert_called_once()


@pytest.mark.parametrize('changes', [
    {'protocol': 'AirPlay 2'}, {'available': False}, {'client': '192.0.2.2'},
])
def test_command_time_recheck_prevents_stale_dispatch(changes):
    transport, bus = remote_bus_transport(**changes)
    with pytest.raises(RuntimeError, match='no longer available'):
        transport.remote_command(':1.42', '192.0.2.1', 'pause')
    assert transport._call.call_count == 2  # never sends RemoteCommand
    bus.disconnect.assert_called_once()


@pytest.mark.parametrize('remote_error', [False, True])
def test_snapshot_reads_remote_properties_without_losing_active_on_remote_error(remote_error):
    from dbus_next import Variant
    transport = ShairportDBusTransport()
    bus = MagicMock()
    bus.introspect = AsyncMock(return_value=SimpleNamespace(interfaces=[
        SimpleNamespace(name=SERVICE, methods=[SimpleNamespace(name=name)
                                              for name in ('DropSession', 'RemoteCommand')]),
        SimpleNamespace(name=REMOTE),
    ]))
    transport._connect = AsyncMock(return_value=bus)
    remote = {'Available': Variant('b', True), 'PlayerState': Variant('s', 'Playing'),
              'Client': Variant('s', '192.0.2.1')}
    transport._call = AsyncMock(side_effect=[
        [':1.42'], [{'Active': Variant('b', True), 'Protocol': Variant('s', 'AirPlay')}],
        RuntimeError('remote unavailable') if remote_error else [remote],
    ])
    snapshot = asyncio.run(transport.snapshot())
    assert snapshot[:3] == (':1.42', True, True)
    assert snapshot[3]['available'] is not remote_error
    assert snapshot[3]['can_command'] is True
    assert snapshot[3]['protocol'] == 'AirPlay'


def test_remote_command_timeout_closes_connection_without_retry():
    transport, bus = remote_bus_transport()

    async def hang(*args):
        await asyncio.sleep(60)

    transport._call = AsyncMock(side_effect=hang)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(transport._remote_command(':1.42', '192.0.2.1', 'playpause'), timeout=0.01))
    bus.disconnect.assert_called_once()
    transport._call.assert_awaited_once()


def test_stop_during_activity_does_not_reenable_remote_on_late_metadata(setup):
    router, mpd, source, transport = setup
    source.update(':1.1', True)  # receiver activity arrives before client metadata
    router.claim_source('mpd')
    source.update(':1.1', True, remote=classic(player_state='Paused'))
    assert not source.capabilities
    source.update(':1.1', False, remote=classic(player_state='Paused'))
    assert router.get_active_source() == 'mpd'
    assert not source.get_status()['has_media']
