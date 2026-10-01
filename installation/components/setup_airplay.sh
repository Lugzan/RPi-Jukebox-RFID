#!/usr/bin/env bash
# Add the optional receiver to an existing Phoniebox without rerunning the installer.

[[ $(uname -s) == Linux && $(id -u) != 0 ]] || {
    echo "Run this on the Phoniebox as its normal user, not with sudo."
    exit 1
}
INSTALLATION_PATH=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd) || exit 1
CURRENT_USER=$(id -un)
CURRENT_USER_GROUP=$(id -gn)
HOME_PATH=$(getent passwd "$CURRENT_USER" | cut -d: -f6)
[[ -n "$HOME_PATH" ]] || exit 1

print_lc() { printf '%s\n' "$1"; }
log() { print_lc "$1"; }
exit_on_error() { print_lc "$1"; exit 1; }
run_with_log_frame() { print_lc "$2"; "$1"; }

source "${INSTALLATION_PATH}/installation/includes/00_constants.sh" || exit 1
source "${INSTALLATION_PATH}/installation/routines/setup_airplay.sh" || exit 1
ENABLE_AIRPLAY=true
setup_airplay
print_lc "$FIN_MESSAGE"
