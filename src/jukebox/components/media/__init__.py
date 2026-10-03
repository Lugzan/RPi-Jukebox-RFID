# RPi-Jukebox-RFID Version 3
# Copyright (c) See file LICENSE in project root folder
"""Active media-source routing.

This package provides the stable transport endpoint ``media.ctrl``. Inputs
should target this endpoint instead of a concrete player implementation. Media
sources register an adapter and claim the endpoint when they begin a playback
session. The source that claims control stops the formerly active source before
becoming the target for subsequent transport commands.
"""
import logging
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Optional

import jukebox.plugs as plugin
import jukebox.publishing as publishing
import jukebox.cfghandler


logger = logging.getLogger('jb.media')


class MediaSource(ABC):
    """Interface implemented by a controllable media source."""

    def __init__(self, source_id: str, name: str, capabilities: Iterable[str]):
        self.source_id = source_id
        self.name = name
        self.capabilities = frozenset(capabilities)

    def supports(self, command: str) -> bool:
        """Return whether ``command`` is supported by this source."""
        return command in self.capabilities

    @abstractmethod
    def invoke(self, command: str, *args, **kwargs) -> Any:
        """Execute a transport command supported by this source."""

    @abstractmethod
    def get_status(self) -> Dict[str, Any]:
        """Return source status using the normalized media status field names."""


class MpdMediaSource(MediaSource):
    """Adapter for the existing ``player.ctrl`` MPD implementation."""

    _COMMANDS = {
        'play': 'play',
        'pause': 'pause',
        'toggle': 'toggle',
        'next': 'next',
        'previous': 'prev',
        'stop': 'stop',
    }

    def __init__(self, player):
        super().__init__('mpd', 'Local MPD', self._COMMANDS.keys())
        # Resolve once during plugin initialization. Taking the plugin registry
        # lock inside the router lock deadlocks with RPC calls in the other order.
        self._player = player

    def invoke(self, command: str, *args, **kwargs) -> Any:
        method = self._COMMANDS[command]
        return getattr(self._player, method)(*args, **kwargs)

    def get_status(self) -> Dict[str, Any]:
        status = self._player.playerstatus()
        if not isinstance(status, dict):
            return {'state': 'unavailable'}
        return {
            'state': status.get('state', 'stopped'),
            'title': status.get('title'),
            'artist': status.get('artist'),
            'album': status.get('album'),
            'position': status.get('elapsed'),
            'duration': status.get('duration'),
            'has_media': status.get('songid') is not None,
        }


