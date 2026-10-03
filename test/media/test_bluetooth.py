"""Native D-Bus values, lifecycle recovery and concurrent transport regression tests."""
import asyncio
import sys
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from dbus_next import DBusError, Message, Variant
import jukebox.plugs as plugin

allow_direct_imports = plugin.ALLOW_DIRECT_IMPORTS
plugin.ALLOW_DIRECT_IMPORTS = True
sys.modules['jukebox.publishing'] = MagicMock()
from components.media import MediaRouter, MediaSource  # noqa: E402
from components.media.bluetooth import (  # noqa: E402
    BluezMediaSource, BluezMediaMonitor, BluezDBusTransport, BluezPlayerTransport,
    MEDIA_PLAYER_INTERFACE, OBJECT_MANAGER_INTERFACE,
)
plugin.ALLOW_DIRECT_IMPORTS = allow_direct_imports


class LocalSource(MediaSource):
    def __init__(self):
        super().__init__('mpd', 'Local', ('stop', 'toggle'))
        self.commands = []

    def invoke(self, command):
        self.commands.append(command)

    def get_status(self):
        return {'state': 'stopped'}


def objects(state='playing'):
    return {'/player': {MEDIA_PLAYER_INTERFACE: {
        'Status': Variant('s', state), 'Position': Variant('u', 3250),
        'Track': Variant('a{sv}', {'Title': Variant('s', 'Song'),
                                 'Artist': Variant('as', ['Artist']), 'Duration': Variant('u', 250000)}),
    }}}


@pytest.fixture
def setup():
    router, mpd = MediaRouter(), LocalSource()
    transport = MagicMock()
    source = BluezMediaSource(transport, status_callback=router.publish_status)
    source.set_activity_callback(lambda active: (
        router.claim_source('bluetooth') if active else router.release_source('bluetooth')))
    router.register_source(mpd)
    router.register_source(source)
    return router, mpd, source, BluezMediaMonitor(source), transport


@pytest.mark.parametrize('entry', ['snapshot', 'added', 'changed'])
def test_native_variants_claim_and_decode_nested_metadata(setup, entry):
    router, mpd, source, monitor, _ = setup
    interfaces = objects()['/player']
    if entry == 'snapshot':
        monitor.process_managed_objects(objects())
    elif entry == 'added':
        monitor.interfaces_added('/player', interfaces)
    else:
        monitor.properties_changed('/player', MEDIA_PLAYER_INTERFACE, interfaces[MEDIA_PLAYER_INTERFACE])
    assert router.get_active_source() == 'bluetooth'
    assert mpd.commands == ['stop']
    assert source.get_status() == {'state': 'playing', 'has_media': True, 'title': 'Song',
                                   'artist': 'Artist', 'position': 3.25, 'duration': 250.0}


def test_pause_resume_remains_on_phone_and_idle_pause_does_not_claim(setup):
    router, mpd, source, monitor, transport = setup
    monitor.process_managed_objects(objects('paused'))
    assert router.get_active_source() == 'mpd'
    assert not source.capabilities
    monitor.process_managed_objects(objects())
    router.toggle()
    monitor.process_managed_objects(objects('paused'))
    router.toggle()
    assert router.get_active_source() == 'bluetooth'
    assert transport.call.call_args_list == [(('/player', 'Pause'),), (('/player', 'Play'),)]
    assert mpd.commands == ['stop']
    monitor.process_managed_objects({})
    assert router.get_active_source() == 'mpd'
    assert source.get_status()['state'] == 'unavailable'


@pytest.mark.parametrize('state', ['playing', 'paused'])
def test_stop_releases_and_late_snapshots_do_not_reclaim(setup, state):
    router, _, source, monitor, transport = setup
    monitor.process_managed_objects(objects())
    monitor.process_managed_objects(objects(state))
    router.claim_source('mpd')
    transport.call.assert_called_once_with('/player', 'Stop')
    monitor.process_managed_objects(objects(state))
    assert router.get_active_source() == 'mpd'
    assert not source.capabilities
    monitor.process_managed_objects(objects('paused'))
    monitor.process_managed_objects(objects())
    assert router.get_active_source() == 'bluetooth'


def test_full_snapshot_removes_old_properties_and_invalidated_status_releases(setup):
    router, _, source, monitor, _ = setup
    monitor.process_managed_objects(objects())
    monitor.process_managed_objects({'/player': {MEDIA_PLAYER_INTERFACE: {'Status': Variant('s', 'paused')}}})
    assert source.get_status() == {'state': 'paused', 'has_media': True}
    monitor.properties_changed('/player', MEDIA_PLAYER_INTERFACE, {}, ['Status'])
    assert router.get_active_source() == 'mpd'


def test_daemon_replacement_discards_paused_session_and_unsupported_cache(setup):
    router, _, source, monitor, _ = setup
    monitor.transport = MagicMock()
    monitor._apply_snapshot(':1.1', objects())
    monitor.transport.call.side_effect = DBusError('org.bluez.Error.NotSupported', 'Next')
    router.next()
    assert 'next' not in source.capabilities
    monitor._apply_snapshot(':1.2', objects('paused'))
    assert router.get_active_source() == 'mpd'
    monitor._apply_snapshot(':1.2', objects())
    assert 'next' in source.capabilities
    monitor.transport.call.side_effect = None
    router.next()
    monitor.transport.call.assert_called_with(':1.2', '/player', 'Next')


