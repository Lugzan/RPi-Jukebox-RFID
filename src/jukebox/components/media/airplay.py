"""Optional Shairport Sync native D-Bus adapter (Classic and AirPlay 2).

Classic remote commands are enabled only when the receiver reports DACP
availability. AirPlay 2 retains receiver-local session termination only.
"""
import asyncio
import logging
import threading
import time

from . import MediaSource


logger = logging.getLogger('jb.media.airplay')
SERVICE = 'org.gnome.ShairportSync'
PATH = '/org/gnome/ShairportSync'
PROPERTIES = 'org.freedesktop.DBus.Properties'
REMOTE = SERVICE + '.RemoteControl'
# Same DACP requests used by Shairport Sync 5.5.2 RemoteControl handlers.
REMOTE_COMMANDS = {
    'play': 'play', 'pause': 'pause', 'toggle': 'playpause',
    'next': 'nextitem', 'previous': 'previtem',
}


class AirPlayMediaSource(MediaSource):
    """Claim on audio activity; retain confirmed controllable Classic pauses.

    Active includes Shairport's configured inactivity grace period. Repeated
    active snapshots never reclaim control after another source takes over.
    """

    def __init__(self, transport=None, activity_callback=None, status_callback=None):
        super().__init__('airplay', 'AirPlay', ())
        self._transport = transport
        self._activity_callback = activity_callback
        self._status_callback = status_callback
        self._owner = None
        self._client = ''
        self._active = False
        self._session = False
        self._stop_requested = False
        self._state = 'unavailable'
        self._lock = threading.RLock()
        self._last_observation = None

    def update(self, owner, active, can_stop=True, remote=None):
        """Apply receiver activity and optional Classic remote-control status.

        Only actual activity starts a claim. A known, controllable paused client
        may retain an existing claim after the audio inactivity grace period.
        """
        remote = remote or {}
        client = remote.get('client', '')
        enabled = remote.get('protocol') == 'AirPlay' and remote.get('available') is True
        enabled = enabled and remote.get('can_command') is True
        player_state = remote.get('player_state')
        with self._lock:
            before = (self._state, self._session, self.capabilities)
            previous_owner, was_active, had_session = self._owner, self._active, self._session
            same_client = owner == self._owner and client == self._client
            new_client = bool(self._client and client and self._client != client)
            if owner != previous_owner or new_client or (active and not was_active):
                self._stop_requested = False
            retain_pause = (had_session and same_client and bool(client) and enabled
                            and player_state == 'Paused' and not self._stop_requested)
            self._owner, self._client = owner, client
            self._active = bool(owner and active)
            self._session = bool(owner and (self._active or retain_pause))
            self._state = self._normalized_state(player_state, enabled)
            commands = set()
            if self._session and not self._stop_requested:
                if can_stop:
                    commands.add('stop')
                if enabled:
                    commands.update(REMOTE_COMMANDS)
            self.capabilities = frozenset(commands)
            changed = had_session != self._session or (self._active and previous_owner != owner)
            session = self._session
            status_changed = before != (self._state, self._session, self.capabilities)
            observation = (owner, client, self._active, can_stop, remote.get('protocol'),
                           remote.get('available'), remote.get('can_command'), player_state,
                           self._session, self._stop_requested, self.capabilities)
            if observation != self._last_observation:
                logger.info('AirPlay observation: owner=%s active=%s protocol=%s remote_available=%s '
                            'can_command=%s can_stop=%s client_present=%s client_changed=%s '
                            'player_state=%s state=%s session=%s stop_requested=%s capabilities=%s',
                            owner, self._active, remote.get('protocol'), remote.get('available'),
                            remote.get('can_command'), can_stop, bool(client), not same_client,
                            player_state, self._state, self._session, self._stop_requested, sorted(commands))
                self._last_observation = observation
        # Never acquire the router lock while holding the adapter lock.
        if changed and self._activity_callback is not None:
            self._activity_callback(session)
        if status_changed and self._status_callback is not None:
            self._status_callback()

    def _normalized_state(self, player_state, remote_enabled):
        if self._owner is None:
            return 'unavailable'
        if not self._session:
            return 'stopped'
        if remote_enabled and player_state in ('Playing', 'Paused', 'Stopped'):
            return player_state.lower()
        return 'playing' if self._active else 'stopped'

    def disconnected(self):
        self.update(None, False)

    def get_status(self):
        with self._lock:
            return {'state': self._state, 'has_media': self._session}

    def invoke(self, command, *args, **kwargs):
        started = time.monotonic()
        with self._lock:
            if not self.supports(command) or self._transport is None:
                logger.warning("AirPlay command '%s' is unavailable", command)
                return False
            owner, client = self._owner, self._client
            logger.debug('AirPlay command: command=%s owner=%s state=%s active=%s session=%s',
                         command, owner, self._state, self._active, self._session)
        try:
            if command == 'stop':
                self._transport.drop_session(owner)
                with self._lock:
                    if (owner, client) == (self._owner, self._client):
                        self._stop_requested = True
                        self.capabilities = frozenset()
            else:
                self._transport.remote_command(owner, client, REMOTE_COMMANDS[command])
        except Exception as error:
            logger.warning("AirPlay %s failed: owner=%s elapsed_ms=%.1f error=%s",
                           command, owner, (time.monotonic() - started) * 1000, error, exc_info=True)
            return False
        # Do not fabricate playback state from a command acknowledgement.
        # In particular, keep the observed Active edge across DropSession's
        # grace period so the next snapshot cannot steal control back.
        logger.debug('AirPlay command acknowledged: command=%s owner=%s elapsed_ms=%.1f; awaiting receiver state',
                     command, owner, (time.monotonic() - started) * 1000)
        return True


