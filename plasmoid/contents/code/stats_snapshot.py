#!/usr/bin/env python3
"""Emit widget state as JSON on stdout.

Called by the plasmoid through Plasma5Support.DataSource, mirroring the helper
pattern used by the AI Usage Rings widget.

Every window and the hourly graph come from the same per-hour rows, so the
totals and the graph can never disagree.

If the daemon is not running, that is reported explicitly rather than as zeros:
a widget showing 0 because the daemon died looks exactly like a widget showing
0 because nobody typed, and the two need to be distinguishable.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(
    os.environ.get("KDECLICK_DB")
    or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    / "kdeclick/stats.db"
)

# A flush lands every 10s, so silence well past that means the daemon is gone.
STALE_AFTER_SECONDS = 120

METRICS = [
    "keystrokes", "click_left", "click_right", "click_middle",
    "click_side", "click_extra", "scroll_wheel", "scroll_touchpad", "motion_units",
]


def emit(payload):
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def totals_for(connection, since_bucket=None):
    if since_bucket:
        rows = connection.execute(
            "SELECT metric, SUM(value) FROM counts WHERE bucket >= ? GROUP BY metric",
            (since_bucket,),
        )
    else:
        rows = connection.execute("SELECT metric, SUM(value) FROM counts GROUP BY metric")
    totals = {metric: 0.0 for metric in METRICS}
    for metric, value in rows:
        totals[metric] = value or 0.0
    return totals


def main():
    if not DB_PATH.exists():
        emit({
            "available": False,
            "error": "No database yet. Is kdeclickd running? "
                     "Check: systemctl --user status kdeclickd",
        })
        return

    now = time.time()
    local = time.localtime(now)
    today = time.strftime("%Y-%m-%d", local)
    week_start = time.strftime("%Y-%m-%d", time.localtime(now - 6 * 86400))

    try:
        connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        emit({"available": False, "error": f"Cannot open database: {exc}"})
        return

    with connection:
        payload = {
            "available": True,
            "today": totals_for(connection, today),
            "week": totals_for(connection, week_start),
            "all": totals_for(connection),
        }

        # 24 hourly values ending at the current hour, for the activity graph.
        hours, labels = [], []
        for offset in range(23, -1, -1):
            moment = time.localtime(now - offset * 3600)
            bucket = time.strftime("%Y-%m-%d %H", moment)
            row = connection.execute(
                "SELECT SUM(value) FROM counts WHERE bucket = ? "
                "AND metric IN ('keystrokes','click_left','click_right','click_middle')",
                (bucket,),
            ).fetchone()
            hours.append(row[0] or 0.0)
            labels.append(time.strftime("%H", moment))
        payload["activity"] = {"values": hours, "labels": labels}

        peak = max(range(24), key=lambda i: hours[i]) if any(hours) else None
        payload["peak"] = {"hour": labels[peak], "value": hours[peak]} if peak is not None else None

        latest = connection.execute("SELECT MAX(bucket) FROM counts").fetchone()[0]

    # A bucket is only stamped to the hour, so compare against the hour's end.
    payload["stale"] = True
    if latest:
        try:
            bucket_start = time.mktime(time.strptime(latest, "%Y-%m-%d %H"))
            payload["stale"] = (now - (bucket_start + 3600)) > STALE_AFTER_SECONDS
        except ValueError:
            payload["error"] = f"Unparseable bucket in database: {latest!r}"
    payload["last_bucket"] = latest

    emit(payload)


if __name__ == "__main__":
    main()
