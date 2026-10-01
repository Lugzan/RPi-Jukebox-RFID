# Optional AirPlay media source

The media router uses **Shairport Sync** to receive AirPlay audio. The installer
now offers an optional receiver setup, defaulting to No. Choosing Yes installs
and configures Classic AirPlay and enables the adapter. Bluetooth adapters and
system volume controls are independent.

## Supported interface and modes

The target is Shairport Sync **5.5.2**, in either Classic AirPlay or AirPlay 2 mode,
built with `--with-dbus-interface` and exposing its native interface on the system bus. Compatibility
is based on its native `org.gnome.ShairportSync` D-Bus interface: boolean `Active`
and method `DropSession` at `/org/gnome/ShairportSync`. Older or newer releases
with that same interface may work, but have not been qualified. The implementation
has mocked tests; receiver/hardware testing is still required on the target Pi.

The source reports `playing`, `paused`, `stopped`, or `unavailable` and `has_media`.
Capabilities depend on the observed receiver and session:

| Receiver/session | Controls |
| --- | --- |
| Classic (`Protocol=AirPlay`), remote available, active or retained paused session | Play, Pause, Toggle, Next, Previous, Stop |
| AirPlay 2, unknown protocol, or remote unavailable | Stop while receiver-active |
| Idle, disconnected, or stopped after a completed release | None |

Remote controls require `RemoteControl.Available=true` and an introspected
`RemoteCommand` method. `Protocol` describes the **receiver build**, so AirPlay 2
builds remain Stop-only even if they accept a Classic sender. Experimental
AirPlay 2 remote control is not enabled by this adapter.

