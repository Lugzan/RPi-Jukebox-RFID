# Media routing and recovery

The router owns physical Next, Previous and Toggle commands. Starting local MPD
playback, Bluetooth playback or an AirPlay session claims routing and attempts to
stop the previous source. Unsupported commands do not fall through to another
player. A failed Stop is logged; the router still permits the requested handoff.

## Upgrading installed configurations

Before loading plugins, startup inserts `media: media` immediately after `player`
in `modules.named` if the media entry is absent. It preserves the remaining
settings, module order and YAML comments. The normal configuration save persists
the insertion; it also takes effect immediately on that startup. An explicit
media module mapping is preserved, and configurations without a player are left
alone. No manual replacement of `shared/settings/jukebox.yaml` is needed.

## Local playback

The media plugin attaches its router to the initialized MPD controller. The
actual MPD playback entry points acquire routing before MPD's lock, stop the
remote source, and start local playback while still holding the routing lock.
This covers RFID cards, folder/album/single playback from the Web UI, and local
resume/transport actions. Composite card/replay operations hold that lock but
defer claiming until they actually start playback. Card removal that only pauses
MPD, a no-op second swipe, or replay-if-stopped while already playing does not
steal an active remote session.

The MPD adapter resolves its controller once at initialization. Monitor threads
call that controller directly instead of acquiring the plugin registry lock
inside the router lock. RPC calls take the locks in the other order, so resolving
the controller this way avoids a lock cycle. MPD still works without an attached
router.

## Bluetooth

The BlueZ monitor reads `GetNameOwner` and a complete `GetManagedObjects` snapshot
every 0.5 seconds. It recursively decodes D-Bus variants, including Track metadata,
and replaces cached properties instead of preserving removed values. Missing
players release their sessions. A unique-owner change clears state and capabilities
before accepting the replacement daemon's snapshot.

Only an observed playing/seeking player starts a session. Once established, a
paused player retains its controls so the next Toggle resumes the phone. An
initially discovered paused phone or a connected headset does not claim routing.
Stop, player removal, or daemon/bus loss releases the source without resuming MPD.
After a successful Stop/handoff, late playing snapshots are ignored until the
player has been observed inactive; they cannot immediately take controls back.

Commands use separate, short-lived D-Bus connections addressed to the unique
daemon owner which supplied the session. Monitor callbacks run outside the
monitor's asyncio loop, and command replies do not depend on that loop. A monitor
waiting for the router cannot prevent an outstanding command from completing.
There is a three-second command/snapshot deadline, with up to one additional
second for connection cleanup. Timed-out calls are cancelled and never retried
automatically, since a Next/Toggle might already have executed remotely.

Connection/snapshot failures clear the source and close the monitoring connection.
The monitor retries every two seconds, including when BlueZ is absent at boot.
It logs the first failure and recovery, rather than every retry. Stop wakes the
poll/retry wait, and shutdown safely closes the event loop after bounded I/O.

Polling can miss an inactive interval shorter than 0.5 seconds and can allow brief
audio overlap before detecting remote playback. These adapters do not guarantee
exclusive audio before the first sample. Command acknowledgment also does not
prove that a sender changed playback; subsequent state and device testing matter.

## Validation

Platform-independent regression tests cover native variants, retained pauses,
late snapshots, daemon replacement, retry/cancellation/shutdown, real MPD entry
points with a mocked client, concurrent handoff and configuration migration:

```sh
.venv/bin/python -m pytest test/media test/cfghandler/test_configmigration.py
```

See [Device diagnostics](device-diagnostics.md) for real-device test sequences and
the log collection command. These tests do not replace Pi audio/BlueZ/reader tests.
