# Diagnosing tests on the Raspberry Pi

The media, GPIO and PN532 diagnostics use the existing application logger. They
record input actions, source selection, supported commands, D-Bus state changes,
command results and duration, recovery, and exception tracebacks. A returned or
acknowledged command does not prove that playback changed; compare the following
receiver state and what you heard.

## Before testing

Use the normal Jukebox service and its `shared/settings/logger.yaml`. The default
logger already writes DEBUG messages to `shared/logs/app.log`, with rotation.
If using a customized logger, merge these levels into its existing `loggers`
mapping, and ensure its file handler accepts DEBUG messages:

```yaml
jb.media:
  level: DEBUG
jb.gpioz:
  level: DEBUG
jb.rfid:
  level: DEBUG
```

Keep the `jb` file handler and propagation from these child loggers. Restart
Jukebox once after deploying the changes/configuration:

```sh
systemctl --user restart jukebox-daemon.service
```

The foreground `run_jukebox.sh -vv` option uses console logging instead of the
configured file handlers, so it does not produce the same app.log bundle.
For a custom log location, include those files separately.

Look for `Media router configuration`, `Registered media source`, and `PN532 UART
configured` at startup. Existing installations automatically insert the missing
`media: media` entry after `player` before loading input components. The migration
logs `media added after player` and preserves your other settings. An explicitly
configured media mapping is retained.

## Reproduce and collect

Note the time, including time zone, and keep the failing sequence short. Collect
immediately after the failure, before restarting repeatedly: logs rotate on
application startup as well as size. From the repository, as the normal Jukebox
user, run:

```sh
.venv/bin/python tools/collect_device_diagnostics.py --since '30 minutes ago'
```

This prints the path of a new `/tmp/phoniebox-diagnostics-*.tar.gz` archive and
an unpacked directory. It gathers current/rotated application logs, bounded
service and kernel journal excerpts, Git revision and dirty status, dependency
versions, USB and audio status, and selected configuration fields. It uses no
sudo, changes no services/settings, and records unavailable commands or access
errors while continuing. Each external command has a ten-second timeout.
Package versions come from the Python environment used to run the collector.

Logs retain existing card IDs, device addresses, names and media information;
review the archive before sharing it. Raw settings, card assignments, environment
variables and credentials are not deliberately collected. The settings summary
includes reader paths, GPIO pin parameters and action names, module loading,
audio output names and volume limits, automatic output switching, and logging
levels. It omits arbitrary RPC arguments. Restricted system journals may
be missing; their report files explain why.

For Bluetooth volume failures, collect while the sender is still connected and
attempting playback. The detailed PulseAudio reports include sink/source volume
and mute state, card profiles, and playback/capture streams. These help separate
speaker-volume changes from changes to the incoming Bluetooth stream. They may
also include connected device names and media titles. The reports are snapshots;
correlate them with `GPIO action triggered` and volume commands in `app.log` to
identify repeated button actions during a reported drop. Record whether volume
changes on the sender, in the web UI, or only audibly.

Send the archive with:

- The failure time and the exact order of card taps, button presses, connections
  and disconnects.
- What you expected and what actually happened, including any delay or audio
  overlap.
- The sender app/phone OS, and PN532 wiring and HSU switch positions if relevant.
- Whether restarting or reconnecting recovered the device.

The unpacked `reproduction.txt` is an optional template for those details. If you
edit/redact the unpacked directory, recreate the archive before sharing it.

## Useful test sequences

| Area | Sequence |
| --- | --- |
| PN532 | Tap a known card, leave it placed, remove it, tap again; unplug/reconnect USB and repeat. |
| Touch controls | Press each control; hold volume up/down; press the volume pair together. |
| Bluetooth | Start music, Next, Previous, Toggle twice with a pause between, disconnect/reconnect. |
| AirPlay Classic | Start music, Next/Previous, pause longer than 10 seconds, resume, then Stop. |
| Handoff | MPD → remote playback → RFID local playback; check audio and where the next physical button goes. |
| Recovery | Restart the receiver/BlueZ during testing and check whether routing and controls recover. |

PN532 emits cumulative scan/target/card/error totals about every 30 seconds,
plus connection attempts. No targets while scanning suggests a reader/card/mode
problem; targets without cards points at activation. Idle scans and unchanged
AirPlay observations are not logged individually. Card IDs are not added to
PN532 summaries; the existing `log_all_cards` setting enables per-scan IDs when
needed, and the common RFID layer already logs recognized cards.

The original findings are recorded in [the review](unpushed-review.md).
The subsequent fixes and recovery behavior are described in
[Media routing and recovery](media-routing.md).
