"""Installed configuration upgrades preserve user choices and load ordering."""
from io import StringIO

import pytest
from ruamel.yaml import YAML

from jukebox.cfghandler import ConfigHandler
from jukebox.configmigration import migrate_media_module


@pytest.mark.parametrize('roundtrip', [False, True])
def test_missing_router_is_inserted_after_player_and_migration_is_idempotent(roundtrip):
    yaml = YAML(typ='rt' if roundtrip else 'safe')
    cfg = ConfigHandler('migration')
    cfg.config_dict(yaml.load('''modules:
  named:
    publishing: publishing
    player: playermpd # Keep my comment
    cards: rfid.cards
    gpio: gpio.gpioz.plugin
airplay_media: {enable: false}
system: {box_name: My box}
'''))
    assert migrate_media_module(cfg)
    assert list(cfg.getn('modules', 'named')) == ['publishing', 'player', 'media', 'cards', 'gpio']
    assert cfg.getn('modules', 'named', 'media') == 'media'
    assert cfg.getn('airplay_media', 'enable') is False
    assert cfg.getn('system', 'box_name') == 'My box'
    assert cfg.is_modified()
    assert not migrate_media_module(cfg)
    if roundtrip:
        stream = StringIO()
        yaml.dump(cfg.getn('modules'), stream)
        assert '# Keep my comment' in stream.getvalue()


@pytest.mark.parametrize('modules', [{}, {'gpio': 'gpio'}, {'player': 'custom', 'media': 'custom_router'}])
def test_absent_player_or_explicit_router_is_preserved(modules):
    cfg = ConfigHandler('migration')
    cfg.config_dict({'modules': {'named': modules}})
    assert not migrate_media_module(cfg)
    assert cfg.getn('modules', 'named') == modules
    assert not cfg.is_modified()
