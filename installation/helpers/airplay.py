"""Verify optional receiver setup and enable only its Jukebox setting."""
import argparse
import asyncio
import os
from pathlib import Path
import stat
import tempfile
from xml.sax.saxutils import quoteattr


SERVICE = 'org.gnome.ShairportSync'
PATH = '/org/gnome/ShairportSync'


def policy(user):
    # Both processes run as the same user. No blanket permission for other users.
    return (f'<busconfig>\n  <policy user={quoteattr(user)}>\n'
            f'    <allow own="{SERVICE}"/>\n'
            f'    <allow send_destination="{SERVICE}"/>\n'
            '  </policy>\n</busconfig>\n')


def enable(path):
    from ruamel.yaml import YAML
    path = Path(path)
    yaml = YAML()
    with path.open() as stream:
        settings = yaml.load(stream)
    if not isinstance(settings, dict):
        raise ValueError('Jukebox settings must be a YAML mapping')
    section = settings.setdefault('airplay_media', {})
    if not isinstance(section, dict):
        raise ValueError('airplay_media must be a YAML mapping')
    section['enable'] = True
    # Keep unrelated settings/comments and file permissions, replace atomically.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = stream.name
            yaml.dump(settings, stream)
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


async def check_once():
    from dbus_next import BusType
    from dbus_next.aio import MessageBus
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        node = await bus.introspect(SERVICE, PATH)
        interface = next(i for i in node.interfaces if i.name == SERVICE)
        methods = {method.name for method in interface.methods}
        if not {'DropSession', 'RemoteCommand'} <= methods:
            raise RuntimeError('Receiver lacks required native D-Bus controls')
        proxy = bus.get_proxy_object(SERVICE, PATH, node)
        properties = proxy.get_interface('org.freedesktop.DBus.Properties')
        values = await properties.call_get_all(SERVICE)
        if values['Protocol'].value != 'AirPlay' or type(values['Active'].value) is not bool:
            raise RuntimeError('Expected a Classic AirPlay receiver with boolean Active status')
        remote = await properties.call_get_all(SERVICE + '.RemoteControl')
        if type(remote['Available'].value) is not bool:
            raise RuntimeError('Receiver lacks remote-control availability status')
    finally:
        bus.disconnect()
        await bus.wait_for_disconnect()


async def check():
    for attempt in range(10):
        try:
            await asyncio.wait_for(check_once(), timeout=3)
            return
        except Exception:
            if attempt == 9:
                raise
            await asyncio.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('policy', 'check', 'enable'))
    parser.add_argument('value', nargs='?')
    args = parser.parse_args()
    if args.action != 'check' and args.value is None:
        parser.error('policy/enable require a username/settings path')
    if args.action == 'policy':
        print(policy(args.value), end='')
    elif args.action == 'enable':
        enable(args.value)
    else:
        asyncio.run(check())


if __name__ == '__main__':
    main()