Classic commands use the same DACP requests as the named D-Bus methods:
`play`, `pause`, `playpause`, `nextitem` and `previtem`. We send these through
`org.gnome.ShairportSync.RemoteCommand`, because the named methods discard the
sender's response code. Only a 2xx response counts as accepted; rejection,
connection failures (including Shairport's custom 49x codes), malformed replies
and timeouts are logged and return false. There is no automatic retry: retrying
a timed-out Toggle or Next could execute it twice. Acceptance is not proof of
playback change; subsequent receiver snapshots determine status.

`Available` describes the remote-control connection, not per-command sender
support. Advertised Classic capabilities mean these commands can be attempted;
a sender may still reject an individual command. Rejection does not permanently
remove a capability, since sender behavior may depend on playback state. Before
each remote command, the adapter rechecks protocol, availability and client
identity. The unique D-Bus owner is pinned, and commands cannot silently switch
to a replacement daemon. Shairport exposes no atomic session-targeted command,
so a sender change after the final check remains a race.

Stop uses `DropSession` when introspection finds it. It **disconnects the incoming
session locally**, including for AirPlay 2; the sender may show a connection
error. Stop is not a remote pause and cannot be resumed locally. Unsupported
button presses are logged and never fall through to MPD. Metadata and volume
controls are not implemented.

## Optional installer setup

On a fresh Raspberry Pi OS / Debian installation, answer **Yes** to
**Install the AirPlay receiver? [y/N]**. No manual AirPlay configuration is then
needed. After installation, select **Phoniebox (hostname)** on an AirPlay sender
on the same local network. Normal Phoniebox audio-device setup still applies.

For scripted installations, `ENABLE_AIRPLAY=true` selects this step without
asking the AirPlay question; other installation questions remain unchanged.
A No answer installs no receiver packages, services or D-Bus policy and leaves
the adapter disabled.

For an already installed Phoniebox, run this from the repository as the same
normal user that runs Jukebox (the script requests sudo for system changes):

```sh
bash installation/components/setup_airplay.sh
```

This uses the same setup routine without rerunning the full installer or
replacing existing Jukebox settings. The routine refuses to overwrite an existing
Shairport Sync or managed AirPlay installation; integrate those manually below.
It is an initial setup command, not an upgrade/reinstallation tool.

The setup downloads the pinned upstream **5.5.2** source archive, verifies its
SHA-256, and builds Classic mode with FFmpeg, PulseAudio, Avahi, native D-Bus and
DACP support. This takes several minutes, especially on older Pis. It installs
build dependencies only when selected; no NQPTP or AirPlay 2 service is installed.
Distribution receiver packages are not used because build features vary.
Source-built receiver upgrades are not delivered through apt; updating the
pinned version/checksum requires reviewing upstream changes and retesting.

Installed files and services:

- `/opt/phoniebox-airplay/bin/shairport-sync`: dedicated receiver binary.
- `/etc/phoniebox-airplay.conf`: receiver settings and advertised name.
- `~/.config/systemd/user/phoniebox-airplay.service`: user service sharing
  Jukebox's PulseAudio server and default output, enabled at boot via lingering.
- `/etc/dbus-1/system.d/phoniebox-airplay.conf`: grants only the Jukebox user
  ownership and access to the receiver's native system-bus interface.

The routine starts Avahi and the receiver, checks native D-Bus properties and
methods as the Jukebox user, then sets `airplay_media.enable: true` while preserving
other settings and restarts Jukebox. If verification fails, setup reports failure
and stops the receiver without enabling the adapter. Installation files and
build dependencies remain for diagnosis; no automatic removal is attempted.

Inspect receiver failures with:

```sh
systemctl --user status phoniebox-airplay.service
journalctl --user -u phoniebox-airplay.service
```

To disable reception, run `systemctl --user disable --now phoniebox-airplay.service`,
set `airplay_media.enable: false`, and restart Jukebox. This leaves the installed
files and shared audio/network services intact.

## Manual receiver setup

Use this path for existing receivers, custom output configuration or AirPlay 2.

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

No shell hooks, MQTT broker or metadata-reader daemon are required. Missing D-Bus support, access errors or a stopped receiver
leave AirPlay unavailable, with retries and a warning when the failure begins.

## Ownership and timing

The monitor reads complete `Active` snapshots every 0.5 seconds. `Active=true`
means audio activity, including Shairport's inactivity grace period. An idle
connection alone does not claim control. A rising edge claims the router, stopping
the previous source before subsequent physical controls are routed to AirPlay.
An already-active receiver is also recognized at Jukebox startup.

Shairport normally keeps Active true for `active_state_timeout` (default 10
seconds) after audio stops, covering gaps between tracks. The router retains
AirPlay ownership during this period. For Classic playback, the reported
`PlayerState` distinguishes a confirmed pause from receiver activity.

After actual playback has claimed the router, a known Classic client reporting
`Paused` with remote control still available retains its existing routing even
when Active becomes false. This permits Play/Toggle to resume after the grace
period. Merely discovering a paused or connected client never claims control.
The pause retention ends on Stop, changed/missing client identity, lost remote
availability, stopped/unknown player state, or receiver loss. Senders which tear
down playback instead of reporting Paused cannot retain pause/resume routing.

Other inactive sessions, daemon disappearance, invalid core snapshots or bus
failure release ownership to MPD **without resuming playback**. Failures reading
the optional remote interface disable remote controls while preserving an active
receiver's local Stop capability. Read/command deadlines are three seconds;
optional remote snapshots get a one-second deadline. Recovery retries occur
every two seconds. The monitor also releases ownership on shutdown.

Repeated active or paused snapshots do not reclaim from another source after a
handoff. A new inactive-to-active transition or replacement daemon owner permits
a new claim. All audio within one Active interval is treated as one session;
sender changes within that interval are not separate activity claims. A complete
inactive interval shorter than the polling interval may be missed. When remote
state is unavailable, `playing` means receiver-active, including its grace period,
not a sample-accurate assertion that sound is audible. Status and capability
changes are published even when ownership stays unchanged.

For strict pre-audio exclusivity, this polling adapter is insufficient: incoming
audio may overlap the old source until the next poll and stop command completes.
Failed Stop calls are logged and return false. The existing router still allows
a handoff if the previous source cannot stop; this adapter does not change that
shared policy.

## Verification and references

Run only the platform-independent tests on macOS:

```sh
python -m pytest test/installation/test_airplay_setup.py test/media/test_airplay.py test/media/test_media_router.py
```

The installer tests mock Linux system commands; a real source build and boot
verification must still be performed on Raspberry Pi OS.

On a Pi, verify idle connection, playback claim, inactivity release, daemon restart,
and Stop with each intended sender. For Classic builds, also verify each remote
command, pause longer than the inactivity timeout, resume with a physical button,
and disconnect while paused. Confirm that MPD stays stopped after release.

Authoritative interface references:

- [Native D-Bus schema, version 5.5.2](https://github.com/mikebrady/shairport-sync/blob/5.5.2/org.gnome.ShairportSync.xml)
- [D-Bus usage and DropSession](https://github.com/mikebrady/shairport-sync/blob/master/documents/sample%20dbus%20commands)
- [Active/inactive events and grace period](https://github.com/mikebrady/shairport-sync/blob/master/ADVANCED%20TOPICS/Events.md)
- [Receiver modes and Classic remote controls](https://github.com/mikebrady/shairport-sync)

- [D-Bus command handlers and result propagation, 5.5.2](https://github.com/mikebrady/shairport-sync/blob/5.5.2/dbus-service.c)
- [DACP response handling, 5.5.2](https://github.com/mikebrady/shairport-sync/blob/5.5.2/dacp.c)
