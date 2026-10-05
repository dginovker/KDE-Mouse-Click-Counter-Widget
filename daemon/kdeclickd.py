#!/usr/bin/env python3
"""Input and network analytics daemon.

Live snapshots are pushed over the session D-Bus while per-hour rows are
flushed to stats.db every 10 seconds. One row per hour per metric is roughly
9 KB/year. Totals are held in memory, seeded from the database at startup, so
the live view never waits on a database flush.

Key identities are never recorded -- only how many keys were pressed. The
database cannot reconstruct anything typed.

Run with --duration N to count for N seconds and print a summary, for testing
without installing the service.
"""

import argparse
import json
import os
import selectors
import signal
import sqlite3
import sys
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import libinput_ffi as li

DATA_DIR = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
) / "kdeclick"

FLUSH_SECONDS = 10.0
RESCAN_SECONDS = 5.0
PUBLISH_MIN_INTERVAL = 0.25
NETWORK_SAMPLE_SECONDS = 1.0

DBUS_SERVICE = "io.github.dginovker.KDEClickAnalytics"
DBUS_PATH = "/io/github/dginovker/KDEClickAnalytics"
DBUS_INTERFACE = "io.github.dginovker.KDEClickAnalytics1"
STATE_SCHEMA = 3

# Counted as "actions" in the activity graph.
ACTION_METRICS = ("keystrokes", "click_left", "click_right", "click_middle")
NETWORK_METRICS = ("network_rx_bytes", "network_tx_bytes")
GRAPH_METRICS = ACTION_METRICS + NETWORK_METRICS
DISPLAY_METRICS = ACTION_METRICS + (
    "click_side",
    "click_extra",
    "scroll_wheel",
    "scroll_touchpad",
    "motion_units",
) + NETWORK_METRICS

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
            if metric in GRAPH_METRICS:
                self._hourly[(bucket, metric)] += amount
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

    def mark_dirty(self):
        with self._lock:
            self.dirty = True

    def needs_snapshot(self):
        with self._lock:
            return self.dirty

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
        try:
            self._connection.executemany(
                "INSERT INTO counts (bucket, metric, value) VALUES (?, ?, ?) "
                "ON CONFLICT(bucket, metric) DO UPDATE SET value = value + excluded.value",
                [(bucket, metric, value) for (bucket, metric), value in counts.items()],
            )
            self._connection.commit()
        except sqlite3.Error as write_error:
            try:
                self._connection.rollback()
            except sqlite3.Error as rollback_error:
                raise RuntimeError(
                    f"database rollback failed after {write_error}: {rollback_error}"
                ) from rollback_error
            raise

    def close(self):
        self._connection.close()

    @staticmethod
    def load(path):
        """Read all-time totals and recent graph rows into memory."""
        if not Path(path).exists():
            return {}, {}
        # Read-write on purpose: after a hard reset SQLite must roll back the hot
        # journal before the first read, and a read-only connection cannot.
        connection = sqlite3.connect(str(path))
        with connection:
            totals = {m: v for m, v in connection.execute(
                "SELECT metric, SUM(value) FROM counts GROUP BY metric")}
            cutoff = bucket_for(time.time() - 24 * 3600)
            placeholders = ",".join("?" * len(GRAPH_METRICS))
            hourly = {(b, m): v for b, m, v in connection.execute(
                f"SELECT bucket, metric, value FROM counts WHERE bucket >= ? "
                f"AND metric IN ({placeholders})",
                (cutoff, *GRAPH_METRICS))}
        connection.close()
        return totals, hourly


