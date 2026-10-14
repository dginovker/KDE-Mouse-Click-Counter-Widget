"""Local usage metadata and observed working-agent time; never stores message content."""

from datetime import datetime
import ctypes
import errno
import json
import os
from pathlib import Path
import sqlite3
import struct
import dbus
import threading
import time

PROVIDERS = ("pi", "claude", "codex")
SAMPLE_SECONDS = 5
TOKEN_SECONDS = 5
TOKEN_IDLE_SECONDS = 60
TOKEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_files (
    path TEXT PRIMARY KEY, inode INTEGER NOT NULL, position INTEGER NOT NULL,
    session TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS token_usage (
    provider TEXT NOT NULL, event TEXT NOT NULL,
    read_tokens INTEGER NOT NULL, write_tokens INTEGER NOT NULL,
    PRIMARY KEY(provider, event)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS token_hourly_usage (
    provider TEXT NOT NULL, event TEXT NOT NULL, bucket TEXT NOT NULL,
    read_tokens INTEGER NOT NULL, write_tokens INTEGER NOT NULL,
    PRIMARY KEY(provider, event)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS token_hourly_bucket ON token_hourly_usage(bucket);
"""


def token_number(usage, key):
    value = usage[key] if key in ("input", "output", "input_tokens", "output_tokens") else usage.get(key, 0)
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid {key}: {value!r}")
    return value


def token_event(provider, item, session):
    message = item.get("message") or {}
    if provider == "pi":
        if item.get("type") == "session":
            return None, item["id"]
        if item.get("type") != "message" or message.get("role") != "assistant" or not message.get("usage"):
            return None, session
        usage = message["usage"]
        read = sum(token_number(usage, key) for key in ("input", "cacheRead", "cacheWrite"))
        write = token_number(usage, "output")
        event = f"{item['id']}:{item['timestamp']}"
    elif provider == "claude":
        if item.get("type") != "assistant" or not message.get("usage"):
            return None, session
        usage = message["usage"]
        read = sum(token_number(usage, key) for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        write = token_number(usage, "output_tokens")
        event = message["id"]
    elif provider == "codex":
        payload = item.get("payload") or {}
        if item.get("type") == "session_meta":
            return None, json.dumps({"id": payload["id"], "previous": [0, 0], "total": [0, 0]})
        if item.get("type") != "event_msg" or payload.get("type") != "token_count":
            return None, session
        usage = (payload.get("info") or {}).get("total_token_usage")
        if not usage:
            return None, session
        if not session:
            raise ValueError("Codex token count has no session_meta id")
        # Codex already includes cached input and reasoning output in these totals.
        current = [token_number(usage, key) for key in ("input_tokens", "output_tokens")]
        state = json.loads(session)
        state["total"] = [total + max(0, value - previous) for total, value, previous
                          in zip(state["total"], current, state["previous"])]
        state["previous"] = current
        read, write = state["total"]
        event, session = state["id"], json.dumps(state)
    else:
        raise ValueError(f"unknown token provider: {provider}")
    if not isinstance(event, str) or not event:
        raise ValueError("token event has no identity")
    return (provider, event, read, write), session


class TokenFileWatcher:
    MODIFY, CREATE, DELETE, MOVED_FROM, MOVED_TO = 0x2, 0x100, 0x200, 0x40, 0x80
    IGNORED, OVERFLOW, ISDIR = 0x8000, 0x4000, 0x40000000

    def __init__(self, directories):
        self.directories = directories
        self.watches, self.pending = {}, set()
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_add_watch.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        try:
            for _, directory in directories:
                parent = directory.parent
                while True:
                    if parent.is_dir():
                        self.watch(parent)
                    if parent == parent.parent:
                        break
                    parent = parent.parent
                if directory.is_dir():
                    self.add_tree(directory)
        except BaseException:
            self.close()
            raise

    def provider(self, path):
        return next((provider for provider, root in self.directories
                     if path == root or root in path.parents), None)

    def relevant(self, path):
        return self.provider(path) is not None or any(path in root.parents for _, root in self.directories)

    def watch(self, directory):
        mask = self.MODIFY | self.CREATE | self.DELETE | self.MOVED_FROM | self.MOVED_TO
        descriptor = self.libc.inotify_add_watch(self.fd, os.fsencode(directory), mask)
        if descriptor < 0:
            raise OSError(ctypes.get_errno(), f"cannot watch token directory {directory}")
        self.watches[descriptor] = directory

    def add_tree(self, directory):
        try:
            self.watch(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False) and self.relevant(path):
                        self.add_tree(path)
                    elif path.suffix == ".jsonl" and self.provider(path):
                        self.pending.add((self.provider(path), path))
        except FileNotFoundError:
            # A directory can disappear between its creation event and enumeration.
            return

    def remove_tree(self, directory):
        for descriptor, path in list(self.watches.items()):
            if path == directory or directory in path.parents:
                del self.watches[descriptor]
                if self.libc.inotify_rm_watch(self.fd, descriptor) < 0 and ctypes.get_errno() != errno.EINVAL:
                    raise OSError(ctypes.get_errno(), f"cannot remove token watch {path}")

    def changes(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            position = 0
            while position < len(data):
                descriptor, mask, _, size = struct.unpack_from("iIII", data, position)
                name = os.fsdecode(data[position + 16:position + 16 + size].split(b"\0", 1)[0])
                position += 16 + size
                if mask & self.OVERFLOW:
                    raise RuntimeError("token file watcher overflowed; restart kdeclickd to re-import missed changes")
                if mask & self.IGNORED:
                    self.watches.pop(descriptor, None)
                    continue
                if descriptor not in self.watches:
                    continue
                path = self.watches[descriptor] / name
                if not self.relevant(path):
                    continue
                if mask & self.ISDIR:
                    if mask & (self.MOVED_FROM | self.DELETE):
                        self.remove_tree(path)
                    elif mask & (self.CREATE | self.MOVED_TO):
                        self.add_tree(path)
                elif path.suffix == ".jsonl" and mask & (self.MODIFY | self.CREATE | self.MOVED_TO):
                    self.pending.add((self.provider(path), path))
        pending, self.pending = self.pending, set()
        return pending

    def close(self):
        os.close(self.fd)


class TokenLedger:
    def __init__(self, database, roots=None):
        self.connection = sqlite3.connect(str(database), timeout=30)
        has_history = self.connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'token_hourly_usage'").fetchone()
        history_version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if history_version not in (0, 1, 2):
            raise ValueError(f"unsupported token history schema: {history_version}")
        self.connection.executescript(TOKEN_SCHEMA)
        if not has_history or history_version < 2:
            # Re-import available timestamps once without erasing or double-counting lifetime totals.
            with self.connection:
                self.connection.execute("DELETE FROM token_files")
                self.connection.execute("DELETE FROM token_hourly_usage")
                self.connection.execute("PRAGMA user_version = 2")
        self.roots = roots if roots is not None else {
            "pi": Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser() / "sessions",
            "claude": Path(os.environ.get("CLAUDE_HOME", "~/.claude")).expanduser() / "projects",
            "codex": Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser(),
        }

        self.directories = tuple((provider, directory) for provider, root in self.roots.items()
                                 for directory in ((root / "sessions", root / "archived_sessions") if provider == "codex" else (root,)))

    def scan_file(self, provider, path):
        row = self.connection.execute("SELECT inode, position, session FROM token_files WHERE path = ?", (str(path),)).fetchone()
        with path.open("rb") as source:
            stat = os.fstat(source.fileno())
            if row and row[0] == stat.st_ino and row[1] == stat.st_size:
                return
            position, session = (row[1], row[2]) if row and row[0] == stat.st_ino and row[1] <= stat.st_size else (0, "")
            source.seek(position)
            with self.connection:
                while line := source.readline():
                    # A writer may be mid-record; leave that byte range for the next scan.
                    if not line.endswith(b"\n"):
                        break
                    relevant = (b'"token_count"' in line or b'"session_meta"' in line) if provider == "codex" else b'"usage"' in line
                    event = None
                    if relevant:
                        try:
                            item = json.loads(line)
                            previous = json.loads(session)["total"] if provider == "codex" and session else (0, 0)
                            event, session = token_event(provider, item, session)
                            if event:
                                self.record_hour(event, item, previous)
                        except (ValueError, KeyError, TypeError, AttributeError) as exc:
                            raise ValueError(f"{path}:{position}: {exc}") from exc
                    if event:
                        self.connection.execute(
                            "INSERT INTO token_usage VALUES (?, ?, ?, ?) ON CONFLICT(provider, event) DO UPDATE SET "
                            "read_tokens = MAX(read_tokens, excluded.read_tokens), write_tokens = MAX(write_tokens, excluded.write_tokens)", event)
                    position = source.tell()
                self.connection.execute("INSERT OR REPLACE INTO token_files VALUES (?, ?, ?, ?)",
                                        (str(path), stat.st_ino, position, session))

    def record_hour(self, event, item, previous):
        provider, identity, read, write = event
        timestamp = item["timestamp"]
        stamp = datetime.fromisoformat(timestamp)
        if stamp.tzinfo is None:
            raise ValueError("token timestamp has no timezone")
        bucket = time.strftime("%Y-%m-%d %H", time.localtime(stamp.timestamp()))
        if provider == "codex":
            # Imported rollouts can stamp many distinct cumulative updates with the same timestamp.
            usage = item["payload"]["info"]["total_token_usage"]
            identity = f"{identity}:{timestamp}:{usage['input_tokens']}:{usage['output_tokens']}"
            read, write = read - previous[0], write - previous[1]
            if read + write == 0:
                return
        # A partial Codex log lacks the preceding cumulative count; a complete copy supplies the smaller, exact delta.
        aggregate = "MIN" if provider == "codex" else "MAX"
        self.connection.execute(
            "INSERT INTO token_hourly_usage VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, event) DO UPDATE SET "
            f"bucket = MIN(bucket, excluded.bucket), read_tokens = {aggregate}(read_tokens, excluded.read_tokens), "
            f"write_tokens = {aggregate}(write_tokens, excluded.write_tokens)", (provider, identity, bucket, read, write))

    def hourly(self):
        cutoff = time.strftime("%Y-%m-%d %H", time.localtime(time.time() - 24 * 3600))
        return dict(self.connection.execute(
            "SELECT bucket, SUM(write_tokens) FROM token_hourly_usage WHERE bucket >= ? GROUP BY bucket", (cutoff,)))

    def scan(self, stop_event=None, paths=None):
        errors, self.failed_paths = [], set()
        if paths is None:
            paths = ((provider, path) for provider, directory in self.directories for path in directory.rglob("*.jsonl"))
        for provider, path in paths:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                self.scan_file(provider, path)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, sqlite3.Error) as exc:
                errors.append(f"{provider}: {exc}")
                self.failed_paths.add((provider, path))
        totals = {provider: {"read": 0, "write": 0} for provider in PROVIDERS}
        for provider, read, write in self.connection.execute(
                "SELECT provider, SUM(read_tokens), SUM(write_tokens) FROM token_usage GROUP BY provider"):
            totals[provider] = {"read": read, "write": write}
        return totals, errors

    def close(self):
        self.connection.close()


class AgentAnalytics:
    def __init__(self, counters, database, log):
        self.counters, self.database, self.log = counters, database, log
        self._lock = threading.Lock()
        self._previous = None
        self._token_previous = None
        self._token_live_until = 0
        self._token_refresh = threading.Event()
        self._state = {"status": "initializing", "working": None, "idle": None, "sampled_at": 0,
                       "error": "", "warning": "", "token_status": "initializing", "tokens": {}, "token_updated": 0, "token_error": "", "token_tps": None}
        self._threads = []
        self._token_hourly = {}

    def request_tokens(self):
        now = time.monotonic()
        with self._lock:
            was_live = now < self._token_live_until
            self._token_live_until = now + TOKEN_SECONDS * 3
        if not was_live:
            self._token_refresh.set()

    def token_interval(self):
        with self._lock:
            return TOKEN_SECONDS if time.monotonic() < self._token_live_until else TOKEN_IDLE_SECONDS

    def token_hourly(self):
        with self._lock:
            return self._token_hourly

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def _update(self, token_hourly=None, **values):
        with self._lock:
            previous = dict(self._state)
            self._state.update(values)
            if token_hourly is not None:
                self._token_hourly = token_hourly
        for key in ("error", "warning", "token_error"):
            if values.get(key) and values[key] != previous[key]:
                self.log(f"agents {key}: {values[key]}")
        self.counters.mark_dirty()

    def observe(self, counts, wall, monotonic):
        working, idle = counts["working"], counts["idle"]
        if any(type(value) is not int or value < 0 for value in (working, idle)):
            raise ValueError("invalid working/idle agent counts")
        if self._previous:
            start, previous_monotonic, previous_working = self._previous
            elapsed = wall - start
            # Monotonic time excludes suspend; do not turn the last pre-sleep count into hours of work.
            if 0 < elapsed <= SAMPLE_SECONDS * 3 and abs(elapsed - (monotonic - previous_monotonic)) < 1:
                while start < wall:
                    local = time.localtime(start)
                    end = min(wall, start - local.tm_min * 60 - local.tm_sec - start % 1 + 3600)
                    self.counters.bump("agent_working_seconds", previous_working * (end - start), when=start)
                    start = end
        self._previous = (wall, monotonic, working)
        self._update(status="ok", working=working, idle=idle, sampled_at=wall, error="",
                     warning=counts.get("warning", ""))

    def observe_tokens(self, totals, errors, hourly, wall, monotonic):
        total = sum(value["write"] for value in totals.values())
        rate = None
        if not errors and self._token_previous is not None:
            start, previous_monotonic, previous_total = self._token_previous
            elapsed = wall - start
            # Imports at startup and gaps across suspend are not current throughput.
            if 0 < elapsed <= TOKEN_SECONDS * 3 and abs(elapsed - (monotonic - previous_monotonic)) < 1:
                if total < previous_total:
                    raise ValueError("lifetime output token total decreased")
                rate = (total - previous_total) / elapsed
        self._token_previous = None if errors else (wall, monotonic, total)
        self._update(tokens=totals, token_hourly=hourly, token_status="error" if errors else "ok",
                     token_updated=wall, token_error="\n".join(errors), token_tps=rate)

    def sample(self):
        try:
            bus = dbus.SessionBus(private=True)
            try:
                proxy = bus.get_object("io.github.dginovker.KDEAgentCounts", "/io/github/dginovker/KDEAgentCounts", introspect=False)
                counts = json.loads(str(dbus.Interface(proxy, "io.github.dginovker.KDEAgentCounts1").GetCounts(timeout=5)))
            finally:
                bus.close()
            if counts.get("error"):
                # Missing registrations may join on a later poll; observed agents still count.
                if str(counts["error"]).startswith("Agent counts incomplete: "):
                    counts["warning"] = counts.pop("error")
                else:
                    raise RuntimeError(counts["error"])
            self.observe(counts, time.time(), time.monotonic())
        except (dbus.DBusException, OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            self._previous = None
            self._update(status="error", working=None, idle=None, sampled_at=time.time(), error=str(exc), warning="")

    def start(self, stop_event, fatal_callback):
        def count_loop():
            try:
                while not stop_event.is_set():
                    self.sample()
                    if stop_event.wait(SAMPLE_SECONDS):
                        break
            except BaseException as exc:
                fatal_callback(RuntimeError(f"agent sampler failed: {exc}"))

        def token_loop():
            ledger = watcher = None
            try:
                ledger = TokenLedger(self.database)
                watcher = TokenFileWatcher(ledger.directories)
                retry = set()
                while not stop_event.is_set():
                    self._token_refresh.clear()
                    started = time.monotonic()
                    changed = watcher.changes() | retry
                    totals, errors = ledger.scan(stop_event, changed)
                    retry = ledger.failed_paths
                    if stop_event.is_set():
                        break
                    self.observe_tokens(totals, errors, ledger.hourly(), time.time(), time.monotonic())
                    interval = self.token_interval()
                    self.log(f"token scan: {len(changed)} changed files, {len(errors)} errors, {time.monotonic() - started:.2f}s; next in {interval}s")
                    self._token_refresh.wait(max(0, interval - (time.monotonic() - started)))
            except BaseException as exc:
                self._update(token_status="error", token_error=str(exc), token_tps=None)
            finally:
                if watcher:
                    watcher.close()
                if ledger:
                    ledger.close()

        for name, target in (("kdeclick-agents", count_loop), ("kdeclick-tokens", token_loop)):
            thread = threading.Thread(target=target, name=name)
            thread.start()
            self._threads.append(thread)

    def stop(self):
        self._token_refresh.set()
        for thread in self._threads:
            thread.join()