class MediaRouter:
    """Select and dispatch commands to the currently active media source."""

    _STATE_MAP = {
        'play': 'playing',
        'playing': 'playing',
        'pause': 'paused',
        'paused': 'paused',
        'stop': 'stopped',
        'stopped': 'stopped',
    }

    def __init__(self):
        self._sources: Dict[str, MediaSource] = {}
        self._active_source_id: Optional[str] = None
        self._lock = threading.RLock()
        self._command_sequence = 0

    def register_source(self, source: MediaSource, *, make_active: bool = False) -> None:
        """Register a media source adapter.

        Source identifiers are stable API values, such as ``mpd`` or
        ``airplay``. A source may be registered before it has an active playback
        session. The first registered source becomes active by default.
        """
        if not isinstance(source, MediaSource):
            raise TypeError('Media source must implement MediaSource')
        if not source.source_id:
            raise ValueError('Media source id must not be empty')

        with self._lock:
            if source.source_id in self._sources:
                raise KeyError(f"Media source '{source.source_id}' is already registered")
            self._sources[source.source_id] = source
            if self._active_source_id is None or make_active:
                self._active_source_id = source.source_id
            logger.info("Registered media source '%s': capabilities=%s active=%s",
                        source.source_id, sorted(source.capabilities), self._active_source_id)
        self.publish_status()

    def unregister_source(self, source_id: str) -> None:
        """Remove a source and fall back to MPD when it was active."""
        with self._lock:
            if source_id not in self._sources:
                raise KeyError(f"Unknown media source '{source_id}'")
            del self._sources[source_id]
            if self._active_source_id == source_id:
                self._active_source_id = 'mpd' if 'mpd' in self._sources else None
            logger.info("Unregistered media source '%s': active=%s", source_id, self._active_source_id)
        self.publish_status()

    def claim_source(self, source_id: str) -> None:
        """Make a new playback source active, stopping the previously active one."""
        logger.debug("Media claim requested: source=%s", source_id)
        with self._lock:
            source = self._get_source_locked(source_id)
            previous = self._get_active_source_locked()
            if previous is source:
                logger.debug("Media claim unchanged: source=%s", source_id)
                return
            if previous is not None and previous.supports('stop'):
                logger.info("Media source '%s' claims control; stopping '%s'", source_id, previous.source_id)
                try:
                    started = time.monotonic()
                    result = previous.invoke('stop')
                    logger.debug("Handoff stop returned: source=%s result=%r elapsed_ms=%.1f",
                                 previous.source_id, result, (time.monotonic() - started) * 1000)
                    if result is False:
                        logger.warning("Handoff continuing despite rejected stop: previous=%s requested=%s",
                                       previous.source_id, source_id)
                except Exception:
                    logger.exception("Could not stop former source '%s' during claim by '%s'",
                                     previous.source_id, source_id)
            elif previous is not None:
                logger.warning("Handoff without stop capability: previous=%s requested=%s",
                               previous.source_id, source_id)
            self._active_source_id = source_id
        logger.info("Media source '%s' is now active", source_id)
        self.publish_status()

    def release_source(self, source_id: str) -> None:
        """Release an active source and fall back to MPD without resuming it."""
        with self._lock:
            if self._active_source_id != source_id:
                logger.debug("Ignoring media release: source=%s active=%s", source_id, self._active_source_id)
                return
            self._active_source_id = 'mpd' if source_id != 'mpd' and 'mpd' in self._sources else None
            logger.info("Media source released: source=%s fallback=%s", source_id, self._active_source_id)
        self.publish_status()

    @contextmanager
    def local_playback(self, *, claim=True):
        """Serialize local playback and handoff before acquiring MPD's lock.

        Composite card/replay actions can defer claiming until they actually
        invoke playback, while retaining this lock across their nested calls.
        """
        with self._lock:
            if claim:
                self.claim_source('mpd')
            yield

    def _get_source_locked(self, source_id: str) -> MediaSource:
        try:
            return self._sources[source_id]
        except KeyError:
            raise KeyError(f"Unknown media source '{source_id}'")

    def _get_active_source_locked(self) -> Optional[MediaSource]:
        if self._active_source_id is None:
            return None
        return self._sources.get(self._active_source_id)

    def _dispatch(self, command: str, *args, **kwargs) -> Any:
        started = time.monotonic()
        logger.debug("Media command requested: command=%s", command)
        with self._lock:
            source = self._get_active_source_locked()
            if source is None:
                logger.warning("Ignoring '%s': no active media source", command)
                return None
            if not source.supports(command):
                logger.warning("Ignoring '%s': active source '%s' does not support it", command, source.source_id)
                return None
            self._command_sequence += 1
            command_id = self._command_sequence
            logger.debug("Media command #%d dispatch: source=%s command=%s capabilities=%s",
                         command_id, source.source_id, command, sorted(source.capabilities))
            try:
                result = source.invoke(command, *args, **kwargs)
            except Exception:
                logger.exception("Media command #%d failed: source=%s command=%s elapsed_ms=%.1f",
                                 command_id, source.source_id, command, (time.monotonic() - started) * 1000)
                raise
            logger.debug("Media command #%d returned: source=%s command=%s result=%r elapsed_ms=%.1f",
                         command_id, source.source_id, command, result, (time.monotonic() - started) * 1000)
        self.publish_status()
        return result

    def _normalized_status(self) -> Dict[str, Any]:
        with self._lock:
            source = self._get_active_source_locked()
            if source is None:
                return {
                    'active_source': None,
                    'active_source_name': None,
                    'state': 'unavailable',
                    'capabilities': [],
                    'has_media': False,
                }
            try:
                status = source.get_status()
            except Exception:
                logger.exception("Could not obtain status from '%s'", source.source_id)
                status = {'state': 'unavailable'}

            normalized = {
                'active_source': source.source_id,
                'active_source_name': source.name,
                'state': self._STATE_MAP.get(status.get('state'), status.get('state', 'unknown')),
                'capabilities': sorted(source.capabilities),
                'has_media': bool(status.get('has_media', False)),
            }
            for field in ('title', 'artist', 'album', 'position', 'duration'):
                if field in status:
                    normalized[field] = status[field]
            return normalized

    def publish_status(self) -> Dict[str, Any]:
        """Publish the normalized state of the currently active source."""
        status = self._normalized_status()
        publishing.get_publisher().send('media.status', status)
        return status

    @plugin.tag
    def play(self):
        return self._dispatch('play')

    @plugin.tag
    def pause(self):
        return self._dispatch('pause')

    @plugin.tag
    def toggle(self):
        return self._dispatch('toggle')

    @plugin.tag
    def next(self):
        return self._dispatch('next')

    @plugin.tag
    def prev(self):
        return self._dispatch('previous')

    @plugin.tag
    def stop(self):
        return self._dispatch('stop')

    @plugin.tag
    def get_active_source(self) -> Optional[str]:
        with self._lock:
            return self._active_source_id

    @plugin.tag
    def get_sources(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                source_id: {
                    'name': source.name,
                    'capabilities': sorted(source.capabilities),
                    'active': source_id == self._active_source_id,
                }
                for source_id, source in self._sources.items()
            }

    @plugin.tag
    def get_status(self) -> Dict[str, Any]:
        return self._normalized_status()

    @plugin.tag
    def claim(self, source_id: str) -> None:
        self.claim_source(source_id)

    @plugin.tag
    def release(self, source_id: str) -> None:
        self.release_source(source_id)


