"""BlueZ AVRCP adapter for :mod:`components.media`.

BlueZ exposes phone-provided transport controls as ``org.bluez.MediaPlayer1``
objects.  A connected headset alone does not create a claim: only a player
whose ``Status`` is ``playing`` claims the media router.
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

    def __init__(self, transport=None):
        super().__init__('bluetooth', 'Bluetooth', ())
        self._transport = transport
        self._players: Dict[str, Dict[str, Any]] = {}
        self._unsupported: Dict[str, set] = {}
        self._active_player_path: Optional[str] = None
        self._activity_callback: Optional[Callable[[bool], None]] = None
        self._lock = threading.RLock()
        self._refresh_capabilities_locked()

    def set_transport(self, transport) -> None:
        """Set the D-Bus transport after its event loop has connected."""
        with self._lock:
            self._transport = transport
        logger.info('BlueZ transport ready=%s', transport is not None)

    def set_activity_callback(self, callback: Callable[[bool], None]) -> None:
        """Call ``callback`` when the aggregate playback activity changes."""
        with self._lock:
            self._activity_callback = callback

    def update_player(self, path: str, properties: Mapping[str, Any]) -> None:
        """Merge a BlueZ ``MediaPlayer1`` property snapshot for ``path``."""
        callback, active = None, False
        with self._lock:
            was_active = self._is_active_locked()
            current = self._players.setdefault(path, {})
            old_status = current.get('Status')
            current.update(dict(properties))
            if self._status_is_playing(current):
                self._active_player_path = path
            elif self._active_player_path == path:
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

    def remove_player(self, path: str) -> None:
        """Forget a removed BlueZ player and release activity if necessary."""
        callback, active = None, False
        with self._lock:
            was_active = self._is_active_locked()
            self._players.pop(path, None)
            self._unsupported.pop(path, None)
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
        if method is None:
            return None
        try:
            return self._transport.call(path, method)
        except Exception as error:
            if self._is_unsupported_error(error):
                with self._lock:
                    self._unsupported.setdefault(path, set()).update(
                        self._commands_for_unsupported_method(command, method))
                    self._refresh_capabilities_locked()
                logger.info("BlueZ player '%s' does not support %s", path, command)
            else:
                logger.warning("BlueZ %s failed for '%s': %s: %s",
                               method, path, error.__class__.__name__, error, exc_info=True)
            return None

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
            return 'Pause' if str(status).lower() == 'playing' else 'Play'
        return self._COMMANDS.get(command)

    @staticmethod
    def _commands_for_unsupported_method(command: str, method: str):
        if command != 'toggle':
            return (command,)
        return ('toggle', method.lower())

    def _is_active_locked(self) -> bool:
        return self._find_playing_player_locked() is not None

    def _find_playing_player_locked(self) -> Optional[str]:
        if (self._active_player_path is not None
                and self._status_is_playing(self._players.get(self._active_player_path, {}))):
            return self._active_player_path
        for path, properties in reversed(tuple(self._players.items())):
            if self._status_is_playing(properties):
                return path
        return None

    def _find_any_player_locked(self) -> Optional[str]:
        if self._active_player_path in self._players:
            return self._active_player_path
        return next(reversed(self._players), None)

    @staticmethod
    def _status_is_playing(properties: Mapping[str, Any]) -> bool:
        return str(properties.get('Status', '')).lower() == 'playing'

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


class BluezMediaMonitor:
    """Maintain a :class:`BluezMediaSource` from BlueZ D-Bus signals.

    The monitor owns a tiny asyncio loop in a daemon thread.  It is deliberately
    optional: systems without BlueZ or ``dbus-next`` keep the MPD-only router.
    """

    def __init__(self, source: BluezMediaSource):
        self.source = source
        self._thread: Optional[threading.Thread] = None
        self._loop = None
        self._bus = None
        self._stopped = threading.Event()
        self._player_properties = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name='BluezMediaMonitor')
        logger.info('Starting BlueZ media monitor: service=%s', BLUEZ_SERVICE)
        self._thread.start()

    def stop(self):
        """Stop the monitor and return its thread for plugin shutdown."""
        self._stopped.set()
        logger.info('Stopping BlueZ media monitor')
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        return self._thread

    def process_managed_objects(self, objects: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> None:
        """Process ``ObjectManager.GetManagedObjects`` output (also test hook)."""
        logger.debug('BlueZ initial discovery: objects=%d players=%d', len(objects),
                     sum(MEDIA_PLAYER_INTERFACE in interfaces for interfaces in objects.values()))
        for path, interfaces in objects.items():
            properties = interfaces.get(MEDIA_PLAYER_INTERFACE)
            if properties is not None:
                self.source.update_player(str(path), properties)

    def interfaces_added(self, path: str, interfaces: Mapping[str, Mapping[str, Any]]) -> None:
        """Handle BlueZ ``InterfacesAdded`` (also test hook)."""
        properties = interfaces.get(MEDIA_PLAYER_INTERFACE)
        if properties is not None:
            self.source.update_player(str(path), properties)

    def interfaces_removed(self, path: str, interfaces: Iterable[str]) -> None:
        """Handle BlueZ ``InterfacesRemoved`` (also test hook)."""
        if MEDIA_PLAYER_INTERFACE in interfaces:
            self.source.remove_player(str(path))

    def properties_changed(self, path: str, interface: str, changed: Mapping[str, Any],
                           invalidated: Iterable[str] = ()) -> None:
        """Handle a ``PropertiesChanged`` update (also test hook)."""
        if interface == MEDIA_PLAYER_INTERFACE:
            logger.debug('BlueZ properties changed: path=%s fields=%s invalidated=%s',
                         path, sorted(changed), invalidated)
            self.source.update_player(str(path), changed)
            if 'Status' in invalidated:
                # Do not leave a stale playing state in control if BlueZ omits
                # the new value.  The D-Bus callback then refreshes it.
                self.source.update_player(str(path), {'Status': 'stopped'})

    def _run(self) -> None:
        try:
            from dbus_next import BusType
            from dbus_next.aio import MessageBus
        except ImportError:
            logger.info('Bluetooth media support unavailable: install dbus-next on the BlueZ host')
            return

        self._loop = asyncio.new_event_loop()
        self._loop.set_exception_handler(self._log_async_error)
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect(MessageBus, BusType))
            logger.info('BlueZ media monitor connected: watched_players=%d', len(self._player_properties))
            if not self._stopped.is_set():
                self._loop.run_forever()
        except Exception as error:
            logger.warning('Bluetooth media monitor could not connect to BlueZ: %s: %s',
                           error.__class__.__name__, error, exc_info=True)
        finally:
            if self._bus is not None:
                self._bus.disconnect()
            self._loop.close()
            logger.info('BlueZ media monitor exited: shutdown_requested=%s', self._stopped.is_set())

    @staticmethod
    def _log_async_error(loop, context):
        error = context.get('exception')
        logger.error('BlueZ background operation failed: %s', context.get('message'),
                     exc_info=(type(error), error, error.__traceback__) if error is not None else None)

    async def _connect(self, message_bus, bus_type) -> None:
        self._bus = await message_bus(bus_type=bus_type.SYSTEM).connect()
        manager = await self._get_interface('/', OBJECT_MANAGER_INTERFACE)
        managed_objects = await manager.call_get_managed_objects()
        self.process_managed_objects(managed_objects)
        manager.on_interfaces_added(self._on_interfaces_added)
        manager.on_interfaces_removed(self._on_interfaces_removed)
        for path, interfaces in managed_objects.items():
            if MEDIA_PLAYER_INTERFACE in interfaces:
                await self._watch_player(path)
        self.source.set_transport(self)

    async def _get_interface(self, path: str, interface: str):
        introspection = await self._bus.introspect(BLUEZ_SERVICE, path)
        proxy_object = self._bus.get_proxy_object(BLUEZ_SERVICE, path, introspection)
        return proxy_object.get_interface(interface)

    async def _watch_player(self, path: str) -> None:
        if path in self._player_properties:
            return
        properties = await self._get_interface(path, PROPERTIES_INTERFACE)
        properties.on_properties_changed(
            lambda interface, changed, invalidated: self._on_player_properties_changed(
                path, interface, changed, invalidated))
        self._player_properties[path] = properties
        logger.debug('BlueZ property watch installed: path=%s', path)

    def _on_player_properties_changed(self, path, interface, changed, invalidated) -> None:
        self.properties_changed(path, interface, changed, invalidated)
        if interface == MEDIA_PLAYER_INTERFACE and invalidated:
            asyncio.ensure_future(self._refresh_player(path), loop=self._loop)

    async def _refresh_player(self, path: str) -> None:
        properties = self._player_properties.get(path)
        if properties is None:
            return
        try:
            self.source.update_player(path, await properties.call_get_all(MEDIA_PLAYER_INTERFACE))
        except Exception as error:
            logger.debug("Could not refresh BlueZ player '%s': %s", path, error, exc_info=True)

    def _on_interfaces_added(self, path, interfaces) -> None:
        self.interfaces_added(path, interfaces)
        if MEDIA_PLAYER_INTERFACE in interfaces:
            asyncio.ensure_future(self._watch_player(path), loop=self._loop)

    def _on_interfaces_removed(self, path, interfaces) -> None:
        self._player_properties.pop(path, None)
        self.interfaces_removed(path, interfaces)

    def call(self, path: str, method: str):
        """Synchronously invoke a MediaPlayer1 method from the router thread."""
        if self._loop is None or self._loop.is_closed():
            logger.warning('BlueZ D-Bus command skipped: loop unavailable path=%s method=%s', path, method)
            return None
        started = time.monotonic()
        logger.debug('BlueZ D-Bus command: path=%s method=%s timeout_s=5', path, method)
        future = asyncio.run_coroutine_threadsafe(self._call(path, method), self._loop)
        try:
            return future.result(timeout=5)
        finally:
            logger.debug('BlueZ D-Bus wait ended: path=%s method=%s done=%s elapsed_ms=%.1f',
                         path, method, future.done(), (time.monotonic() - started) * 1000)

    async def _call(self, path: str, method: str):
        player = await self._get_interface(path, MEDIA_PLAYER_INTERFACE)
        call_method = getattr(player, 'call_' + method.lower())
        return await call_method()
