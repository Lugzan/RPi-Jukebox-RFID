"""Collector resilience and settings filtering without Linux services."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


spec = importlib.util.spec_from_file_location(
    'collect_device_diagnostics', Path(__file__).resolve().parents[2] / 'tools' / 'collect_device_diagnostics.py')
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


@pytest.mark.parametrize('error', [FileNotFoundError('missing command'), subprocess.TimeoutExpired(['fake'], 10)])
def test_command_failure_is_recorded(monkeypatch, tmp_path, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(diagnostics.subprocess, 'run', fail)
    assert type(error).__name__ in diagnostics.run_command(['fake'], tmp_path)


def test_collect_preserves_rotated_logs_and_omits_unrelated_settings(monkeypatch, tmp_path):
    root, output = tmp_path / 'repo', tmp_path / 'bundle'
    settings = root / 'shared' / 'settings'
    logs = root / 'shared' / 'logs'
    settings.mkdir(parents=True)
    logs.mkdir()
    output.mkdir()
    (settings / 'jukebox.yaml').write_text(
        'modules: {named: {media: media}}\nairplay_media: {enable: true}\n'
        'pulse:\n  toggle_on_connect: false\n  soft_max_volume: 70\n  password: SECRET\n'
        '  outputs:\n    primary:\n      pulse_sink_name: alsa_output.test\n'
        '      volume_limit: 80\n      password: SECRET\n'
        'wifi: {password: SECRET}\n')
    (settings / 'rfid.yaml').write_text(
        'rfid:\n  readers:\n    read_00:\n      module: pn532_uart\n'
        '      config: {device: /dev/ttyUSB0, log_all_cards: false, password: SECRET}\n')
    (settings / 'gpio.yaml').write_text(
        'input_devices:\n  PlayPause:\n    type: Button\n    kwargs: {pin: 10, pull_up: false}\n'
        '    actions: {on_press: {alias: toggle, args: SECRET}}\n')
    (logs / 'app.log').write_text('current session\n')
    (logs / 'app.log.1').write_text('previous session\n')
    (logs / 'outside.log').symlink_to(settings / 'jukebox.yaml')
    commands = []

    def record_command(command, cwd):
        commands.append(command)
        return 'command unavailable\n'

    monkeypatch.setattr(diagnostics, 'run_command', record_command)
    diagnostics.collect(root, output, '5 minutes ago')
    summary = json.loads((output / 'settings-summary.json').read_text())
    assert summary['jukebox']['airplay_media']['enable'] is True
    assert summary['jukebox']['pulse']['toggle_on_connect'] is False
    assert summary['jukebox']['pulse']['soft_max_volume'] == 70
    assert summary['jukebox']['pulse']['outputs']['primary']['pulse_sink_name'] == 'alsa_output.test'
    assert summary['jukebox']['pulse']['outputs']['primary']['volume_limit'] == 80
    assert summary['rfid']['read_00']['config']['device'] == '/dev/ttyUSB0'
    assert summary['gpio']['PlayPause']['kwargs']['pin'] == 10
    assert summary['gpio']['PlayPause']['actions'] == ['on_press']
    assert 'SECRET' not in (output / 'settings-summary.json').read_text()
    assert (output / 'logs' / 'app.log.1').read_text() == 'previous session\n'
    assert not (output / 'logs' / 'outside.log').exists()
    assert (output / 'user-journal.txt').read_text() == 'command unavailable\n'
    assert (output / 'reproduction.txt').exists()
    for report in ('sink-details', 'sources', 'cards', 'playback-streams', 'capture-streams'):
        assert (output / f'audio-{report}.txt').read_text() == 'command unavailable\n'
    for object_type in ('sinks', 'sources', 'cards', 'sink-inputs', 'source-outputs'):
        assert ['pactl', 'list', object_type] in commands
    user_commands = [command for command in commands if '--user' in command]
    assert {command[0] for command in user_commands} == {'systemctl', 'journalctl'}
    for command in user_commands:
        assert 'jukebox-daemon.service' in command
        assert 'jukebox.service' not in command