media_ctrl: Optional[MediaRouter] = None
bluetooth_source: Optional[MediaSource] = None
bluetooth_monitor = None
airplay_monitor = None
cfg = jukebox.cfghandler.get_handler('jukebox')


def register_source(source: MediaSource, *, make_active: bool = False) -> None:
    """Register a source adapter with the initialized global media router."""
    if media_ctrl is None:
        raise RuntimeError('Media router is not initialized')
    media_ctrl.register_source(source, make_active=make_active)


def unregister_source(source_id: str) -> None:
    """Remove a source adapter from the initialized global media router."""
    if media_ctrl is None:
        raise RuntimeError('Media router is not initialized')
    media_ctrl.unregister_source(source_id)


def claim_source(source_id: str) -> None:
    """Claim routing for a source that has started a playback session."""
    if media_ctrl is None:
        raise RuntimeError('Media router is not initialized')
    media_ctrl.claim_source(source_id)


def release_source(source_id: str) -> None:
    """Release routing for a source whose playback session has ended."""
    if media_ctrl is None:
        raise RuntimeError('Media router is not initialized')
    media_ctrl.release_source(source_id)


@plugin.initialize
def initialize():
    global media_ctrl, bluetooth_source, bluetooth_monitor, airplay_monitor
    media_ctrl = MediaRouter()
    player = plugin.get('player', 'ctrl')
    media_ctrl.register_source(MpdMediaSource(player))
    if hasattr(player, 'set_media_router'):
        player.set_media_router(media_ctrl)
    logger.info("Media router configuration: bluetooth_enabled=%s airplay_enabled=%s",
                cfg.setndefault('bluetooth_media', 'enable', value=True),
                cfg.setndefault('airplay_media', 'enable', value=False))
    if cfg.setndefault('bluetooth_media', 'enable', value=True):
        # Delayed to avoid making the base router depend on optional D-Bus code.
        from .bluetooth import BluezMediaMonitor, BluezMediaSource
        bluetooth_source = BluezMediaSource(status_callback=media_ctrl.publish_status)
        bluetooth_source.set_activity_callback(
            lambda active: media_ctrl.claim_source('bluetooth') if active else media_ctrl.release_source('bluetooth'))
        media_ctrl.register_source(bluetooth_source)
        bluetooth_monitor = BluezMediaMonitor(bluetooth_source)
        bluetooth_monitor.start()
    if cfg.setndefault('airplay_media', 'enable', value=False):
        from .airplay import AirPlayMediaMonitor, AirPlayMediaSource
        airplay_source = AirPlayMediaSource(activity_callback=(
            lambda active: media_ctrl.claim_source('airplay') if active else media_ctrl.release_source('airplay')),
            status_callback=media_ctrl.publish_status)
        media_ctrl.register_source(airplay_source)
        airplay_monitor = AirPlayMediaMonitor(airplay_source)
        airplay_monitor.start()
    plugin.register(media_ctrl, name='ctrl')


@plugin.atexit
def atexit(**ignored_kwargs):
    """Stop the optional BlueZ watcher without affecting legacy HID controls."""
    if bluetooth_monitor is not None:
        return bluetooth_monitor.stop()


@plugin.atexit
def stop_airplay(**ignored_kwargs):
    """Stop the optional receiver monitor independently of Bluetooth."""
    if airplay_monitor is not None:
        return airplay_monitor.stop()
