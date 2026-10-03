"""Run real MPD entry points with a fake MPD client and no audio hardware."""
import sys
import threading
from unittest.mock import MagicMock

import pytest
import jukebox.plugs as plugin

allow_direct_imports = plugin.ALLOW_DIRECT_IMPORTS
plugin.ALLOW_DIRECT_IMPORTS = True
sys.modules['jukebox.publishing'] = MagicMock()
from components import playermpd  # noqa: E402
from components.media import MediaRouter, MpdMediaSource  # noqa: E402
from components.media.airplay import AirPlayMediaSource  # noqa: E402
from components.media.bluetooth import BluezMediaSource, BluezMediaMonitor, MEDIA_PLAYER_INTERFACE  # noqa: E402
plugin.ALLOW_DIRECT_IMPORTS = allow_direct_imports


@pytest.fixture
def playback(monkeypatch):
    # Avoid constructor I/O (MPD, persistent files and the polling thread), while
    # exercising the production public methods and adapter below.
    player = playermpd.PlayerMPD.__new__(playermpd.PlayerMPD)
    player._media_router = None
    player.mpd_lock = threading.RLock()
    player.mpd_client = MagicMock()
    player.mpd_status = {'state': 'stop', 'pos': '0', 'playlistlength': '3'}
    player.current_folder_status = {'CURRENTSONGPOS': 0, 'ELAPSED': 0}
    player.music_player_status = {'player_status': {'last_played_folder': 'album'}, 'audio_folder_status': {}}
    player.second_swipe_action = None
    player.stopped_next_action = player._next_in_stopped_state
    player.stopped_prev_action = player._prev_in_stopped_state
    player.end_of_playlist_next_action = lambda: None
    monkeypatch.setattr(playermpd, 'play_card_callbacks', MagicMock(), raising=False)
    monkeypatch.setattr(playermpd.components.player, 'get_music_library_path', lambda: '/music')
    collector = MagicMock()
    collector.__iter__.return_value = iter(['song.mp3'])
    monkeypatch.setattr(playermpd.playlistgenerator, 'PlaylistCollector', lambda *args: collector)

    router = MediaRouter()
    router.register_source(MpdMediaSource(player))
    player.set_media_router(router)
    transport = MagicMock()
    remote = AirPlayMediaSource(transport, lambda active: (
        router.claim_source('airplay') if active else router.release_source('airplay')))
    router.register_source(remote)
    remote.update(':1.1', True)
    events = []
    transport.drop_session.side_effect = lambda *args: events.append('remote-stop')
    player.mpd_client.play.side_effect = lambda *args: events.append('local-play')
    player.mpd_client.pause.side_effect = lambda *args: events.append('local-pause-or-resume')
    player.mpd_client.stop.side_effect = lambda *args: events.append('local-stop')
    return player, router, remote, events


@pytest.mark.parametrize('method,args', [
    ('play', ()), ('play_single', ('song.mp3',)), ('play_folder', ('album',)),
    ('play_album', ('Artist', 'Album')), ('play_card', ('album',)),
    ('resume', ()), ('replay', ()), ('rewind', ()), ('replay_if_stopped', ()),
    ('next', ()), ('prev', ()), ('toggle', ()), ('pause', (0,)),
])
def test_local_playback_stops_airplay_before_audio_and_routes_next_button(playback, method, args):
    player, router, remote, events = playback
    getattr(player, method)(*args)
    assert events[0] == 'remote-stop'
    assert len(events) >= 2
    assert router.get_active_source() == 'mpd'
    # A late active snapshot must not undo the handoff.
    remote.update(':1.1', True)
    router.toggle()
    assert events[-1] == 'local-pause-or-resume'
    assert events.count('remote-stop') == 1


@pytest.mark.parametrize('action', ['card-removal', 'noop-second-swipe', 'replay-while-playing'])
def test_actions_that_do_not_start_local_playback_leave_remote_owner(playback, action):
    player, router, _, events = playback
    if action == 'card-removal':
        player.pause(1)
    elif action == 'noop-second-swipe':
        player.second_swipe_action = lambda: None
        player.play_card('album')
    else:
        player.mpd_status['state'] = 'play'
        player.replay_if_stopped()
    assert router.get_active_source() == 'airplay'
    assert 'remote-stop' not in events


def test_nested_second_swipe_resume_claims_once(playback):
    player, router, _, events = playback
    player.second_swipe_action = player.resume
    player.play_card('album')
    assert events == ['remote-stop', 'local-play']
    assert router.get_active_source() == 'mpd'


@pytest.mark.parametrize('state', ['playing', 'paused'])
def test_card_playback_reclaims_from_bluetooth_and_survives_late_snapshot(playback, state):
    player, router, _, events = playback
    transport = MagicMock()
    bluetooth = BluezMediaSource(transport)
    bluetooth.set_activity_callback(lambda active: (
        router.claim_source('bluetooth') if active else router.release_source('bluetooth')))
    router.register_source(bluetooth)
    monitor = BluezMediaMonitor(bluetooth)
    monitor.process_managed_objects({'/player': {MEDIA_PLAYER_INTERFACE: {'Status': 'playing'}}})
    monitor.process_managed_objects({'/player': {MEDIA_PLAYER_INTERFACE: {'Status': state}}})
    events.clear()
    transport.call.side_effect = lambda *args: events.append('bluetooth-stop')
    player.play_card('album')
    assert events == ['bluetooth-stop', 'local-play']
    transport.call.assert_called_once_with('/player', 'Stop')
    monitor.process_managed_objects({'/player': {MEDIA_PLAYER_INTERFACE: {'Status': state}}})
    assert router.get_active_source() == 'mpd'
    router.toggle()
    assert events[-1] == 'local-pause-or-resume'


def test_standalone_mpd_still_works_without_router(playback):
    player, _, _, events = playback
    player.set_media_router(None)
    player.play_single('song.mp3')
    assert events == ['local-play']


def test_local_play_and_remote_claim_cannot_interleave(playback):
    player, router, _, events = playback
    play_started, allow_play, claim_started, claim_done = (threading.Event() for _ in range(4))

    def play():
        events.append('local-play-started')
        play_started.set()
        if allow_play.wait(2):
            events.append('local-play-finished')

    def claim():
        claim_started.set()
        router.claim_source('airplay')
        claim_done.set()

    player.mpd_client.play.side_effect = play
    playing = threading.Thread(target=player.play, daemon=True)
    claiming = threading.Thread(target=claim, daemon=True)
    playing.start()
    try:
        assert play_started.wait(1)
        claiming.start()
        assert claim_started.wait(1)
        assert not claim_done.wait(0.03)
    finally:
        allow_play.set()
        playing.join(1)
        if claiming.ident is not None:
            claiming.join(1)
    assert not playing.is_alive() and not claiming.is_alive()
    assert events == ['remote-stop', 'local-play-started', 'local-play-finished', 'local-stop']
    assert router.get_active_source() == 'airplay'


def test_router_transport_does_not_acquire_plugin_registry_lock(playback):
    _, router, _, events = playback
    router.claim_source('mpd')
    done = threading.Event()

    def toggle():
        router.toggle()
        done.set()

    thread = threading.Thread(target=toggle, daemon=True)
    try:
        # RPC holds this lock before entering the router. A monitor must not
        # acquire it in the opposite order while stopping/polling MPD.
        with plugin._lock_module:
            thread.start()
            assert done.wait(1)
    finally:
        thread.join(1)
    assert events[-1] == 'local-pause-or-resume'