def test_late_error_from_previous_daemon_does_not_disable_new_daemon_controls(setup):
    _, _, source, monitor, old_transport = setup
    monitor.process_managed_objects(objects())

    def restart_then_reject(*args):
        source.disconnected()
        source.set_transport(MagicMock())
        monitor.process_managed_objects(objects())
        raise DBusError('org.bluez.Error.NotSupported', 'old daemon rejected Next')

    old_transport.call.side_effect = restart_then_reject
    source.invoke('next')
    assert 'next' in source.capabilities


def test_monitor_retries_boot_and_bus_failures_and_cleans_up(setup):
    router, _, source, _, _ = setup
    transport = MagicMock()
    transport.snapshot = AsyncMock(side_effect=[
        OSError('bus not ready'), (':1.1', objects()), OSError('bus lost'),
        (':1.2', objects('paused')), (':1.2', objects()),
    ])
    transport.close = AsyncMock()
    monitor = BluezMediaMonitor(source, transport)
    states, waits = [], []

    def wait(delay):
        states.append(router.get_active_source())
        waits.append(delay)
        if len(states) == 5:
            monitor.stop()

    monitor._stopped.wait = wait
    monitor._run()
    assert states == ['mpd', 'bluetooth', 'mpd', 'mpd', 'bluetooth']
    assert waits == [2, 0.5, 2, 0.5, 0.5]
    assert router.get_active_source() == 'mpd'
    assert source.get_status()['state'] == 'unavailable'
    assert monitor._loop.is_closed()
    monitor.stop()  # also safe after the loop has closed
    assert transport.close.await_count == 3


def test_shutdown_during_snapshot_cancels_it_and_does_not_apply_late_state(setup):
    router, _, source, _, _ = setup
    started, cancelled = threading.Event(), threading.Event()

    async def snapshot():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    transport = MagicMock(snapshot=snapshot, close=AsyncMock())
    monitor = BluezMediaMonitor(source, transport)
    monitor.SNAPSHOT_TIMEOUT = 0.05
    monitor.start()
    try:
        assert started.wait(1)
        thread = monitor.stop()
        thread.join(1)
        assert not thread.is_alive()
        assert cancelled.is_set()
        assert router.get_active_source() == 'mpd'
    finally:
        monitor.stop().join(1)


def fake_bus():
    return MagicMock(wait_for_disconnect=AsyncMock())


def test_native_snapshot_is_pinned_to_unique_owner():
    transport, bus = BluezDBusTransport(), fake_bus()
    transport._connect = AsyncMock(return_value=bus)
    transport._call = AsyncMock(side_effect=[[':1.7'], [objects()]])
    owner, snapshot = asyncio.run(transport.snapshot())
    assert owner == ':1.7'
    assert snapshot['/player'][MEDIA_PLAYER_INTERFACE]['Status'].value == 'playing'
    transport._call.assert_awaited_with(bus, ':1.7', '/', OBJECT_MANAGER_INTERFACE, 'GetManagedObjects')
    asyncio.run(transport.close())
    bus.disconnect.assert_called_once()


def test_native_command_errors_keep_bluez_error_type():
    request = Message(destination='org.bluez', path='/player', member='Next', serial=1)
    bus = fake_bus()
    bus.call = AsyncMock(return_value=Message.new_error(request, 'org.bluez.Error.NotSupported', 'unsupported'))
    with pytest.raises(DBusError) as error:
        asyncio.run(BluezDBusTransport._call(bus, ':1.1', '/player', MEDIA_PLAYER_INTERFACE, 'Next'))
    assert error.value.type == 'org.bluez.Error.NotSupported'


def test_command_timeout_cancels_without_retry_and_disconnects():
    transport, bus = BluezDBusTransport(), fake_bus()
    transport.COMMAND_TIMEOUT = 0.01
    transport._connect = AsyncMock(return_value=bus)
    cancelled = []

    async def hang(*args):
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    transport._call = AsyncMock(side_effect=hang)
    with pytest.raises(asyncio.TimeoutError):
        transport.call(':1.3', '/player', 'Next')
    assert cancelled == [True]
    transport._call.assert_awaited_once_with(bus, ':1.3', '/player', MEDIA_PLAYER_INTERFACE, 'Next')
    bus.disconnect.assert_called_once()


def test_command_completes_while_monitor_callback_waits_for_router_lock(setup, caplog):
    router, _, source, monitor, _ = setup
    transport, bus = BluezDBusTransport(), fake_bus()
    transport.COMMAND_TIMEOUT = 0.5
    transport._connect = AsyncMock(return_value=bus)
    command_started, callback_started, command_completed = (threading.Event() for _ in range(3))

    def activity(active):
        if active:
            router.claim_source('bluetooth')
        else:
            callback_started.set()
            router.release_source('bluetooth')

    async def command(*args):
        command_started.set()
        while not callback_started.is_set():
            await asyncio.sleep(0.001)
        command_completed.set()

    source.set_activity_callback(activity)
    source.set_transport(BluezPlayerTransport(transport, ':1.1'))
    transport._call = AsyncMock(side_effect=command)
    monitor.process_managed_objects(objects())

    def update():
        if command_started.wait(1):
            monitor.process_managed_objects(objects('stopped'))

    thread = threading.Thread(target=update, daemon=True)
    thread.start()
    try:
        router.toggle()
        thread.join(1)
        assert command_completed.is_set()
        assert not thread.is_alive()
        assert 'failed' not in caplog.text
        assert router.get_active_source() == 'mpd'
    finally:
        callback_started.set()
        thread.join(1)
