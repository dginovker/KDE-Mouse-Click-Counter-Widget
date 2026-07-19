#!/usr/bin/env python3
"""Input analytics daemon: counts keystrokes, clicks, scrolls and pointer travel.

Two outputs, for two different jobs:

  state.json  rewritten within ~250ms of activity, holding live all-time totals
              and the 24h activity graph. The widget just reads this file, so it
              costs a 2ms `cat` instead of a 33ms Python run, and shows counts
              that are current rather than lagging the database flush.

  stats.db    per-hour rows flushed every 10s, so history survives restarts.
              One row per hour per metric is roughly 9 KB/year.

Totals are held in memory (seeded from the database at startup), so state.json
never waits on a flush.

Key identities are never recorded -- only how many keys were pressed. The
database cannot reconstruct anything typed.

Run with --duration N to count for N seconds and print a summary, for testing
without installing the service.
"""

import argparse
import json
import os
import selectors
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import libinput_ffi as li

DATA_DIR = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
) / "kdeclick"

FLUSH_SECONDS = 10.0
RESCAN_SECONDS = 5.0
STATE_MIN_INTERVAL = 0.25

# state.json is rewritten on this interval even when nothing changed, so its
# timestamp means "the daemon is alive" rather than "you typed recently".
# Without it, sitting idle is indistinguishable from a dead daemon and the
# widget would cry stale at someone who simply walked away.
HEARTBEAT_SECONDS = 30.0

# Counted as "actions" in the activity graph.
ACTION_METRICS = ("keystrokes", "click_left", "click_right", "click_middle")

SCHEMA = """
CREATE TABLE IF NOT EXISTS counts (
    bucket TEXT NOT NULL,          -- local time, 'YYYY-MM-DD HH'
    metric TEXT NOT NULL,
    value  REAL NOT NULL,
    PRIMARY KEY (bucket, metric)
) WITHOUT ROWID;
"""


def log(message):
    print(f"[kdeclickd] {message}", file=sys.stderr, flush=True)


def bucket_for(when):
    return time.strftime("%Y-%m-%d %H", time.localtime(when))


class Counters:
    """Live totals plus the per-hour deltas still awaiting a database flush."""

    def __init__(self, totals, hourly):
        self._lock = threading.Lock()
        self._pending = defaultdict(float)
        self._totals = defaultdict(float, totals)
        self._hourly = defaultdict(float, hourly)
        self._session = defaultdict(float)
        self._bucket = ""
        self._bucket_expires = 0.0
        self.dirty = True

    def _current_bucket(self, now):
        # Recomputed only when the hour rolls over, not per event.
        if now >= self._bucket_expires:
            local = time.localtime(now)
            self._bucket = time.strftime("%Y-%m-%d %H", local)
            self._bucket_expires = now - (local.tm_min * 60 + local.tm_sec) + 3600
        return self._bucket

    def bump(self, metric, amount=1.0):
        now = time.time()
        with self._lock:
            bucket = self._current_bucket(now)
            self._pending[(bucket, metric)] += amount
            self._totals[metric] += amount
            self._session[metric] += amount
            if metric in ACTION_METRICS:
                self._hourly[bucket] += amount
            self.dirty = True

    def drain_pending(self):
        with self._lock:
            drained = self._pending
            self._pending = defaultdict(float)
            return drained

    def restore_pending(self, counts):
        """Return undrained counts after a failed flush so nothing is lost."""
        with self._lock:
            for key, value in counts.items():
                self._pending[key] += value

    def snapshot(self):
        with self._lock:
            self.dirty = False
            return dict(self._totals), dict(self._hourly)

    def session_totals(self):
        with self._lock:
            return dict(self._session)


