"""Exercise the diagnostic wrapper through gpiozero's real callback binding."""
import functools
import logging

from gpiozero.pins.mock import MockFactory

from components.gpio.gpioz.core.input_devices import Button
import jukebox.utils


def test_touch_action_logs_device_and_preserves_bound_arguments(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(jukebox.utils, 'bind_rpc_command', lambda *args, **kwargs: functools.partial(calls.append, 'next'))
    caplog.set_level(logging.DEBUG, logger='jb.gpioz')
    button = Button(11, pull_up=False, pin_factory=MockFactory(), name='Next')
    try:
        button.set_rpc_actions({'on_press': {'alias': 'next_song'}})
        button.pin.drive_high()
        button.pin.drive_low()
        assert calls == ['next']
        assert 'GPIO action triggered: device=Next action=on_press' in caplog.text
    finally:
        button.close()
