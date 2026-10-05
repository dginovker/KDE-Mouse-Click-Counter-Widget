"""Local usage metadata and observed working-agent time; never stores message content."""

from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import dbus
import threading
import time

PROVIDERS = ("pi", "claude", "codex")
SAMPLE_SECONDS = 5
TOKEN_SECONDS = 60
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
            "SELECT bucket, SUM(read_tokens + write_tokens) FROM token_hourly_usage WHERE bucket >= ? GROUP BY bucket", (cutoff,)))

    def scan(self, stop_event=None):
        errors = []
        for provider, root in self.roots.items():
            if stop_event is not None and stop_event.is_set():
                break
            roots = (root / "sessions", root / "archived_sessions") if provider == "codex" else (root,)
            for directory in roots:
                for path in directory.rglob("*.jsonl"):
                    if stop_event is not None and stop_event.is_set():
                        break
                    try:
                        self.scan_file(provider, path)
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        errors.append(f"{provider}: {exc}")
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
        self._state = {"status": "initializing", "working": None, "idle": None, "sampled_at": 0,
                       "error": "", "token_status": "initializing", "tokens": {}, "token_updated": 0, "token_error": ""}
        self._threads = []
        self._token_hourly = {}

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
        for key in ("error", "token_error"):
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
        self._update(status="ok", working=working, idle=idle, sampled_at=wall, error="")

    def sample(self):
        try:
            bus = dbus.SessionBus(private=True)
            try:
                proxy = bus.get_object("io.github.dginovker.KDEAgentCounts", "/io/github/dginovker/KDEAgentCounts", introspect=False)
                counts = json.loads(str(dbus.Interface(proxy, "io.github.dginovker.KDEAgentCounts1").GetCounts(timeout=5)))
            finally:
                bus.close()
            if counts.get("error"):
                raise RuntimeError(counts["error"])
            self.observe(counts, time.time(), time.monotonic())
        except (dbus.DBusException, OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            self._previous = None
            self._update(status="error", working=None, idle=None, sampled_at=time.time(), error=str(exc))

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
            ledger = None
            try:
                ledger = TokenLedger(self.database)
                while not stop_event.is_set():
                    started = time.monotonic()
                    totals, errors = ledger.scan(stop_event)
                    if stop_event.is_set():
                        break
                    self._update(tokens=totals, token_hourly=ledger.hourly(), token_status="error" if errors else "ok",
                                 token_updated=time.time(), token_error="\n".join(errors))
                    self.log(f"token scan: {sum(value['read'] + value['write'] for value in totals.values()):,} tokens, {len(errors)} errors, {time.monotonic() - started:.2f}s")
                    if stop_event.wait(TOKEN_SECONDS):
                        break
            except BaseException as exc:
                self._update(token_status="error", token_error=str(exc))
            finally:
                if ledger:
                    ledger.close()

        for name, target in (("kdeclick-agents", count_loop), ("kdeclick-tokens", token_loop)):
            thread = threading.Thread(target=target, name=name)
            thread.start()
            self._threads.append(thread)

    def stop(self):
        for thread in self._threads:
            thread.join()
