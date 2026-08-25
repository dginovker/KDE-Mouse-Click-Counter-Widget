#!/usr/bin/env bash
set -euo pipefail
umask 077

cd -- "$(dirname -- "$0")"

DAEMON_DIR="$HOME/.local/lib/kdeclick"
DATA_DIR="$HOME/.local/share/kdeclick"
UNIT_DIR="$HOME/.config/systemd/user"
PLASMOID_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/plasma/plasmoids/local.clickanalytics"
DB_PATH="$DATA_DIR/stats.db"
STATE_PATH="$DATA_DIR/state.json"
STATE_TEMP_PATH="$STATE_PATH.tmp"
BUS_NAME="io.github.dginovker.KDEClickAnalytics"
OBJECT_PATH="/io/github/dginovker/KDEClickAnalytics"
INTERFACE="io.github.dginovker.KDEClickAnalytics1"

die() { echo "ERROR: $*" >&2; exit 1; }

for command in cmp install kpackagetool6 mktemp plasmashell python3 qmllint systemctl systemd-analyze; do
    command -v "$command" >/dev/null || die "required command not found: $command"
done
if ! id -nG | tr ' ' '\n' | grep -qx input; then
    die "$(id -un) is not in the input group; add it, then log out and back in"
fi

# Reject a broken replacement before stopping the currently counting daemon.
python3 - "$(plasmashell --version)" <<'PY'
import ctypes, json, re, sys
from pathlib import Path

for path in ("daemon/kdeclickd.py", "daemon/libinput_ffi.py"):
    compile(Path(path).read_bytes(), path, "exec")
metadata = json.loads(Path("plasmoid/metadata.json").read_text())
main_qml = Path("plasmoid/contents/ui/main.qml").read_text()
version = re.search(r"\b(\d+)\.(\d+)(?:\.\d+)?\b", sys.argv[1])
if not version or tuple(map(int, version.groups()[:2])) < (6, 4):
    raise SystemExit(f"ERROR: Plasma 6.4 or newer is required; found {sys.argv[1]!r}")
if metadata.get("X-Plasma-API-Minimum-Version") != "6.4":
    raise SystemExit("ERROR: the widget must declare Plasma 6.4")
if "import org.kde.plasma.workspace.dbus as DBus" not in main_qml:
    raise SystemExit("ERROR: the widget does not import Plasma's D-Bus module")
try:
    ctypes.CDLL("libinput.so.10")
    import dbus, dbus.mainloop.glib, dbus.service, gi
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib
    dbus.SessionBus()
except Exception as error:
    raise SystemExit(f"ERROR: libinput, dbus-python, PyGObject GLib, and session D-Bus are required: {error}")
PY
qmllint plasmoid/contents/ui/main.qml
# A known executable lets this syntax check work on a first installation too.
UNIT_CHECK="$(mktemp --suffix=.service)"
sed 's|^ExecStart=.*|ExecStart=/bin/true|' systemd/kdeclickd.service > "$UNIT_CHECK"
systemd-analyze --user verify "$UNIT_CHECK" || { rm -f -- "$UNIT_CHECK"; exit 1; }
rm -f -- "$UNIT_CHECK"

mkdir -p "$DAEMON_DIR" "$DATA_DIR" "$UNIT_DIR"
install -m 0755 daemon/kdeclickd.py "$DAEMON_DIR/kdeclickd.py.installing"
install -m 0644 daemon/libinput_ffi.py "$DAEMON_DIR/libinput_ffi.py.installing"
install -m 0644 systemd/kdeclickd.service "$UNIT_DIR/kdeclickd.service.installing"

OLD_PID=0
if systemctl --user cat kdeclickd.service >/dev/null 2>&1; then
    OLD_PID="$(systemctl --user show kdeclickd.service -p MainPID --value)"
    [[ "$OLD_PID" =~ ^[0-9]+$ ]] || die "invalid installed daemon PID: $OLD_PID"
fi
if (( OLD_PID > 0 )); then
    echo "Stopping kdeclickd with SIGINT so pending statistics are flushed"
    systemctl --user kill --kill-who=main --signal=SIGINT kdeclickd.service
    for ((attempt = 0; attempt < 150; attempt++)); do
        current_pid="$(systemctl --user show kdeclickd.service -p MainPID --value)"
        [[ "$current_pid" == 0 ]] && break
        sleep 0.1
    done
    [[ "$current_pid" == 0 ]] || die "kdeclickd did not stop within 15 seconds"
    systemctl --user stop kdeclickd.service
    [[ "$(systemctl --user show kdeclickd.service -p ActiveState --value)" == inactive ]] \
        || die "kdeclickd did not reach the inactive state"
fi

[[ ! -e "$STATE_TEMP_PATH" ]] \
    || die "$STATE_TEMP_PATH exists after shutdown; preserving it for manual recovery"
[[ -e "$DB_PATH" || ! -e "$STATE_PATH" ]] \
    || die "$STATE_PATH exists without stats.db; refusing to delete the only totals snapshot"

BACKUP_PATH="$(python3 - "$DB_PATH" "$STATE_PATH" "$DATA_DIR/backups" <<'PY'
import datetime, json, math, sqlite3, sys
from pathlib import Path

database, state_path, backup_dir = map(Path, sys.argv[1:])
if not database.exists():
    raise SystemExit
