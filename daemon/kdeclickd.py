#!/usr/bin/env python3
"""Input analytics daemon: counts keystrokes, clicks, scrolls and pointer travel.

Counts are aggregated per local hour and flushed to SQLite. One row per hour per
metric keeps the database around 9 KB/year while still supporting today / week /
all-time totals and the 24h activity graph from a single GROUP BY.

Key identities are never recorded -- only how many keys were pressed. The
database is therefore useless for reconstructing anything typed.

Run with --duration N to count for N seconds and print a summary instead of
running forever; used for testing without installing the service.
"""

import argparse
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

DEFAULT_DB = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
) / "kdeclick/stats.db"

FLUSH_SECONDS = 10.0
RESCAN_SECONDS = 5.0

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


class Counters:
    """Thread-safe (bucket, metric) tallies drained by the flush thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts = defaultdict(float)
        self._session = defaultdict(float)
        self._bucket = ""
        self._bucket_expires = 0.0

    def _current_bucket(self, now):
        # Recomputed only when the hour rolls over rather than per event.
        if now >= self._bucket_expires:
            local = time.localtime(now)
            self._bucket = time.strftime("%Y-%m-%d %H", local)
            self._bucket_expires = now - (local.tm_min * 60 + local.tm_sec) + 3600
        return self._bucket

    def bump(self, metric, amount=1.0):
        with self._lock:
            self._counts[(self._current_bucket(time.time()), metric)] += amount
            self._session[metric] += amount

    def drain(self):
        with self._lock:
            drained = self._counts
            self._counts = defaultdict(float)
            return drained

    def merge_back(self, counts):
        """Return undrained counts after a failed flush so nothing is lost."""
        with self._lock:
            for key, value in counts.items():
                self._counts[key] += value

    def session_totals(self):
        """Everything counted since start, including already-flushed events."""
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


class Daemon:
    def __init__(self, db_path):
        self.counters = Counters()
        self.db_path = db_path
        self.context = li.create_context()
        self.added = {}
        self.stop_event = threading.Event()
        self.flush_failures = 0

    def rescan_devices(self):
        """Add device nodes that appeared since the last scan.

        Tracked by path so a device is never added twice -- a duplicate handle
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

    def flush_loop(self):
        try:
            storage = Storage(self.db_path)
        except sqlite3.Error as exc:
            log(f"FATAL: cannot open database {self.db_path}: {exc}")
            self.stop_event.set()
            return
        log(f"database {self.db_path}")

        while not self.stop_event.wait(FLUSH_SECONDS):
            self._flush_once(storage)
        self._flush_once(storage)

    def _flush_once(self, storage):
        counts = self.counters.drain()
        if not counts:
            return
        try:
            storage.write(counts)
            self.flush_failures = 0
        except sqlite3.Error as exc:
            # Put the counts back so a transient failure costs nothing, and be
            # loud about it -- a daemon that silently stops persisting looks
            # identical to a user who stopped typing.
            self.counters.merge_back(counts)
            self.flush_failures += 1
            log(f"flush failed ({self.flush_failures}x): {exc}")

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
        log(f"watching {len(self.added)} devices")

        flusher = threading.Thread(target=self.flush_loop, daemon=True)
        flusher.start()

        selector = selectors.DefaultSelector()
        selector.register(li.get_fd(self.context), selectors.EVENT_READ)

        deadline = time.monotonic() + duration if duration else None
        next_rescan = time.monotonic() + RESCAN_SECONDS

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
                if now >= next_rescan:
                    self.rescan_devices()
                    next_rescan = now + RESCAN_SECONDS
        except KeyboardInterrupt:
            pass
        finally:
            # No return here: returning from finally would swallow any exception
            # raised in the loop, hiding real failures.
            self.stop_event.set()
            flusher.join(timeout=5.0)
        return self.counters.session_totals()


def main():
    parser = argparse.ArgumentParser(description="Input analytics daemon")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    parser.add_argument(
        "--duration", type=float, default=None,
        help="Count for N seconds, print a summary, and exit (for testing)",
    )
    args = parser.parse_args()

    daemon = Daemon(args.db)
    pending = daemon.run(duration=args.duration)

    if args.duration:
        print("\n=== COUNTED THIS RUN ===")
        for metric in sorted(pending):
            print(f"  {metric:20} {pending[metric]:.0f}")
        if not pending:
            print("  (no input recorded)")


if __name__ == "__main__":
    main()
