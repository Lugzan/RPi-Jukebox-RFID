#!/usr/bin/env python3
"""Collect local, bounded Phoniebox diagnostics without sudo or service changes."""
import argparse
from importlib import metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]


def run_command(command, cwd):
    """Keep failures/timeouts in the report and continue collecting other data."""
    try:
        result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors='replace', timeout=10, check=False)
        return f'$ {command!r}\nexit_code={result.returncode}\n{result.stdout}'
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'$ {command!r}\n{type(error).__name__}: {error}\n'


def selected_settings(root):
    """Include wiring, routing and log levels; omit arbitrary RPC arguments and credentials."""
    from ruamel.yaml import YAML
    yaml = YAML(typ='safe')
    result = {}
    for name in ('jukebox', 'rfid', 'gpio', 'logger'):
        try:
            with (root / 'shared' / 'settings' / f'{name}.yaml').open() as stream:
                settings = yaml.load(stream) or {}
            if name == 'jukebox':
                result[name] = {'modules': settings.get('modules')}
                for section in ('bluetooth_media', 'airplay_media', 'bluetooth_audio_buttons', 'gpioz'):
                    result[name][section] = {'enable': settings.get(section, {}).get('enable')}
                pulse = settings.get('pulse') or {}
                result[name]['pulse'] = {
                    field: pulse.get(field)
                    for field in ('startup_volume', 'toggle_on_connect', 'soft_max_volume')
                }
                result[name]['pulse']['outputs'] = {
                    output: {field: values.get(field)
                             for field in ('alias', 'pulse_sink_name', 'volume_limit', 'soft_max_volume')}
                    for output, values in (pulse.get('outputs') or {}).items()
                }
            elif name == 'rfid':
                readers = settings.get('rfid', {}).get('readers', {})
                result[name] = {
                    key: {**{field: reader.get(field) for field in ('module', 'same_id_delay', 'place_not_swipe')},
                          'config': {field: reader.get('config', {}).get(field)
                                     for field in ('device', 'log_all_cards')}}
                    for key, reader in readers.items()
                }
            elif name == 'gpio':
                result[name] = {}
                for key, device in (settings.get('input_devices') or {}).items():
                    result[name][key] = {
                        'type': device.get('type'),
                        'kwargs': {field: device.get('kwargs', {}).get(field)
                                   for field in ('pin', 'a', 'b', 'pull_up', 'bounce_time', 'hold_time', 'hold_repeat')},
                        'actions': list(device.get('actions', {})),
                    }
            else:
                result[name] = {section: {key: {field: value.get(field)
                                               for field in ('level', 'handlers', 'filename', 'propagate')}
                                         for key, value in (settings.get(section) or {}).items()}
                                for section in ('loggers', 'handlers')}
        except Exception as error:
            result[name] = f'{type(error).__name__}: {error}'
    return result


def collect(root, destination, since):
    versions = {'collected_utc': datetime.now(timezone.utc).isoformat(),
                'python': sys.version, 'platform': platform.platform(), 'since': since}
    for package in ('dbus-next', 'nfcpy', 'pyserial', 'gpiozero', 'python-mpd2', 'ruamel.yaml'):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = 'not installed in this Python environment'
    (destination / 'versions.json').write_text(json.dumps(versions, indent=2) + '\n')
    try:
        settings = selected_settings(root)
    except Exception as error:
        settings = {'error': f'{type(error).__name__}: {error}'}
    (destination / 'settings-summary.json').write_text(json.dumps(settings, indent=2, default=str) + '\n')

    commands = {
        'revision': ['git', 'log', '-1', '--format=%H %s'],
        'working-tree': ['git', 'status', '--short', '--branch'],
        'local-changes': ['git', 'diff', 'HEAD', '--stat'],
        'os-release': ['cat', '/etc/os-release'],
        'user-groups': ['id'],
        'usb': ['lsusb'],
        'serial-devices': ['ls', '-l', '/dev/serial/by-id'],
        'audio-server': ['pactl', 'info'],
        'audio-sinks': ['pactl', 'list', 'short', 'sinks'],
        'audio-sink-details': ['pactl', 'list', 'sinks'],
        'audio-sources': ['pactl', 'list', 'sources'],
        'audio-cards': ['pactl', 'list', 'cards'],
        'audio-playback-streams': ['pactl', 'list', 'sink-inputs'],
        'audio-capture-streams': ['pactl', 'list', 'source-outputs'],
        'mpd': ['mpc', 'status'],
        'user-services': ['systemctl', '--user', '--no-pager', '--full', 'status',
                          'jukebox-daemon.service', 'mpd.service', 'pulseaudio.service', 'phoniebox-airplay.service'],
        'bluetooth-service': ['systemctl', '--no-pager', '--full', 'status', 'bluetooth.service'],
        'user-journal': ['journalctl', '--user', '--no-pager', '-o', 'short-iso-precise', '--since', since,
                         '-n', '2000', '-u', 'jukebox-daemon.service', '-u', 'phoniebox-airplay.service',
                         '-u', 'mpd.service', '-u', 'pulseaudio.service'],
        'bluetooth-journal': ['journalctl', '--no-pager', '-o', 'short-iso-precise', '--since', since,
                              '-n', '1000', '-u', 'bluetooth.service'],
        'kernel-journal': ['journalctl', '-k', '--no-pager', '-o', 'short-iso-precise',
                           '--since', since, '-n', '1000'],
    }
    for name, command in commands.items():
        print(f'Collecting {name}...', flush=True)
        (destination / f'{name}.txt').write_text(run_command(command, root))

    logs = destination / 'logs'
    logs.mkdir()
    copy_errors = []
    for path in sorted((root / 'shared' / 'logs').glob('*.log*')):
        if path.is_file() and not path.is_symlink():
            try:
                shutil.copy2(path, logs / path.name)
            except OSError as error:
                copy_errors.append(f'{path.name}: {error}')
    (destination / 'log-copy-errors.txt').write_text('\n'.join(copy_errors))
    (destination / 'reproduction.txt').write_text(
        'Fill in before sharing:\n'
        'Time of failure (with time zone):\n'
        'Steps / card taps / button presses, in order:\n'
        'Expected behavior:\n'
        'Actual behavior (including audible overlap, delays, LED behavior):\n'
        'Phone OS / sender app / reader wiring and HSU switch settings:\n'
        'Restart, disconnect or recovery attempts:\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', default='30 minutes ago', help='journalctl time range (default: 30 minutes ago)')
    parser.add_argument('--output', type=Path, default=Path(tempfile.gettempdir()), help='parent of the new bundle directory')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix='phoniebox-diagnostics-', dir=args.output))
    collect(ROOT, destination, args.since)
    archive = destination.with_suffix('.tar.gz')
    with tarfile.open(archive, 'w:gz') as bundle:
        bundle.add(destination, arcname=destination.name)
    archive.chmod(0o600)
    print(f'Diagnostics directory: {destination}\nArchive: {archive}')
    print('Review logs for personal data before sharing. Send reproduction details alongside the archive.')


if __name__ == '__main__':
    main()
