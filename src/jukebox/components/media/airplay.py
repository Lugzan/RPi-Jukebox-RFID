"""Optional Shairport Sync native D-Bus adapter (Classic and AirPlay 2).

Only receiver-local session termination is advertised. Sender-side DACP
controls are deliberately excluded because their availability varies by sender
and they are not reliably available for AirPlay 2.
"""
import asyncio
import logging
import threading

from . import MediaSource


logger = logging.getLogger('jb.media.airplay')
SERVICE = 'org.gnome.ShairportSync'
PATH = '/org/gnome/ShairportSync'
PROPERTIES = 'org.freedesktop.DBus.Properties'


class AirPlayMediaSource(MediaSource):
    """Claim on an Active rising edge; release on inactive or receiver loss.

    Active includes Shairport's configured inactivity grace period. Repeated
    active snapshots never reclaim control after another source takes over.
    """

    def __init__(self, transport=None, activity_callback=None):
        super().__init__('airplay', 'AirPlay', ())
        self._transport = transport
        self._activity_callback = activity_callback
        self._owner = None
        self._active = False
        self._lock = threading.RLock()

    def update(self, owner, active, can_stop=True):
        """Apply a validated snapshot; a changed D-Bus owner is a new session."""
        with self._lock:
            previous_owner, was_active = self._owner, self._active
            self._owner = owner
            self._active = bool(owner and active)
            self.capabilities = frozenset(('stop',)) if self._active and can_stop else frozenset()
            changed = was_active != self._active or (self._active and previous_owner != owner)
            is_active = self._active
        # Never acquire the router lock while holding the adapter lock.
        if changed and self._activity_callback is not None:
            self._activity_callback(is_active)

    def disconnected(self):
        self.update(None, False)

    def get_status(self):
        with self._lock:
            return {
                'state': 'unavailable' if self._owner is None else 'playing' if self._active else 'stopped',
                'has_media': self._active,
            }

    def invoke(self, command, *args, **kwargs):
        with self._lock:
            if command != 'stop' or not self.supports(command) or self._transport is None:
                logger.warning("AirPlay command '%s' is unavailable", command)
                return False
            owner = self._owner
        try:
            self._transport.drop_session(owner)
        except Exception as error:
            logger.warning('AirPlay DropSession failed: %s', error)
            return False
        # Wait for receiver inactivity; clearing Active here would make the
        # grace-period snapshot look like a fresh session and steal control.
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
            self.owner = owner
        properties = (await self._call(self.bus, owner, PATH, PROPERTIES, 'GetAll', 's', [SERVICE]))[0]
        active = properties['Active'].value
        if not isinstance(active, bool):
            raise ValueError('Shairport Sync Active must be a boolean')
        return owner, active, self.can_stop

    def close(self):
        if self.bus is not None:
            self.bus.disconnect()
        self.bus = None
        self.owner = None

    def drop_session(self, owner):
        asyncio.run(asyncio.wait_for(self._drop_session(owner), timeout=3))

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
            self._thread = threading.Thread(target=self._run, name='AirPlayMediaMonitor', daemon=True)
            self._thread.start()

    def stop(self):
        self._stopped.set()
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
                    failed = False
                except Exception as error:
                    self.source.disconnected()
                    self.transport.close()
                    if not failed:
                        logger.warning('AirPlay receiver unavailable: %s', error)
                    failed = True
                self._stopped.wait(2 if failed else 0.5)
        finally:
            self.transport.close()
            self.source.disconnected()
            loop.close()
