#!/usr/bin/env bash

# Classic mode matches the media adapter's verified remote controls. Install only
# our binary/configuration, not upstream's system service or its global policy.
AIRPLAY_VERSION=5.5.2
AIRPLAY_SHA256=abcdb59674b6eedb4f3f6228f3c702e65d2cdc037231b0c48617fd90891b49e9
AIRPLAY_PREFIX=/opt/phoniebox-airplay
AIRPLAY_CONFIG=/etc/phoniebox-airplay.conf
AIRPLAY_POLICY=/etc/dbus-1/system.d/phoniebox-airplay.conf
AIRPLAY_SERVICE=phoniebox-airplay.service

_airplay_preflight() {
    [[ $(uname -s) == Linux && $(id -u) != 0 ]] || {
        print_lc "AirPlay setup requires Linux; run as the Jukebox user, not root."
        return 1
    }
    [[ $(id -un) == "$CURRENT_USER" ]] || return 1
    [[ -x "${VIRTUAL_ENV}/bin/python" && -f "${SETTINGS_PATH}/jukebox.yaml" ]] || {
        print_lc "Install Jukebox Core before setting up AirPlay."
        return 1
    }
    if command -v shairport-sync >/dev/null 2>&1 ||
       [[ -e "$AIRPLAY_PREFIX" || -e "$AIRPLAY_CONFIG" || -e "$AIRPLAY_POLICY" ]] ||
       systemctl cat shairport-sync.service >/dev/null 2>&1 ||
       systemctl --user cat shairport-sync.service >/dev/null 2>&1 ||
       systemctl --user cat "$AIRPLAY_SERVICE" >/dev/null 2>&1; then
        print_lc "An AirPlay installation already exists. Nothing was overwritten.
Use documentation/builders/airplay.md to integrate it manually."
        return 1
    fi
    systemctl --user show-environment >/dev/null || return 1
    "${VIRTUAL_ENV}/bin/python" -c 'import dbus_next; import ruamel.yaml' || return 1
}

_airplay_install_dependencies() {
    sudo apt-get update && sudo apt-get install -y --no-install-recommends \
        build-essential autoconf automake libtool pkg-config ca-certificates wget \
        libpopt-dev libconfig-dev libavahi-client-dev libssl-dev libsoxr-dev \
        libavutil-dev libavcodec-dev libavformat-dev libswresample-dev \
        libpulse-dev libglib2.0-dev avahi-daemon dbus || return 1
}

_airplay_build() (
    local build_dir
    build_dir=$(mktemp -d) || return 1
    trap 'rm -rf "$build_dir"' EXIT
    cd "$build_dir" || return 1
    wget -q "https://codeload.github.com/mikebrady/shairport-sync/tar.gz/refs/tags/${AIRPLAY_VERSION}" \
        -O source.tar.gz || return 1
    printf '%s  source.tar.gz\n' "$AIRPLAY_SHA256" | sha256sum --check --status || {
        print_lc "Shairport Sync source checksum mismatch. Build aborted."
        return 1
    }
    tar -xzf source.tar.gz || return 1
    cd "shairport-sync-${AIRPLAY_VERSION}" || return 1
    autoreconf -fi || return 1
    ./configure --prefix="$AIRPLAY_PREFIX" --with-pulseaudio --with-avahi \
        --with-ssl=openssl --with-soxr --with-ffmpeg --with-dbus-interface || return 1
    # One compiler process keeps memory use bounded on older Raspberry Pis.
    make -j1 || return 1
    sudo install -d -m 755 "${AIRPLAY_PREFIX}/bin" || return 1
    sudo install -m 755 shairport-sync "${AIRPLAY_PREFIX}/bin/shairport-sync" || return 1
)

_airplay_configure() {
    local policy_file
    policy_file=$(mktemp) || return 1
    "${VIRTUAL_ENV}/bin/python" "${INSTALLATION_PATH}/installation/helpers/airplay.py" policy \
        "$CURRENT_USER" > "$policy_file" || { rm -f "$policy_file"; return 1; }
    sudo install -m 644 "$policy_file" "$AIRPLAY_POLICY"
    local policy_result=$?
    rm -f "$policy_file"
    [[ $policy_result == 0 ]] || return 1
    sudo install -m 644 "${INSTALLATION_PATH}/resources/default-settings/shairport-sync.conf" \
        "$AIRPLAY_CONFIG" || return 1
    mkdir -p "${HOME_PATH}/.config/systemd/user" || return 1
    command install -m 644 "${INSTALLATION_PATH}/resources/default-services/phoniebox-airplay.service" \
        "${HOME_PATH}/.config/systemd/user/${AIRPLAY_SERVICE}" || return 1
    # Reload policy without restarting the system bus (or disrupting Bluetooth).
    sudo systemctl reload dbus || return 1
    sudo systemctl enable --now avahi-daemon || return 1
    sudo loginctl enable-linger "$CURRENT_USER" || return 1
    systemctl --user daemon-reload || return 1
}

_airplay_start_and_verify() {
    systemctl --user start "$AIRPLAY_SERVICE" || return 1
    "${VIRTUAL_ENV}/bin/python" "${INSTALLATION_PATH}/installation/helpers/airplay.py" check || {
        systemctl --user stop "$AIRPLAY_SERVICE"
        print_lc "AirPlay interface verification failed; adapter remains unchanged.
Inspect: journalctl --user -u ${AIRPLAY_SERVICE}"
        return 1
    }
    systemctl --user enable "$AIRPLAY_SERVICE" || return 1
    "${VIRTUAL_ENV}/bin/python" "${INSTALLATION_PATH}/installation/helpers/airplay.py" enable \
        "${SETTINGS_PATH}/jukebox.yaml" || return 1
    systemctl --user restart jukebox-daemon.service || return 1
}

_run_setup_airplay() {
    _airplay_preflight || exit_on_error "AirPlay preflight failed."
    _airplay_install_dependencies || exit_on_error "AirPlay dependencies failed."
    _airplay_build || exit_on_error "Shairport Sync build failed."
    _airplay_configure || exit_on_error "AirPlay configuration failed."
    _airplay_start_and_verify || {
        systemctl --user disable --now "$AIRPLAY_SERVICE"
        exit_on_error "AirPlay startup failed. Receiver disabled; inspect the installation log."
    }
    FIN_MESSAGE="${FIN_MESSAGE:+$FIN_MESSAGE\n}Classic AirPlay is enabled: choose Phoniebox on your sender."
}

setup_airplay() {
    if [[ "${ENABLE_AIRPLAY:-false}" == true ]]; then
        run_with_log_frame _run_setup_airplay "Install Classic AirPlay receiver"
    fi
}
