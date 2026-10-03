"""BlueZ AVRCP adapter for :mod:`components.media`.

BlueZ exposes phone-provided transport controls as ``org.bluez.MediaPlayer1``
objects. A connected headset alone does not create a claim: only observed
playback claims the router, and an established session retains paused controls.
"""
import asyncio
import logging
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable, Dict, Iterable, Optional

from . import MediaSource


logger = logging.getLogger('jb.media.bluetooth')

BLUEZ_SERVICE = 'org.bluez'
MEDIA_PLAYER_INTERFACE = 'org.bluez.MediaPlayer1'
OBJECT_MANAGER_INTERFACE = 'org.freedesktop.DBus.ObjectManager'
PROPERTIES_INTERFACE = 'org.freedesktop.DBus.Properties'


def unpack_properties(value):
    """Decode the nested a{sv} values returned by every BlueZ property API."""
    from dbus_next import Variant
    if isinstance(value, Variant):
        return unpack_properties(value.value)
    if isinstance(value, Mapping):
        return {key: unpack_properties(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [unpack_properties(item) for item in value]
    return value


class BluezMediaSource(MediaSource):
    """Expose the current BlueZ ``MediaPlayer1`` object as a media source.

    BlueZ does not advertise an AVRCP operation bitmask.  The adapter starts
    with the standard operation set and removes an operation only after BlueZ
    explicitly rejects it as unsupported for the current player.
    """

    _COMMANDS = {
        'play': 'Play',
        'pause': 'Pause',
        'next': 'Next',
        'previous': 'Previous',
        'stop': 'Stop',
    }
    _ALL_CAPABILITIES = frozenset(tuple(_COMMANDS) + ('toggle',))

    def __init__(self, transport=None, status_callback=None):
        super().__init__('bluetooth', 'Bluetooth', ())
        self._transport = transport
        self._players: Dict[str, Dict[str, Any]] = {}
        self._unsupported: Dict[str, set] = {}
        self._active_player_path: Optional[str] = None
        self._activity_callback: Optional[Callable[[bool], None]] = None
        self._status_callback = status_callback
        self._stopped_players = set()
        self._lock = threading.RLock()
        self._refresh_capabilities_locked()

    def set_transport(self, transport) -> None:
        """Set the D-Bus transport after its event loop has connected."""
        with self._lock:
            self._transport = transport
        logger.info('BlueZ transport ready=%s', transport is not None)

    def set_activity_callback(self, callback: Callable[[bool], None]) -> None:
        """Call ``callback`` when a playing/retained-paused session starts or ends."""
        with self._lock:
            self._activity_callback = callback

    def update_player(self, path: str, properties: Mapping[str, Any], *, replace=False) -> None:
        """Merge a BlueZ ``MediaPlayer1`` property snapshot for ``path``."""
        callback, active = None, False
        with self._lock:
            was_active = self._is_active_locked()
            previous = self._players.get(path, {})
            old_status = previous.get('Status')
            current = dict(properties) if replace else {**previous, **properties}
            self._players[path] = current
            if not self._status_is_playing(current):
                self._stopped_players.discard(path)
            if self._status_is_playing(current) and path not in self._stopped_players:
                self._active_player_path = path
            elif self._active_player_path == path and current.get('Status') != 'paused':
                self._active_player_path = self._find_playing_player_locked()
            self._refresh_capabilities_locked()
            active = self._is_active_locked()
            if old_status != current.get('Status'):
                logger.info("BlueZ player state: path=%s status=%r previous=%r active=%s selected=%s capabilities=%s",
                            path, current.get('Status'), old_status, active,
                            self._active_player_path, sorted(self.capabilities))
            if active != was_active:
                callback = self._activity_callback
        if callback is not None:
            callback(active)
        if current != previous and self._status_callback is not None:
            self._status_callback()

    def remove_player(self, path: str) -> None:
        """Forget a removed BlueZ player and release activity if necessary."""
        callback, active = None, False
        with self._lock:
            was_active = self._is_active_locked()
            self._players.pop(path, None)
            self._unsupported.pop(path, None)
            self._stopped_players.discard(path)
            if self._active_player_path == path:
                self._active_player_path = self._find_playing_player_locked()
            self._refresh_capabilities_locked()
            active = self._is_active_locked()
            logger.info('BlueZ player removed: path=%s active=%s selected=%s remaining=%d',
                        path, active, self._active_player_path, len(self._players))
            if active != was_active:
                callback = self._activity_callback
        if callback is not None:
            callback(active)
        if self._status_callback is not None:
            self._status_callback()

    def disconnected(self):
        """Forget a lost daemon/bus atomically, including its paused session."""
        with self._lock:
            was_active = self._is_active_locked()
            self._players.clear()
            self._unsupported.clear()
            self._stopped_players.clear()
            self._active_player_path = None
            self._transport = None
            self._refresh_capabilities_locked()
        if was_active and self._activity_callback is not None:
            self._activity_callback(False)
        if self._status_callback is not None:
            self._status_callback()

    def invoke(self, command: str, *args, **kwargs) -> Any:
        """Invoke the matching AVRCP method, suppressing player-side errors."""
        del args, kwargs
        with self._lock:
            path = self._active_player_path
            if path is None or self._transport is None or not self.supports(command):
                logger.warning('BlueZ command unavailable: command=%s player=%s transport_ready=%s capabilities=%s',
                               command, path, self._transport is not None, sorted(self.capabilities))
                return None
            method = self._method_for_command_locked(command, path)
            transport = self._transport
        if method is None:
            return None
        try:
            result = transport.call(path, method)
        except Exception as error:
            if self._is_unsupported_error(error):
                with self._lock:
                    if transport is self._transport:
                        self._unsupported.setdefault(path, set()).update(
                            self._commands_for_unsupported_method(command, method))
                        self._refresh_capabilities_locked()
                logger.info("BlueZ player '%s' does not support %s", path, command)
            else:
                logger.warning("BlueZ %s failed for '%s': %s: %s",
                               method, path, error.__class__.__name__, error, exc_info=True)
            return None
        if command == 'stop':
            # Ignore late playing snapshots until an inactive edge is seen.
            # Otherwise a successful handoff could immediately reclaim routing.
            with self._lock:
                same_session = transport is self._transport and path == self._active_player_path
                if same_session:
                    self._stopped_players.add(path)
                    self._active_player_path = None
                    self._refresh_capabilities_locked()
            if same_session and self._activity_callback is not None:
                self._activity_callback(False)
        return result

    def get_status(self) -> Dict[str, Any]:
        """Return BlueZ metadata in the router's normalized status shape."""
        with self._lock:
            path = self._active_player_path or self._find_any_player_locked()
            if path is None:
                return {'state': 'unavailable', 'has_media': False}
            properties = dict(self._players[path])

        track = properties.get('Track')
        if not isinstance(track, Mapping):
            track = {}
        bluez_status = str(properties.get('Status', 'stopped')).lower()
        status = {
            'playing': 'playing',
            'forward-seek': 'playing',
            'reverse-seek': 'playing',
            'paused': 'paused',
            'stopped': 'stopped',
        }.get(bluez_status, 'unavailable')
        result = {
            'state': status,
            'has_media': bool(track or status in ('playing', 'paused')),
        }
        metadata = {
            'title': track.get('Title'),
            'artist': self._first_value(track.get('Artist')),
            'album': track.get('Album'),
        }
        for key, value in metadata.items():
            if value is not None:
                result[key] = str(value)
        position = self._milliseconds_to_seconds(properties.get('Position'))
        duration = self._milliseconds_to_seconds(track.get('Duration'))
        if position is not None:
            result['position'] = position
        if duration is not None:
            result['duration'] = duration
        return result

    def _method_for_command_locked(self, command: str, path: str) -> Optional[str]:
        if command == 'toggle':
            status = self._players.get(path, {}).get('Status', '')
            return 'Pause' if self._status_is_playing({'Status': status}) else 'Play'
        return self._COMMANDS.get(command)

    @staticmethod
    def _commands_for_unsupported_method(command: str, method: str):
        if command != 'toggle':
            return (command,)
        return ('toggle', method.lower())

    def _is_active_locked(self) -> bool:
        return self._active_player_path is not None

    def _find_playing_player_locked(self) -> Optional[str]:
        if (self._active_player_path is not None
                and self._status_is_playing(self._players.get(self._active_player_path, {}))):
            return self._active_player_path
        for path, properties in reversed(tuple(self._players.items())):
            if self._status_is_playing(properties) and path not in self._stopped_players:
                return path
        return None

    def _find_any_player_locked(self) -> Optional[str]:
        if self._active_player_path in self._players:
            return self._active_player_path
        return next(reversed(self._players), None)

    @staticmethod
    def _status_is_playing(properties: Mapping[str, Any]) -> bool:
        return str(properties.get('Status', '')).lower() in ('playing', 'forward-seek', 'reverse-seek')

    def _refresh_capabilities_locked(self) -> None:
        path = self._active_player_path
        if path is None:
            self.capabilities = frozenset()
            return
        unsupported = self._unsupported.get(path, set())
        self.capabilities = self._ALL_CAPABILITIES - unsupported

    @staticmethod
    def _is_unsupported_error(error: Exception) -> bool:
        name = getattr(error, 'type', None) or getattr(error, 'name', None) or ''
        return 'notsupported' in str(name).replace('_', '').lower()

    @staticmethod
    def _first_value(value):
        if isinstance(value, (list, tuple)):
            return value[0] if value else None
        return value

    @staticmethod
    def _milliseconds_to_seconds(value):
        try:
            return float(value) / 1000
        except (TypeError, ValueError):
            return None


class BluezDBusTransport:
    """Complete snapshots on one connection; commands on independent connections.

    The monitor applies snapshots outside its asyncio loop. Router callbacks may
    block that thread, but cannot block command replies on another connection.
    Commands pin the unique owner which supplied their source's state.
    """

    COMMAND_TIMEOUT = 3

    def __init__(self):
        self.bus = None

    async def _connect(self):
        from dbus_next import BusType
        from dbus_next.aio import MessageBus
        bus = MessageBus(bus_type=BusType.SYSTEM)
        try:
            return await bus.connect()
        except BaseException:
            bus.disconnect()
            raise

    @staticmethod
    async def _call(bus, owner, path, interface, member, signature='', body=None):
        from dbus_next import Message, MessageType, DBusError
        reply = await bus.call(Message(destination=owner, path=path, interface=interface,
                                       member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise DBusError(reply.error_name, str(reply.body))
        return reply.body

    async def snapshot(self):
        if self.bus is None:
            self.bus = await self._connect()
        owner = (await self._call(self.bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                                  'org.freedesktop.DBus', 'GetNameOwner', 's', [BLUEZ_SERVICE]))[0]
        objects = (await self._call(self.bus, owner, '/', OBJECT_MANAGER_INTERFACE, 'GetManagedObjects'))[0]
        return owner, objects

    @staticmethod
    async def _disconnect(bus):
        bus.disconnect()
        try:
            await asyncio.wait_for(bus.wait_for_disconnect(), timeout=1)
        except Exception:
            logger.debug('BlueZ connection cleanup after disconnect', exc_info=True)

    async def close(self):
        bus, self.bus = self.bus, None
        if bus is not None:
            await self._disconnect(bus)

    def call(self, owner, path, method):
        started = time.monotonic()
        logger.debug('BlueZ command: owner=%s path=%s method=%s timeout_s=%s',
                     owner, path, method, self.COMMAND_TIMEOUT)
        try:
            return asyncio.run(asyncio.wait_for(self._command(owner, path, method), timeout=self.COMMAND_TIMEOUT))
        finally:
            logger.debug('BlueZ command ended: owner=%s path=%s method=%s elapsed_ms=%.1f',
                         owner, path, method, (time.monotonic() - started) * 1000)

    async def _command(self, owner, path, method):
        bus = await self._connect()
        try:
            await self._call(bus, owner, path, MEDIA_PLAYER_INTERFACE, method)
        finally:
            # wait_for cancels the outstanding call on timeout; never retry a
            # transport action because BlueZ may already have executed it.
            await self._disconnect(bus)


class BluezPlayerTransport:
    """Bind commands to the daemon instance that supplied this session."""

    def __init__(self, transport, owner):
        self.transport = transport
        self.owner = owner

    def call(self, path, method):
        return self.transport.call(self.owner, path, method)


class BluezMediaMonitor:
    """Poll full BlueZ state every 0.5s and retry daemon/bus loss after 2s.

    A complete snapshot removes vanished players and invalidated properties.
    A replacement unique owner starts with a fresh source, so paused sessions
    and unsupported-command caches cannot survive a daemon restart.
    """

    SNAPSHOT_TIMEOUT = 3

    def __init__(self, source: BluezMediaSource, transport=None):
        self.source = source
        self.transport = transport or BluezDBusTransport()
        self._thread: Optional[threading.Thread] = None
        self._loop = None
        self._stopped = threading.Event()
        self._owner = None
        self._known_players = set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name='BluezMediaMonitor')
        logger.info('Starting BlueZ media monitor: service=%s poll_s=0.5 retry_s=2 timeout_s=3', BLUEZ_SERVICE)
        self._thread.start()

    def stop(self):
        """Wake the retry/poll wait; an in-flight snapshot has a bounded deadline."""
        self._stopped.set()
        logger.info('Stopping BlueZ media monitor')
        return self._thread

    def process_managed_objects(self, objects: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> None:
        """Replace cached state from GetManagedObjects, including removals."""
        players = {str(path): interfaces[MEDIA_PLAYER_INTERFACE] for path, interfaces in objects.items()
                   if MEDIA_PLAYER_INTERFACE in interfaces}
        for path in self._known_players - players.keys():
            self.source.remove_player(path)
        for path, properties in players.items():
            self.source.update_player(path, unpack_properties(properties), replace=True)
        self._known_players = set(players)

    def interfaces_added(self, path: str, interfaces: Mapping[str, Mapping[str, Any]]) -> None:
        """Decode an optional InterfacesAdded update using the same boundary."""
        properties = interfaces.get(MEDIA_PLAYER_INTERFACE)
        if properties is not None:
            self._known_players.add(str(path))
            self.source.update_player(str(path), unpack_properties(properties))

    def interfaces_removed(self, path: str, interfaces: Iterable[str]) -> None:
        if MEDIA_PLAYER_INTERFACE in interfaces:
            self._known_players.discard(str(path))
            self.source.remove_player(str(path))

    def properties_changed(self, path: str, interface: str, changed: Mapping[str, Any],
                           invalidated: Iterable[str] = ()) -> None:
        """Decode property updates; the next full snapshot refreshes invalidations."""
        if interface == MEDIA_PLAYER_INTERFACE:
            properties = {key: None for key in invalidated}
            properties.update(unpack_properties(changed))
            self.source.update_player(str(path), properties)

    def _reset_source(self):
        self._owner = None
        self._known_players.clear()
        self.source.disconnected()

    def _apply_snapshot(self, owner, objects):
        if owner != self._owner:
            self._reset_source()
            self._owner = owner
            self.source.set_transport(BluezPlayerTransport(self.transport, owner))
            logger.info('BlueZ daemon connected: owner=%s', owner)
        self.process_managed_objects(objects)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop.set_exception_handler(self._log_async_error)
        failed = False
        try:
            while not self._stopped.is_set():
                try:
                    snapshot = self._loop.run_until_complete(
                        asyncio.wait_for(self.transport.snapshot(), timeout=self.SNAPSHOT_TIMEOUT))
                    if not self._stopped.is_set():
                        self._apply_snapshot(*snapshot)
                    if failed:
                        logger.info('BlueZ media monitor recovered')
                    failed = False
                except Exception:
                    self._reset_source()
                    self._loop.run_until_complete(self.transport.close())
                    if not failed:
                        logger.warning('BlueZ unavailable; retrying in 2s', exc_info=True)
                    failed = True
                self._stopped.wait(2 if failed else 0.5)
        finally:
            self._loop.run_until_complete(self.transport.close())
            self._reset_source()
            self._loop.close()
            logger.info('BlueZ media monitor exited')

    @staticmethod
    def _log_async_error(loop, context):
        error = context.get('exception')
        logger.error('BlueZ background operation failed: %s', context.get('message'),
                     exc_info=(type(error), error, error.__traceback__) if error is not None else None)
