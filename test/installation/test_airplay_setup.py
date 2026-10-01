"""Installer contract tests. Never run apt, sudo, systemd or receiver hardware."""
import asyncio
import importlib.util
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('airplay_setup', ROOT / 'installation/helpers/airplay.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def shell(script, **environment):
    env = os.environ.copy()
    env.update({key: str(value) for key, value in environment.items()})
    return subprocess.run(['bash', '-c', script], cwd=ROOT, env=env, text=True, capture_output=True)


@pytest.mark.parametrize('choice,expected', [('', 'false'), ('n', 'false'), ('yes', 'true')])
def test_installer_option_is_opt_in(choice, expected):
    result = shell('''
source installation/routines/customize_options.sh
clear_c() { :; }; print_c() { :; }; log() { :; }
ENABLE_AIRPLAY=false
_option_airplay <<< "$CHOICE"
echo "$ENABLE_AIRPLAY"
''', CHOICE=choice)
    assert result.returncode == 0
    assert result.stdout.strip() == expected


def test_skipped_setup_runs_no_system_commands():
    result = shell('''
source installation/routines/setup_airplay.sh
run_with_log_frame() { exit 97; }
sudo() { exit 98; }
ENABLE_AIRPLAY=false
setup_airplay
''')
    assert result.returncode == 0


def test_explicit_environment_option_does_not_consume_answer():
    result = shell('''
source installation/includes/01_default_config.sh
source installation/routines/customize_options.sh
log() { :; }
_option_airplay
read -r following <<< next
echo "$ENABLE_AIRPLAY $following"
''', ENABLE_AIRPLAY='true')
    assert result.stdout.strip() == 'true next'


@pytest.mark.parametrize('failure', ['', 'preflight', 'install_dependencies', 'build', 'configure', 'start_and_verify'])
def test_setup_stops_on_failure_before_later_stages(failure):
    result = shell(r'''
source installation/routines/setup_airplay.sh
exit_on_error() { exit 9; }
systemctl() { echo cleanup; }
for stage in preflight install_dependencies build configure start_and_verify; do
    eval "_airplay_${stage}() { echo ${stage}; [[ \\"\$FAILURE\\" != ${stage} ]]; }"
done
_run_setup_airplay
'''.replace('\\"', '"'), FAILURE=failure)
    stages = ['preflight', 'install_dependencies', 'build', 'configure', 'start_and_verify']
    expected = stages if not failure else stages[:stages.index(failure) + 1]
    if failure == 'start_and_verify':
        expected += ['cleanup']
    assert result.stdout.splitlines() == expected, result.stderr
    assert result.returncode == (9 if failure else 0)


def test_probe_failure_never_enables_adapter_or_service(tmp_path):
    fake_python = tmp_path / 'bin/python'
    fake_python.parent.mkdir()
    fake_python.write_text('#!/bin/bash\necho "python $*"\nexit 1\n')
    fake_python.chmod(0o755)
    result = shell('''
source installation/routines/setup_airplay.sh
systemctl() { echo "systemctl $*"; }
print_lc() { :; }
_airplay_start_and_verify
''', VIRTUAL_ENV=tmp_path, INSTALLATION_PATH=ROOT, SETTINGS_PATH=tmp_path)
    assert result.returncode == 1
    assert '--user stop phoniebox-airplay.service' in result.stdout
    assert '--user enable' not in result.stdout
    assert ' enable ' not in result.stdout
    assert 'restart jukebox' not in result.stdout


def test_successful_probe_enables_before_restarting_jukebox(tmp_path):
    fake_python = tmp_path / 'bin/python'
    fake_python.parent.mkdir()
    fake_python.write_text('#!/bin/bash\necho "python $*"\n')
    fake_python.chmod(0o755)
    result = shell('''
source installation/routines/setup_airplay.sh
systemctl() { echo "systemctl $*"; }
_airplay_start_and_verify
''', VIRTUAL_ENV=tmp_path, INSTALLATION_PATH=ROOT, SETTINGS_PATH=tmp_path)
    assert result.returncode == 0
    assert result.stdout.index(' check') < result.stdout.index('--user enable')
    assert result.stdout.index(' enable ') < result.stdout.index('restart jukebox-daemon.service')


def test_checksum_failure_never_builds_or_installs():
    result = shell('''
source installation/routines/setup_airplay.sh
wget() { touch source.tar.gz; }
sha256sum() { cat >/dev/null; return 1; }
print_lc() { echo "$1"; }
autoreconf() { echo UNEXPECTED; }
sudo() { echo UNEXPECTED; }
_airplay_build
''')
    assert result.returncode == 1
    assert 'checksum mismatch' in result.stdout
    assert 'UNEXPECTED' not in result.stdout


def test_settings_change_preserves_other_keys_comments_and_permissions(tmp_path):
    settings = tmp_path / 'jukebox.yaml'
    settings.write_text('# Keep me\nbluetooth_media:\n  enable: true\nairplay_media:\n  enable: false\n  custom: 7\n')
    settings.chmod(0o640)
    helper.enable(settings)
    from ruamel.yaml import YAML
    data = YAML().load(settings)
    assert data == {'bluetooth_media': {'enable': True}, 'airplay_media': {'enable': True, 'custom': 7}}
    assert '# Keep me' in settings.read_text()
    assert settings.stat().st_mode & 0o777 == 0o640
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize('text', ['- invalid\n', 'airplay_media: false\n'])
def test_invalid_settings_are_not_overwritten(tmp_path, text):
    settings = tmp_path / 'jukebox.yaml'
    settings.write_text(text)
    with pytest.raises(ValueError):
        helper.enable(settings)
    assert settings.read_text() == text


def test_bus_policy_is_scoped_to_jukebox_user_and_escaped():
    root = ET.fromstring(helper.policy('a"b&c'))
    policy = root.find('policy')
    assert policy.attrib == {'user': 'a"b&c'}
    assert [entry.attrib for entry in policy] == [
        {'own': helper.SERVICE}, {'send_destination': helper.SERVICE},
    ]


@pytest.mark.parametrize('protocol,available_method,active,valid', [
    ('AirPlay', True, False, True), ('AirPlay 2', True, False, False),
    ('AirPlay', False, False, False), ('AirPlay', True, 'false', False),
])
def test_receiver_probe_checks_classic_interface(monkeypatch, protocol, available_method, active, valid):
    import dbus_next.aio
    bus = MagicMock()
    methods = ['DropSession', 'RemoteCommand'] if available_method else ['DropSession']
    bus.introspect = AsyncMock(return_value=SimpleNamespace(interfaces=[
        SimpleNamespace(name=helper.SERVICE, methods=[SimpleNamespace(name=name) for name in methods])]))
    properties = MagicMock()
    properties.call_get_all = AsyncMock(side_effect=[
        {'Protocol': SimpleNamespace(value=protocol), 'Active': SimpleNamespace(value=active)},
        {'Available': SimpleNamespace(value=False)},
    ])
    bus.get_proxy_object.return_value.get_interface.return_value = properties
    bus.wait_for_disconnect = AsyncMock()
    factory = MagicMock()
    factory.return_value.connect = AsyncMock(return_value=bus)
    monkeypatch.setattr(dbus_next.aio, 'MessageBus', factory)
    if valid:
        asyncio.run(helper.check_once())
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(helper.check_once())
    bus.disconnect.assert_called_once()


def test_preflight_preserves_an_existing_receiver(tmp_path):
    (tmp_path / 'bin').mkdir()
    python = tmp_path / 'bin/python'
    python.write_text('#!/bin/bash\nexit 0\n')
    python.chmod(0o755)
    (tmp_path / 'jukebox.yaml').write_text('airplay_media:\n  enable: false\n')
    result = shell('''
source installation/routines/setup_airplay.sh
uname() { echo Linux; }
id() { if [[ "$1" == -u ]]; then echo 1000; else echo jukebox; fi; }
shairport-sync() { :; }
print_lc() { echo "$1"; }
_airplay_preflight
''', CURRENT_USER='jukebox', VIRTUAL_ENV=tmp_path, SETTINGS_PATH=tmp_path)
    assert result.returncode == 1
    assert 'already exists' in result.stdout
    assert 'enable: false' in (tmp_path / 'jukebox.yaml').read_text()


def test_build_verifies_archive_and_installs_only_private_binary(tmp_path):
    import hashlib
    import tarfile
    source = tmp_path / 'source/shairport-sync-5.5.2'
    source.mkdir(parents=True)
    configure = source / 'configure'
    configure.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CONFIGURE_LOG"\n')
    configure.chmod(0o755)
    archive = tmp_path / 'fixture.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(source, arcname=source.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    prefix = tmp_path / 'install'
    log = tmp_path / 'configure.log'
    result = shell('''
source installation/routines/setup_airplay.sh
AIRPLAY_SHA256="$DIGEST"
AIRPLAY_PREFIX="$PREFIX"
wget() { cp "$ARCHIVE" source.tar.gz; }
sha256sum() { shasum -a 256 "$@"; }
autoreconf() { :; }
make() { [[ "$*" == -j1 ]] && printf '#!/bin/sh\n' > shairport-sync; }
sudo() { "$@"; }
_airplay_build
''', DIGEST=digest, PREFIX=prefix, ARCHIVE=archive, CONFIGURE_LOG=log)
    assert result.returncode == 0, result.stderr
    assert (prefix / 'bin/shairport-sync').is_file()
    flags = log.read_text().splitlines()
    assert '--with-pulseaudio' in flags
    assert '--with-dbus-interface' in flags
    assert '--with-avahi' in flags
    assert '--with-ffmpeg' in flags
    assert '--with-airplay-2' not in flags
    assert '--with-systemd-startup' not in flags
