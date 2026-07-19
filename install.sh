#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

DAEMON_DIR="$HOME/.local/lib/kdeclick"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/kdeclick"
UNIT_DIR="$HOME/.config/systemd/user"

# Checked up front and hard-failed: without input group membership the daemon
# starts, sees no devices, and would otherwise just report zeros forever.
if ! id -nG | tr ' ' '\n' | grep -qx input; then
    echo "ERROR: $USER is not in the 'input' group, so /dev/input/event* cannot be read." >&2
    echo "Fix with:  sudo usermod -aG input $USER   (then log out and back in)" >&2
    exit 1
fi

if ! python3 -c "import ctypes; ctypes.CDLL('libinput.so.10')" 2>/dev/null; then
    echo "ERROR: libinput.so.10 not found. Install the 'libinput' package." >&2
    exit 1
fi

echo "Installing daemon to $DAEMON_DIR"
mkdir -p "$DAEMON_DIR" "$DATA_DIR" "$UNIT_DIR"
install -m 0755 daemon/kdeclickd.py "$DAEMON_DIR/kdeclickd.py"
install -m 0644 daemon/libinput_ffi.py "$DAEMON_DIR/libinput_ffi.py"
install -m 0644 systemd/kdeclickd.service "$UNIT_DIR/kdeclickd.service"

echo "Installing widget"
if kpackagetool6 --type Plasma/Applet --show local.clickanalytics >/dev/null 2>&1; then
    kpackagetool6 --type Plasma/Applet --upgrade plasmoid
else
    kpackagetool6 --type Plasma/Applet --install plasmoid
fi

echo "Starting kdeclickd"
systemctl --user daemon-reload
systemctl --user enable --now kdeclickd.service

sleep 2
if ! systemctl --user is-active --quiet kdeclickd.service; then
    echo "ERROR: kdeclickd failed to start. Logs:" >&2
    systemctl --user status kdeclickd.service --no-pager --lines=20 >&2
    exit 1
fi

echo
echo "Done. kdeclickd is running and counting."
echo "Add the 'Click Analytics' widget to your panel."
echo
echo "  Logs:     journalctl --user -u kdeclickd -f"
echo "  Database: $DATA_DIR/stats.db"