class NetworkSampler:
    """Samples one physical default-route interface from Linux kernel counters."""

    def __init__(
        self, counters, proc_net=Path("/proc/net"), sys_class_net=Path("/sys/class/net")
    ):
        self.counters = counters
        self.proc_net = Path(proc_net)
        self.sys_class_net = Path(sys_class_net)
        self._interface = None
        self._previous = None
        self._state = {
            "status": "initializing",
            "interface": "",
            "sampled_at": 0.0,
            "error": "",
        }

    def snapshot(self):
        return dict(self._state)

    def sample(self):
        sampled_at = time.time()
        try:
            interface = self._select_interface()
            if interface is None:
                self._set_state(
                    "unavailable", "", sampled_at, "no physical default-route interface"
                )
                return
            received, transmitted = self._read_counters(interface)
        except (OSError, RuntimeError, ValueError) as exc:
            self._set_state("error", "", sampled_at, str(exc))
            return

        if interface != self._interface:
            self._interface = interface
            self._previous = (received, transmitted)
            self._set_state("ok", interface, sampled_at, "")
            return

        previous_received, previous_transmitted = self._previous
        self._previous = (received, transmitted)
        if received < previous_received or transmitted < previous_transmitted:
            self._set_state(
                "error",
                interface,
                sampled_at,
                f"kernel counters decreased on {interface}; measurement rebaselined",
            )
            return

        received_delta = received - previous_received
        transmitted_delta = transmitted - previous_transmitted
        if received_delta:
            self.counters.bump("network_rx_bytes", received_delta)
        if transmitted_delta:
            self.counters.bump("network_tx_bytes", transmitted_delta)
        self._set_state("ok", interface, sampled_at, "")

    def _set_state(self, status, interface, sampled_at, error):
        before = tuple(self._state[key] for key in ("status", "interface", "error"))
        self._state = {
            "status": status,
            "interface": interface,
            "sampled_at": sampled_at,
            "error": error,
        }
        self.counters.mark_dirty()

        if (status, interface, error) == before:
            return
        log(f"network sampling {interface}" if status == "ok" else f"network {status}: {error}")

    def _select_interface(self):
        ipv4 = self._family_choice(self._ipv4_default_routes(), "IPv4")
        ipv6 = self._family_choice(self._ipv6_default_routes(), "IPv6")
        selected = {interface for interface in (ipv4, ipv6) if interface is not None}
        if not selected:
            return None
        if len(selected) != 1:
            raise RuntimeError(
                "IPv4 and IPv6 default routes use different physical interfaces: "
                + ", ".join(sorted(selected))
            )
        return selected.pop()

    def _family_choice(self, routes, family):
        physical = {}
        for interface, metric in routes:
            if not (self.sys_class_net / interface / "device").exists():
                continue
            physical[interface] = min(metric, physical.get(interface, metric))
        if not physical:
            return None

        best_metric = min(physical.values())
        candidates = sorted(
            interface for interface, metric in physical.items() if metric == best_metric
        )
        if len(candidates) != 1:
            raise RuntimeError(
                f"{family} physical default route is ambiguous at metric {best_metric}: "
                + ", ".join(candidates)
            )
        return candidates[0]

    def _ipv4_default_routes(self):
        lines = (self.proc_net / "route").read_text(encoding="ascii").splitlines()
        if not lines or not lines[0].startswith("Iface"):
            raise RuntimeError("invalid /proc/net/route header")
        routes = []
        try:
            for line in lines[1:]:
                fields = line.split()
                if fields and fields[1] == "00000000" and fields[7] == "00000000":
                    flags = int(fields[3], 16)
                    metric = int(fields[6], 10)
                    if flags & 0x1:
                        routes.append((fields[0], metric))
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"cannot parse /proc/net/route: {exc}") from exc
        return routes

    def _ipv6_default_routes(self):
        path = self.proc_net / "ipv6_route"
        try:
            lines = path.read_text(encoding="ascii").splitlines()
        except FileNotFoundError:
            return []

        routes = []
        try:
            for line in lines:
                fields = line.split()
                if fields and fields[0] == "0" * 32 and fields[1] == "00":
                    metric = int(fields[5], 16)
                    flags = int(fields[8], 16)
                    if flags & 0x1:
                        routes.append((fields[9], metric))
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"cannot parse /proc/net/ipv6_route: {exc}") from exc
        return routes

    def _read_counters(self, interface):
        for line in (self.proc_net / "dev").read_text(encoding="ascii").splitlines():
            name, separator, values_text = line.partition(":")
            if separator and name.strip() == interface:
                values = values_text.split()
                if len(values) != 16:
                    raise RuntimeError(f"invalid /proc/net/dev counters for {interface}")
                return int(values[0]), int(values[8])
        raise RuntimeError(f"{interface} is missing from /proc/net/dev")


