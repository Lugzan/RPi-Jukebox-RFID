"""Lifecycle jingle selection tests without audio hardware."""
from unittest.mock import Mock

import pytest
import jukebox.plugs as plugin

allow_direct_imports = plugin.ALLOW_DIRECT_IMPORTS
plugin.ALLOW_DIRECT_IMPORTS = True
try:
    from components import jingle
finally:
    plugin.ALLOW_DIRECT_IMPORTS = allow_direct_imports


@pytest.fixture
def playback(monkeypatch):
    play = Mock()
    monkeypatch.setattr(jingle, 'play', play)
    return play


@pytest.mark.parametrize('event', ['startup', 'shutdown'])
def test_existing_file_path_is_preserved(event, monkeypatch, playback):
    path = '/sounds/jingle.wav'
    monkeypatch.setattr(jingle, 'cfg', {'jingle': {f'{event}_sound': path}})
    getattr(jingle, f'play_{event}')()
    playback.assert_called_once_with(path)


@pytest.mark.parametrize('event', ['startup', 'shutdown'])
def test_directory_selects_one_supported_file_per_call(event, tmp_path, monkeypatch, playback):
    candidates = [tmp_path / 'first.wav', tmp_path / 'second.mp3']
    for path in candidates + [tmp_path / 'notes.txt', tmp_path / 'ignored.WAV']:
        path.touch()
    nested = tmp_path / 'nested.wav'
    nested.mkdir()
    (nested / 'third.wav').touch()
    selections = iter(map(str, candidates))

    def choose(files):
        assert set(files) == set(map(str, candidates))
        return next(selections)

    monkeypatch.setattr(jingle.random, 'choice', choose)
    monkeypatch.setattr(jingle, 'cfg', {'jingle': {f'{event}_sound': str(tmp_path)}})
    for expected in candidates:
        playback.reset_mock()
        getattr(jingle, f'play_{event}')()
        playback.assert_called_once_with(str(expected))


def test_empty_directory_is_skipped(tmp_path, playback, caplog):
    (tmp_path / 'notes.txt').touch()
    jingle._play_lifecycle_sound(str(tmp_path))
    playback.assert_not_called()
    assert 'No .wav or .mp3 files' in caplog.text


def test_unreadable_directory_is_skipped(tmp_path, monkeypatch, playback, caplog):
    monkeypatch.setattr(jingle.os, 'scandir', Mock(side_effect=PermissionError('Access denied')))
    jingle._play_lifecycle_sound(str(tmp_path))
    playback.assert_not_called()
    assert 'Cannot read jingle directory' in caplog.text