source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
integrity = [row[0] for row in source.execute("PRAGMA integrity_check")]
if integrity != ["ok"]:
    raise SystemExit("ERROR: stats.db integrity check failed: " + "; ".join(integrity))
persisted = dict(source.execute("SELECT metric, SUM(value) FROM counts GROUP BY metric"))
if state_path.exists():
    live = json.loads(state_path.read_text())["totals"]
    lost = {key: (value, persisted.get(key, 0)) for key, value in live.items()
            if persisted.get(key, 0) < value and not math.isclose(
                persisted.get(key, 0), value, rel_tol=1e-12, abs_tol=1e-6)}
    if lost:
        raise SystemExit(f"ERROR: stats.db did not receive the old daemon's final totals: {lost}")
backup_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f%z")
destination = backup_dir / f"stats-{stamp}.db"
with sqlite3.connect(destination) as backup:
    source.backup(backup)
source.close()
print(destination)
PY
)" || exit $?
if [[ -n "$BACKUP_PATH" ]]; then
    echo "Verified and backed up stats.db to $BACKUP_PATH"
fi

mv -f -- "$DAEMON_DIR/kdeclickd.py.installing" "$DAEMON_DIR/kdeclickd.py"
mv -f -- "$DAEMON_DIR/libinput_ffi.py.installing" "$DAEMON_DIR/libinput_ffi.py"
mv -f -- "$UNIT_DIR/kdeclickd.service.installing" "$UNIT_DIR/kdeclickd.service"

systemctl --user daemon-reload
systemctl --user enable kdeclickd.service
if ! systemctl --user restart kdeclickd.service; then
    systemctl --user status kdeclickd.service --no-pager --lines=30 >&2 || :
    die "the replacement daemon failed to start; stats.db was not removed"
fi
NEW_PID="$(systemctl --user show kdeclickd.service -p MainPID --value)"
[[ "$NEW_PID" =~ ^[1-9][0-9]*$ ]] || die "the replacement daemon has no PID"
[[ "$OLD_PID" == 0 || "$NEW_PID" != "$OLD_PID" ]] || die "the daemon PID did not change"

if ! python3 - "$BUS_NAME" "$OBJECT_PATH" "$INTERFACE" "$BACKUP_PATH" <<'PY'
import dbus, json, math, sqlite3, sys

bus_name, object_path, interface, checkpoint = sys.argv[1:]
try:
    proxy = dbus.SessionBus().get_object(bus_name, object_path, introspect=False)
    state = json.loads(str(dbus.Interface(proxy, interface).GetState(timeout=5)))
except Exception as error:
    raise SystemExit(f"ERROR: D-Bus GetState failed: {error}")
if state.get("schema") != 1 or not isinstance(state.get("totals"), dict):
    raise SystemExit("ERROR: D-Bus GetState did not return schema 1 totals")
persisted = {}
if checkpoint:
    with sqlite3.connect(f"file:{checkpoint}?mode=ro", uri=True) as connection:
        persisted = dict(connection.execute("SELECT metric, SUM(value) FROM counts GROUP BY metric"))
lost = {key: (value, state["totals"].get(key)) for key, value in persisted.items()
        if not isinstance(state["totals"].get(key), (int, float)) or
        (state["totals"][key] < value and not math.isclose(
            state["totals"][key], value, rel_tol=1e-12, abs_tol=1e-6))}
if lost:
    raise SystemExit(f"ERROR: D-Bus totals decreased from stats.db: {lost}")
PY
then
    systemctl --user status kdeclickd.service --no-pager --lines=30 >&2 || :
    die "D-Bus verification failed; the widget and old state file were not changed"
fi

echo "Installing widget"
if kpackagetool6 --type Plasma/Applet --show local.clickanalytics >/dev/null 2>&1; then
    if ! cmp -s plasmoid/metadata.json "$PLASMOID_DIR/metadata.json" \
            || ! cmp -s plasmoid/contents/ui/main.qml "$PLASMOID_DIR/contents/ui/main.qml"; then
        kpackagetool6 --type Plasma/Applet --upgrade plasmoid
    fi
else
    kpackagetool6 --type Plasma/Applet --install plasmoid
fi
kpackagetool6 --type Plasma/Applet --show local.clickanalytics >/dev/null \
    || die "the widget package could not be verified"
cmp -s plasmoid/metadata.json "$PLASMOID_DIR/metadata.json" \
    || die "the installed widget metadata does not match version 0.2.0"
cmp -s plasmoid/contents/ui/main.qml "$PLASMOID_DIR/contents/ui/main.qml" \
    || die "the installed widget is missing the D-Bus state implementation"

# KPackage replaces the files but existing widget instances keep their loaded QML.
if systemctl --user is-active --quiet plasma-plasmashell.service; then
    echo "Restarting Plasma Shell to load the updated widget"
    if ! systemctl --user restart plasma-plasmashell.service; then
        systemctl --user status plasma-plasmashell.service --no-pager --lines=30 >&2 || :
        die "Plasma Shell could not reload the updated widget; the old state file was preserved"
    fi
fi

# At this point SQLite and D-Bus have replaced both jobs the old file performed.
rm -f -- "$STATE_PATH" "$STATE_TEMP_PATH"

echo "Done. kdeclickd PID $NEW_PID is publishing schema 1 over D-Bus."
echo "Database: $DB_PATH"
[[ -z "$BACKUP_PATH" ]] || echo "Backup:   $BACKUP_PATH"