class DBusPublisher:
    """Owns all dbus-python objects on one GLib event-loop thread."""

    def __init__(self, initial_payload, fatal_callback):
        self._initial_payload = initial_payload
        self._fatal_callback = fatal_callback
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread = self._glib = self._loop = self._object = None
        self._pending = None
        self._scheduled = self._stopping = False
        self._error = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="kdeclick-dbus", daemon=True)
        self._thread.start()
        if not self._ready.wait(10.0):
            raise RuntimeError("timed out while claiming the session D-Bus service")
        if self._error:
            raise self._error

    def publish(self, payload):
        with self._lock:
            if self._error:
                raise self._error
            if self._stopping or self._glib is None:
                raise RuntimeError("D-Bus publisher is not running")
            self._pending = payload
            if self._scheduled:
                return
            self._scheduled = True
            glib = self._glib
        glib.idle_add(self._emit)

    def stop(self):
        self._stopping = True
        if self._glib is not None and self._loop is not None:
            self._glib.idle_add(self._loop.quit)
        if self._thread:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                raise RuntimeError("D-Bus event-loop thread did not stop")

    def _run(self):
        bus = None
        try:
            import dbus
            import dbus.mainloop.glib
            import dbus.service
            from gi.repository import GLib

            class StateService(dbus.service.Object):
                def __init__(self, bus_name, payload):
                    super().__init__(bus_name, DBUS_PATH)
                    self._payload = payload

                @dbus.service.method(DBUS_INTERFACE, in_signature="", out_signature="s")
                def GetState(self):
                    return self._payload

                @dbus.service.signal(DBUS_INTERFACE, signature="s")
                def StateChanged(self, payload):
                    pass

                def publish(self, payload):
                    self._payload = payload
                    self.StateChanged(payload)

            bus = dbus.SessionBus(
                private=True, mainloop=dbus.mainloop.glib.DBusGMainLoop()
            )
            bus.set_exit_on_disconnect(False)
            bus.call_on_disconnection(self._disconnected)
            bus_name = dbus.service.BusName(
                DBUS_SERVICE,
                bus=bus,
                allow_replacement=False,
                replace_existing=False,
                do_not_queue=True,
            )
            service_object = StateService(bus_name, self._initial_payload)
            loop = GLib.MainLoop()

            with self._lock:
                self._glib = GLib
                self._loop = loop
                self._object = service_object
            self._ready.set()
            loop.run()
            if not self._stopping and not self._error:
                raise RuntimeError("D-Bus event loop exited unexpectedly")
        except BaseException as exc:
            self._fail(RuntimeError(f"D-Bus service failed: {exc}"))
        finally:
            self._ready.set()
            with self._lock:
                self._object = self._loop = self._glib = None
            if bus is not None:
                bus.close()

    def _emit(self):
        try:
            with self._lock:
                payload = self._pending
                self._pending = None
                service_object = self._object
            service_object.publish(payload)
        except BaseException as exc:
            self._fail(RuntimeError(f"D-Bus publish failed: {exc}"))
            return False

        with self._lock:
            if self._pending is None:
                self._scheduled = False
                return False
        return True

    def _disconnected(self, _connection):
        if not self._stopping:
            self._fail(RuntimeError("session D-Bus disconnected"))

    def _fail(self, error):
        with self._lock:
            if self._error:
                return
            self._error = error
            loop = self._loop
        self._fatal_callback(self._error)
        if loop is not None:
            loop.quit()