class Storage:
    """Owns its own connection; only ever touched by the flush thread."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def write(self, counts):
        self._connection.executemany(
            "INSERT INTO counts (bucket, metric, value) VALUES (?, ?, ?) "
            "ON CONFLICT(bucket, metric) DO UPDATE SET value = value + excluded.value",
            [(bucket, metric, value) for (bucket, metric), value in counts.items()],
        )
        self._connection.commit()

    @staticmethod
    def load(path):
        """Read all-time totals and the last 24h of activity into memory."""
        if not Path(path).exists():
            return {}, {}
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        with connection:
            totals = {m: v for m, v in connection.execute(
                "SELECT metric, SUM(value) FROM counts GROUP BY metric")}
            cutoff = bucket_for(time.time() - 24 * 3600)
            placeholders = ",".join("?" * len(ACTION_METRICS))
            hourly = {b: v for b, v in connection.execute(
                f"SELECT bucket, SUM(value) FROM counts WHERE bucket >= ? "
                f"AND metric IN ({placeholders}) GROUP BY bucket",
                (cutoff, *ACTION_METRICS))}
        connection.close()
        return totals, hourly


class Daemon:
    def __init__(self, data_dir):
        self.db_path = Path(data_dir) / "stats.db"
        self.state_path = Path(data_dir) / "state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        totals, hourly = Storage.load(self.db_path)
        log(f"loaded {len(totals)} metrics, {len(hourly)} recent hours from {self.db_path}")

        self.counters = Counters(totals, hourly)
        self.context = li.create_context()
        self.added = {}
        self.stop_event = threading.Event()

    def rescan_devices(self):
        """Add device nodes that appeared since the last scan.

        Tracked by path so a device is never added twice; a duplicate handle
        would deliver every event twice and silently double all counts.
        """
        try:
            entries = sorted(os.listdir("/dev/input"))
        except OSError as exc:
            log(f"cannot list /dev/input: {exc}")
            return
        for entry in entries:
            if not entry.startswith("event"):
                continue
            path = f"/dev/input/{entry}"
            if path in self.added:
                continue
            device, description = li.add_device(self.context, path)
            if device:
                self.added[path] = device
                log(f"device added   {path}  {description}")

    def _forget_device(self, handle):
        for path, known in list(self.added.items()):
            if known == handle:
                del self.added[path]
                log(f"device removed {path}")
                return

    def write_state(self):
        totals, hourly = self.counters.snapshot()
        now = time.time()

        values, labels = [], []
        for offset in range(23, -1, -1):
            moment = now - offset * 3600
            values.append(hourly.get(bucket_for(moment), 0.0))
            labels.append(time.strftime("%H", time.localtime(moment)))

        peak = max(range(24), key=lambda i: values[i]) if any(values) else None
        payload = {
            "totals": totals,
            "activity": {"values": values, "labels": labels},
            "peak": {"hour": labels[peak], "value": values[peak]} if peak is not None else None,
            "updated": now,
        }

        # Written via a temporary file and renamed, so a reader never sees a
        # half-written file and reports garbage counts.
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload))
        temporary.replace(self.state_path)

    def flush_loop(self):
        try:
            storage = Storage(self.db_path)
        except sqlite3.Error as exc:
            log(f"FATAL: cannot open database {self.db_path}: {exc}")
            self.stop_event.set()
            return

        failures = 0
        while not self.stop_event.wait(FLUSH_SECONDS):
            failures = self._flush_once(storage, failures)
        self._flush_once(storage, failures)

    def _flush_once(self, storage, failures):
        counts = self.counters.drain_pending()
        if not counts:
            return 0
        try:
            storage.write(counts)
            return 0
        except sqlite3.Error as exc:
            # Put the counts back so a transient failure costs nothing, and be
            # loud about it: a daemon that silently stops persisting looks
            # identical to a user who stopped typing.
            self.counters.restore_pending(counts)
            log(f"flush failed ({failures + 1}x): {exc}")
            return failures + 1

    def handle_event(self, event):
        etype = li.event_get_type(event)

        if etype == li.EVENT_KEYBOARD_KEY:
            # Press only. libinput reports press and release, so counting the
            # event type alone would double every keystroke.
            if li.keyboard_get_key_state(li.event_get_keyboard(event)) == 1:
                self.counters.bump("keystrokes")

        elif etype == li.EVENT_POINTER_BUTTON:
            pointer = li.event_get_pointer(event)
            if li.pointer_get_button_state(pointer) == 1:
                button = li.pointer_get_button(pointer)
                self.counters.bump(f"click_{li.BUTTON_NAMES.get(button, hex(button))}")

        elif etype == li.EVENT_SCROLL_WHEEL:
            pointer = li.event_get_pointer(event)
            for axis in (li.AXIS_VERTICAL, li.AXIS_HORIZONTAL):
                if li.pointer_has_axis(pointer, axis):
                    # v120 reports 120 units per physical detent, so this is an
                    # exact notch count rather than a scaled approximation.
                    notches = li.pointer_get_scroll_value_v120(pointer, axis) / 120.0
                    if notches:
                        self.counters.bump("scroll_wheel", abs(notches))

        elif etype in (li.EVENT_SCROLL_FINGER, li.EVENT_SCROLL_CONTINUOUS):
            pointer = li.event_get_pointer(event)
            for axis in (li.AXIS_VERTICAL, li.AXIS_HORIZONTAL):
                if li.pointer_has_axis(pointer, axis):
                    if li.pointer_get_scroll_value(pointer, axis):
                        self.counters.bump("scroll_touchpad")
                        break

        elif etype == li.EVENT_POINTER_MOTION:
            pointer = li.event_get_pointer(event)
            dx = li.pointer_get_dx_unaccel(pointer)
            dy = li.pointer_get_dy_unaccel(pointer)
            self.counters.bump("motion_units", (dx * dx + dy * dy) ** 0.5)

        elif etype == li.EVENT_DEVICE_REMOVED:
            self._forget_device(li.event_get_device(event))

    def run(self, duration=None):
        self.rescan_devices()
        if not self.added:
            raise SystemExit(
                "FATAL: libinput accepted no devices. Check that this user is in "
                "the 'input' group (id -nG) and that /dev/input/event* is readable."
            )
        log(f"watching {len(self.added)} devices, state at {self.state_path}")

        flusher = threading.Thread(target=self.flush_loop, daemon=True)
        flusher.start()
        self.write_state()

        selector = selectors.DefaultSelector()
        selector.register(li.get_fd(self.context), selectors.EVENT_READ)

        deadline = time.monotonic() + duration if duration else None
        next_rescan = time.monotonic() + RESCAN_SECONDS
        next_state = 0.0
        next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS

        try:
            while not self.stop_event.is_set():
                if deadline and time.monotonic() >= deadline:
                    break
                # Short timeout: libinput drives tap detection from timers on
                # this fd and misdetects taps if they are serviced late.
                selector.select(timeout=0.02)
                li.dispatch(self.context)
                while True:
                    event = li.get_event(self.context)
                    if not event:
                        break
                    self.handle_event(event)
                    li.event_destroy(event)

                now = time.monotonic()
                # Rewritten when something changed (at most every 250ms, so
                # holding a key does not rewrite per event) or on the heartbeat.
                if (self.counters.dirty and now >= next_state) or now >= next_heartbeat:
                    self.write_state()
                    next_state = now + STATE_MIN_INTERVAL
                    next_heartbeat = now + HEARTBEAT_SECONDS
                if now >= next_rescan:
                    self.rescan_devices()
                    next_rescan = now + RESCAN_SECONDS
        except KeyboardInterrupt:
            pass
        finally:
            # No return here: returning from finally would swallow exceptions.
            self.stop_event.set()
            flusher.join(timeout=5.0)
            self.write_state()
        return self.counters.session_totals()


def main():
    parser = argparse.ArgumentParser(description="Input analytics daemon")
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="Where to keep stats.db and state.json")
    parser.add_argument(
        "--duration", type=float, default=None,
        help="Count for N seconds, print a summary, and exit (for testing)",
    )
    args = parser.parse_args()

    session = Daemon(args.data_dir).run(duration=args.duration)

    if args.duration:
        print("\n=== COUNTED THIS RUN ===")
        for metric in sorted(session):
            print(f"  {metric:20} {session[metric]:.0f}")
        if not session:
            print("  (no input recorded)")


if __name__ == "__main__":
    main()
