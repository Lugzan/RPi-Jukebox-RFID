# Optional AirPlay media source

The media router can monitor a separately installed **Shairport Sync** receiver.
This integration is disabled by default and does not install, start, or configure
a receiver. Bluetooth adapters and system volume controls are independent.

## Supported interface and modes

The target is Shairport Sync **5.5.2**, in either Classic AirPlay or AirPlay 2 mode,
built with `--with-dbus-interface` and installed as a system service. Compatibility
is based on its native `org.gnome.ShairportSync` D-Bus interface: boolean `Active`
and method `DropSession` at `/org/gnome/ShairportSync`. Older or newer releases
with that same interface may work, but have not been qualified. The implementation
has mocked tests; receiver/hardware testing is still required on the target Pi.

The source reports `playing`, `stopped`, or `unavailable` and `has_media`.
It advertises only `stop`, and only while active and when introspection finds
`DropSession`. Stop **disconnects the incoming session locally**; the sender may
show a connection error. It is not a remote pause and cannot be resumed locally.

Play, Pause, Toggle, Next and Previous are unsupported in this initial adapter.
Classic AirPlay has sender-dependent DACP commands, but no reliable per-command
capability negotiation; AirPlay 2 does not provide those remote controls reliably.
Unsupported button presses are logged and never fall through to MPD. Metadata,
remote volume and sender control are not implemented.

## Setup

1. Install and configure Shairport Sync separately, following its maintained
   [build instructions](https://github.com/mikebrady/shairport-sync/blob/5.5.2/BUILD.md).
   Distribution packages are usable only if they include the native D-Bus interface.
   Choose an audio output compatible with your existing MPD/PulseAudio setup;
   this adapter does not change audio-device permissions or mixing configuration.
2. Ensure `dbus-next` is installed in the Jukebox Python environment. It is already
   in this project's `requirements.txt`; no additional Python dependency is added.
3. Verify access as the user running Jukebox (not just as root):

   ```sh
   dbus-send --system --print-reply --dest=org.gnome.ShairportSync \
     /org/gnome/ShairportSync org.freedesktop.DBus.Properties.Get \
     string:org.gnome.ShairportSync string:Active
   ```

4. Set this in your `shared/settings/jukebox.yaml` and restart Jukebox:

   ```yaml
   airplay_media:
     enable: true
   ```

No shell hooks, MQTT broker, metadata-reader daemon or changes to the installation
scripts are required. Missing D-Bus support, access errors or a stopped receiver
leave AirPlay unavailable, with retries and a warning when the failure begins.

## Ownership and timing

The monitor reads complete `Active` snapshots every 0.5 seconds. `Active=true`
means audio activity, including Shairport's inactivity grace period. An idle
connection alone does not claim control. A rising edge claims the router, stopping
the previous source before subsequent physical controls are routed to AirPlay.
An already-active receiver is also recognized at Jukebox startup.

Shairport normally keeps Active true for `active_state_timeout` (default 10
seconds) after audio stops, covering gaps between tracks. The router retains
AirPlay ownership during this period. Inactive, daemon disappearance, invalid
snapshots or bus failure release ownership to MPD **without resuming playback**.
Read/command deadlines are three seconds; recovery retries occur every two seconds.
The monitor also releases ownership on shutdown.

Repeated active snapshots do not reclaim from another source after a handoff.
A new inactive-to-active transition or a replacement daemon owner permits a new
claim. All audio within one Active interval is treated as one session; sender
changes within the grace period are not distinct sessions. A complete inactive
interval shorter than the polling interval may be missed. Status `playing` thus
means receiver-active, not a sample-accurate assertion that sound is audible.
For strict pre-audio exclusivity, this polling adapter is insufficient: incoming
audio may overlap the old source until the next poll and stop command completes.

Stop targets the observed unique D-Bus owner, avoiding accidental termination of
a replacement daemon. Failed Stop calls are logged and return false. The existing
router still allows a handoff if the previous source cannot stop; this adapter
does not change that shared policy.

## Verification and references

Run only the platform-independent tests on macOS:

```sh
python -m pytest test/media/test_airplay.py test/media/test_media_router.py
```

On a Pi, verify idle connection, playback claim, inactivity release, daemon restart,
and Stop with each intended sender. Confirm that MPD stays stopped after release.

Authoritative interface references:

- [Native D-Bus schema, version 5.5.2](https://github.com/mikebrady/shairport-sync/blob/5.5.2/org.gnome.ShairportSync.xml)
- [D-Bus usage and DropSession](https://github.com/mikebrady/shairport-sync/blob/master/documents/sample%20dbus%20commands)
- [Active/inactive events and grace period](https://github.com/mikebrady/shairport-sync/blob/master/ADVANCED%20TOPICS/Events.md)
- [Receiver modes and Classic remote controls](https://github.com/mikebrady/shairport-sync)