class Daemon:
    def __init__(self, data_dir):
        self.db_path = Path(data_dir) / "stats.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        totals, hourly = Storage.load(self.db_path)
        log(
            f"loaded {len(totals)} metrics, {len(hourly)} recent graph rows "
            f"from {self.db_path}"
        )

        self.counters = Counters(totals, hourly)
        self.network = NetworkSampler(self.counters)
        self.context = li.create_context()
        self.added = {}
        self.stop_event = threading.Event()
        self.flush_stop_event = threading.Event()
        self.flush_ready = threading.Event()
        self._fatal_lock = threading.Lock()
        self._fatal_error = None
        self._instance = str(uuid.uuid4())
        self._revision = 0

    def _record_fatal(self, error):
        with self._fatal_lock:
            if self._fatal_error is not None:
                return
            self._fatal_error = error
        log(f"FATAL: {error}")
        self.stop_event.set()

    def _raise_if_fatal(self):
        with self._fatal_lock:
            error = self._fatal_error
        if error is not None:
            raise error

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

    def build_state(self):
        totals, hourly = self.counters.snapshot()
        for metric in DISPLAY_METRICS:
            totals.setdefault(metric, 0.0)
        now = time.time()

        values, rx_bytes, tx_bytes, labels = [], [], [], []
        for offset in range(23, -1, -1):
            moment = now - offset * 3600
            bucket = bucket_for(moment)
            values.append(sum(hourly.get((bucket, metric), 0.0)
                              for metric in ACTION_METRICS))
            rx_bytes.append(hourly.get((bucket, "network_rx_bytes"), 0.0))
            tx_bytes.append(hourly.get((bucket, "network_tx_bytes"), 0.0))
            labels.append(time.strftime("%H", time.localtime(moment)))

        network = self.network.snapshot()
        network["history"] = {
            "labels": labels,
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
        }
        self._revision += 1
        payload = {
            "schema": STATE_SCHEMA,
            "instance": self._instance,
            "revision": self._revision,
            "updated": now,
            "totals": totals,
            "activity": {"values": values, "labels": labels},
            "network": network,
        }
        return json.dumps(payload, separators=(",", ":"), allow_nan=False)

    def flush_loop(self):
        storage = None
        try:
            storage = Storage(self.db_path)
            self.flush_ready.set()
            failures = 0
            while not self.flush_stop_event.wait(FLUSH_SECONDS):
                failures = self._flush_once(storage, failures)
            while True:
                failures = self._flush_once(storage, failures)
                if not failures:
                    break
                time.sleep(1.0)
        except BaseException as exc:
            self._record_fatal(RuntimeError(f"database flush thread failed: {exc}"))
        finally:
            self.flush_ready.set()
            try:
                if storage is not None:
                    storage.close()
            except sqlite3.Error as exc:
                self._record_fatal(RuntimeError(f"cannot close database {self.db_path}: {exc}"))

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
        except BaseException:
            self.counters.restore_pending(counts)
            raise

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
        production = duration is None
        log(f"watching {len(self.added)} devices, database at {self.db_path}")

        previous_handlers = self._install_signal_handlers()
        flusher = threading.Thread(target=self.flush_loop, name="kdeclick-flush")
        flusher_started = False
        publisher = None
        selector = None
        try:
            self.network.sample()
            flusher.start()
            flusher_started = True
            if not self.flush_ready.wait(10.0):
                raise RuntimeError("timed out while opening the statistics database")
            self._raise_if_fatal()

            if production:
                publisher = DBusPublisher(self.build_state(), self._record_fatal)
                publisher.start()
                log(f"publishing live state on session D-Bus as {DBUS_SERVICE}")

            selector = selectors.DefaultSelector()
            selector.register(li.get_fd(self.context), selectors.EVENT_READ)

            deadline = time.monotonic() + duration if duration is not None else None
            now = time.monotonic()
            next_rescan = now + RESCAN_SECONDS
            next_state = now + PUBLISH_MIN_INTERVAL
            next_network_sample = now + NETWORK_SAMPLE_SECONDS

            while not self.stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
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
                if now >= next_network_sample:
                    self.network.sample()
                    next_network_sample = now + NETWORK_SAMPLE_SECONDS
                if (publisher is not None and self.counters.needs_snapshot()
                        and now >= next_state):
                    publisher.publish(self.build_state())
                    next_state = now + PUBLISH_MIN_INTERVAL
                if now >= next_rescan:
                    self.rescan_devices()
                    next_rescan = now + RESCAN_SECONDS
        finally:
            try:
                self.network.sample()
            except BaseException as exc:
                self._record_fatal(RuntimeError(f"final network sample failed: {exc}"))

            self.flush_stop_event.set()
            if flusher_started:
                flusher.join()

            if publisher is not None:
                try:
                    publisher.stop()
                except BaseException as exc:
                    self._record_fatal(RuntimeError(f"D-Bus shutdown failed: {exc}"))

            if selector is not None:
                selector.close()
            self._restore_signal_handlers(previous_handlers)

        self._raise_if_fatal()
        return self.counters.session_totals()

    def _install_signal_handlers(self):
        if threading.current_thread() is not threading.main_thread():
            return {}
        previous = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle_signal)
        return previous

    def _restore_signal_handlers(self, previous):
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    def _handle_signal(self, signum, _frame):
        log(f"received {signal.Signals(signum).name}; flushing before shutdown")
        self.stop_event.set()


def main():
    parser = argparse.ArgumentParser(description="Input and network analytics daemon")
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="Where to keep stats.db")
    parser.add_argument(
        "--duration", type=float, default=None,
        help="Count for N seconds, print a summary, and exit (for testing)",
    )
    args = parser.parse_args()

    session = Daemon(args.data_dir).run(duration=args.duration)

    if args.duration is not None:
        print("\n=== COUNTED THIS RUN ===")
        for metric in sorted(session):
            print(f"  {metric:20} {session[metric]:.0f}")
        if not session:
            print("  (no input recorded)")


if __name__ == "__main__":
    main()
