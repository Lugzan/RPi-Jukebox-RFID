"""Compatibility updates applied to installed settings before plugin loading."""
import logging


logger = logging.getLogger('jb.configmigration')


def migrate_media_module(cfg):
    """Insert the router after the player without replacing installed settings.

    Existing/custom media mappings are preserved. The normal configuration save
    persists this update; it also applies on every startup until then.
    """
    with cfg:
        modules = cfg.getn('modules', 'named', default={})
        if 'player' not in modules or 'media' in modules:
            return False
        index = list(modules).index('player') + 1
        if hasattr(modules, 'insert'):
            # ruamel's CommentedMap keeps comments and existing key order.
            modules.insert(index, 'media', 'media')
        else:
            items = list(modules.items())
            items.insert(index, ('media', 'media'))
            modules.clear()
            modules.update(items)
    logger.info('Updated installed module configuration: media added after player')
    return True