class ShairportDBusTransport:
    """A monitor-owned connection; commands use a separate bounded connection.

    This keeps router commands independent of the monitor thread, which may be
    waiting for the router lock while reporting an activity transition.
    """

    def __init__(self):
        self.bus = None
        self.owner = None
        self.can_stop = False
        self.can_command = False
        self.has_remote = False
        self._remote_failed = False

    async def _connect(self):
        from dbus_next import BusType
        from dbus_next.aio import MessageBus
        return await MessageBus(bus_type=BusType.SYSTEM).connect()

    @staticmethod
    async def _call(bus, destination, path, interface, member, signature='', body=None):
        from dbus_next import Message, MessageType
        reply = await bus.call(Message(destination=destination, path=path, interface=interface,
                                       member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f'{reply.error_name}: {reply.body}')
        return reply.body

    async def snapshot(self):
        if self.bus is None:
            self.bus = await self._connect()
        owner = (await self._call(self.bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                                  'org.freedesktop.DBus', 'GetNameOwner', 's', [SERVICE]))[0]
        if owner != self.owner:
            node = await self.bus.introspect(owner, PATH)
            interface = next(i for i in node.interfaces if i.name == SERVICE)
            self.can_stop = any(m.name == 'DropSession' for m in interface.methods)
            self.can_command = any(m.name == 'RemoteCommand' for m in interface.methods)
            self.has_remote = any(i.name == REMOTE for i in node.interfaces)
            self.owner = owner
            logger.info('Shairport interface discovered: owner=%s can_stop=%s can_command=%s has_remote=%s',
                        owner, self.can_stop, self.can_command, self.has_remote)
        properties = (await self._call(self.bus, owner, PATH, PROPERTIES, 'GetAll', 's', [SERVICE]))[0]
        active = properties['Active'].value
        if not isinstance(active, bool):
            raise ValueError('Shairport Sync Active must be a boolean')
        remote = {}
        protocol = self._value(properties, 'Protocol')
        if protocol == 'AirPlay' and self.has_remote and self.can_command:
            try:
                remote = await asyncio.wait_for(self._remote_properties(self.bus, owner), timeout=1)
                if self._remote_failed:
                    logger.info('AirPlay remote properties recovered: owner=%s', owner)
                self._remote_failed = False
            except Exception as error:
                # A broken optional remote interface must not lose an audible
                # receiver's claim or remove its receiver-local Stop control.
                if not self._remote_failed:
                    logger.warning('AirPlay remote properties unavailable: owner=%s error=%s', owner, error,
                                   exc_info=True)
                self._remote_failed = True
        return owner, active, self.can_stop, {
            'protocol': protocol, 'can_command': self.can_command,
            'available': self._value(remote, 'Available') is True,
            'player_state': self._value(remote, 'PlayerState'),
            'client': self._value(remote, 'Client') or '',
        }

    @staticmethod
    def _value(properties, name):
        value = properties.get(name)
        return value.value if value is not None else None

    async def _remote_properties(self, bus, owner):
        return (await self._call(bus, owner, PATH, PROPERTIES, 'GetAll', 's', [REMOTE]))[0]

    def close(self):
        if self.bus is not None:
            logger.debug('Closing Shairport monitor connection: owner=%s', self.owner)
            self.bus.disconnect()
        self.bus = None
        self.owner = None
        self._remote_failed = False

    def drop_session(self, owner):
        asyncio.run(asyncio.wait_for(self._drop_session(owner), timeout=3))

    def remote_command(self, owner, client, command):
        if command not in REMOTE_COMMANDS.values():
            raise ValueError('Unsupported AirPlay remote command')
        asyncio.run(asyncio.wait_for(self._remote_command(owner, client, command), timeout=3))

    async def _remote_command(self, owner, client, command):
        bus = await self._connect()
        try:
            # Recheck availability and client immediately before dispatch: the
            # monitor's last snapshot may belong to a different sender.
            protocol = (await self._call(bus, owner, PATH, PROPERTIES, 'Get', 'ss', [SERVICE, 'Protocol']))[0]
            remote = await self._remote_properties(bus, owner)
            if (protocol.value != 'AirPlay' or self._value(remote, 'Available') is not True
                    or (self._value(remote, 'Client') or '') != client):
                raise RuntimeError('Classic remote control is no longer available for this client')
            reply = await self._call(bus, owner, PATH, SERVICE, 'RemoteCommand', 's', [command])
            logger.debug('AirPlay DACP response: owner=%s command=%s code=%r',
                         owner, command, reply[0] if reply else None)
            # The named Play/Pause/etc. methods discard the DACP response.
            # RemoteCommand preserves its HTTP-style code (including 49x
            # connection errors and zero when DACP is not compiled in).
            if not reply or type(reply[0]) is not int or not 200 <= reply[0] < 300:
                raise RuntimeError(f'DACP command rejected or failed (reply code {reply[0] if reply else None})')
        finally:
            bus.disconnect()

    async def _drop_session(self, owner):
        bus = await self._connect()
        try:
            await self._call(bus, owner, PATH, SERVICE, 'DropSession')
        finally:
            bus.disconnect()


class AirPlayMediaMonitor:
    """Poll Active every 0.5 seconds; reconnect after daemon/bus loss.

    Polling uses complete snapshots, so missing/invalidated properties cannot
    leave a stale claim. Every bus operation has a three-second deadline.
    """

    def __init__(self, source, transport=None):
        self.source = source
        self.transport = transport or ShairportDBusTransport()
        self.source._transport = self.transport
        self._stopped = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            logger.info('Starting AirPlay media monitor: service=%s poll_s=0.5 retry_s=2 timeout_s=3', SERVICE)
            self._thread = threading.Thread(target=self._run, name='AirPlayMediaMonitor', daemon=True)
            self._thread.start()

    def stop(self):
        self._stopped.set()
        logger.info('Stopping AirPlay media monitor')
        return self._thread

    def _run(self):
        loop = asyncio.new_event_loop()
        failed = False
        try:
            while not self._stopped.is_set():
                try:
                    snapshot = loop.run_until_complete(asyncio.wait_for(self.transport.snapshot(), timeout=3))
                    if not self._stopped.is_set():
                        self.source.update(*snapshot)
                    if failed:
                        logger.info('AirPlay receiver recovered: owner=%s', snapshot[0])
                    failed = False
                except Exception as error:
                    self.source.disconnected()
                    self.transport.close()
                    if not failed:
                        logger.warning('AirPlay receiver unavailable; retrying in 2s: %s', error, exc_info=True)
                    failed = True
                self._stopped.wait(2 if failed else 0.5)
        finally:
            self.transport.close()
            self.source.disconnected()
            loop.close()
            logger.info('AirPlay media monitor exited')
