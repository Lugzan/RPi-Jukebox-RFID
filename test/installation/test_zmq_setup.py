"""Architecture selection and fatal build errors, without installing anything."""
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def shell(script, **environment):
    env = os.environ.copy()
    env.update({key: str(value) for key, value in environment.items()})
    return subprocess.run(['bash', '-c', script], cwd=ROOT, env=env, text=True, capture_output=True)


@pytest.mark.parametrize('kernel,packages,expected', [
    ('aarch64', 'armhf', 'armv7'),
    ('armv7l', 'armhf', 'armv7'),
    ('armv6l', 'armhf', 'armv6'),
    ('aarch64', 'arm64', 'arm64'),
    ('x86_64', 'amd64', 'x86_64'),
    ('x86_64', 'i386', 'i386'),
    ('armv7l', '', 'armv7'),
    ('aarch64', '', 'arm64'),
    ('x86_64', '', 'x86_64'),
])
def test_binary_architecture_matches_userspace(kernel, packages, expected):
    result = shell('''
source installation/includes/02_helpers.sh
uname() { echo "$KERNEL"; }
dpkg() { [[ -n "$PACKAGES" ]] || return 127; echo "$PACKAGES"; }
get_architecture
''', KERNEL=kernel, PACKAGES=packages)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize('failure', ['', 'extract', 'copy'])
def test_prebuilt_download_uses_32_bit_asset_and_stops_on_install_failure(tmp_path, failure):
    result = shell('''
source installation/includes/02_helpers.sh
source installation/routines/setup_jukebox_core.sh
JUKEBOX_ZMQ_TMP_DIR="$TEST_TMP"
uname() { echo aarch64; }
dpkg() { echo armhf; }
log() { echo "$1"; }
exit_on_error() { echo "$1" >&2; exit 9; }
wget() { echo "download $*"; }
tar() { echo extract; [[ "$FAILURE" != extract ]]; }
sudo() {
    echo "sudo $*"
    [[ "$FAILURE" != copy || "$1" != rsync ]]
}
_jukebox_core_download_prebuilt_libzmq_with_drafts
echo completed
''', TEST_TMP=tmp_path, FAILURE=failure)
    assert 'libzmq5-armv7-4.3.5.tar.gz' in result.stdout
    assert 'libzmq5-arm64' not in result.stdout
    assert 'kernel=aarch64 userspace=armhf asset=armv7' in result.stdout
    assert result.returncode == (9 if failure else 0), result.stderr
    assert ('completed' in result.stdout) == (not failure)
    if failure == 'extract':
        assert 'sudo rsync' not in result.stdout
    if failure:
        assert 'sudo ldconfig' not in result.stdout


@pytest.mark.parametrize('failure', ['', 'configure', 'build', 'install'])
def test_native_build_stops_on_failure_and_refreshes_loader_only_after_install(tmp_path, failure):
    source = tmp_path / 'zeromq-4.3.5'
    source.mkdir()
    configure = source / 'configure'
    configure.write_text('#!/bin/bash\necho configure\n[[ "$FAILURE" != configure ]]\n')
    configure.chmod(0o755)
    result = shell('''
source installation/routines/setup_jukebox_core.sh
JUKEBOX_ZMQ_TMP_DIR="$TEST_TMP"
CPU_COUNT=2
print_lc() { :; }
exit_on_error() { echo "$1" >&2; exit 9; }
wget() { :; }
tar() { :; }
make() { echo build; [[ "$FAILURE" != build ]]; }
sudo() {
    echo "sudo $*"
    [[ "$FAILURE" != install || "$1" != make ]]
}
_jukebox_core_build_libzmq_with_drafts
echo completed
''', TEST_TMP=tmp_path, FAILURE=failure)
    assert result.returncode == (9 if failure else 0), result.stderr
    assert ('completed' in result.stdout) == (not failure)
    if failure == 'configure':
        assert '\nbuild\n' not in result.stdout
    if failure in ('configure', 'build'):
        assert 'sudo make install' not in result.stdout
    assert ('sudo ldconfig' in result.stdout) == (not failure)


def test_pyzmq_failure_aborts_before_settings_or_service_changes(tmp_path):
    result = shell('''
source installation/routines/setup_jukebox_core.sh
JUKEBOX_ZMQ_TMP_DIR="$TEST_TMP"
BUILD_LIBZMQ_WITH_DRAFTS_ON_DEVICE=false
print_lc() { :; }
uname() { echo aarch64; }
dpkg() { echo armhf; }
exit_on_error() { echo "$1" >&2; exit 9; }
_jukebox_core_install_os_dependencies() { :; }
_jukebox_core_install_python_requirements() { :; }
_jukebox_core_download_prebuilt_libzmq_with_drafts() { :; }
pip() {
    [[ "$1" == list ]] && return 0
    echo "pip $*"
    return 1
}
_jukebox_core_configure_pulseaudio() { echo UNEXPECTED; }
_jukebox_core_install_settings() { echo UNEXPECTED; }
_jukebox_core_register_as_service() { echo UNEXPECTED; }
_jukebox_core_check() { echo UNEXPECTED; }
_run_setup_jukebox_core
''', TEST_TMP=tmp_path)
    assert result.returncode == 9
    assert 'Could not build pyzmq' in result.stderr
    assert 'kernel=aarch64, userspace=armhf' in result.stderr
    assert 'UNEXPECTED' not in result.stdout


def test_python_requirements_failure_is_not_silently_ignored(tmp_path):
    venv = tmp_path / 'venv'
    (venv / 'bin').mkdir(parents=True)
    (venv / 'bin' / 'activate').write_text('# test environment\n')
    result = shell('''
source installation/routines/setup_jukebox_core.sh
print_lc() { :; }
exit_on_error() { echo "$1" >&2; exit 9; }
python3() { :; }
_jukebox_core_build_and_install_lg() { :; }
pip() { [[ "$*" != *requirements.txt* ]]; }
_jukebox_core_install_python_requirements
echo UNEXPECTED
''', VIRTUAL_ENV=venv, INSTALLATION_PATH=tmp_path)
    assert result.returncode == 9
    assert 'Could not install Python requirements' in result.stderr
    assert 'UNEXPECTED' not in result.stdout
