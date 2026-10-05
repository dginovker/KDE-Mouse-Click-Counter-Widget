from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daemon"))
from agent_analytics import AgentAnalytics, TokenLedger
from kdeclickd import Counters, Daemon, Storage, bucket_for


class TokenTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.roots = {provider: self.root / provider for provider in ("pi", "claude", "codex")}
        self.stamp = "2026-10-05T00:00:00Z"
        self.database = self.root / "stats.db"
        self.ledger = TokenLedger(self.database, self.roots)
        self.addCleanup(self.ledger.close)

    def records(self, provider, items, name="session.jsonl", append=False):
        root = self.roots[provider] / "sessions" if provider == "codex" else self.roots[provider]
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if append else "w") as file:
            for item in items:
                file.write(json.dumps(item) + "\n")
        return path

    def pi(self, id="a", output=5):
        return {"type": "message", "id": id, "timestamp": "2026-10-05T00:00:00Z", "message": {
            "role": "assistant", "usage": {"input": 10, "cacheRead": 20, "cacheWrite": 30, "output": output, "totalTokens": 65}}}

    def claude(self, output=5):
        return {"type": "assistant", "timestamp": self.stamp, "message": {"id": "msg-a", "usage": {
            "input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 30,
            "output_tokens": output, "cache_creation": {"ephemeral_5m_input_tokens": 30}}}}

    def codex(self, read=100, write=20, timestamp=None):
        timestamp = timestamp if timestamp is not None else f"2026-10-05T00:00:{write:02}Z"
        return {"type": "event_msg", "timestamp": timestamp, "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": read, "cached_input_tokens": 50, "output_tokens": write, "reasoning_output_tokens": 10}}}}

    def hourly(self):
        with patch("agent_analytics.time.time", return_value=datetime.fromisoformat(self.stamp).timestamp() + 3600):
            return self.ledger.hourly()

    def test_pi_and_claude_include_cache_once(self):
        for provider, event in (("pi", self.pi()), ("claude", self.claude())):
            self.records(provider, [event])
        totals, errors = self.ledger.scan()
        self.assertEqual(errors, [])
        self.assertEqual(totals["pi"], {"read": 60, "write": 5})
        self.assertEqual(totals["claude"], {"read": 60, "write": 5})

    def test_codex_cumulative_snapshots_and_cached_subset_not_double_counted(self):
        self.records("codex", [{"type": "session_meta", "payload": {"id": "thread-a"}},
                               self.codex(), self.codex(), self.codex(200, 40)])
        totals, errors = self.ledger.scan()
        self.assertEqual(errors, [])
        self.assertEqual(totals["codex"], {"read": 200, "write": 40})

    def test_codex_decrease_and_subsequent_growth_are_not_lost(self):
        self.records("codex", [{"type": "session_meta", "payload": {"id": "thread-a"}},
                               self.codex(100, 20), self.codex(20, 5)])
        self.ledger.scan()
        self.records("codex", [self.codex(120, 25)], append=True)
        self.assertEqual(self.ledger.scan()[0]["codex"], {"read": 200, "write": 40})

    def test_streamed_claude_duplicates_and_copies_keep_largest_usage(self):
        self.records("claude", [self.claude(2), self.claude(8)])
        self.records("claude", [self.claude(5)], "subagents/copy.jsonl")
        self.assertEqual(self.ledger.scan()[0]["claude"], {"read": 60, "write": 8})

    def test_pi_forked_history_deduplicates(self):
        self.records("pi", [self.pi()])
        self.records("pi", [self.pi(), self.pi("b")], "fork.jsonl")
        self.assertEqual(self.ledger.scan()[0]["pi"], {"read": 120, "write": 10})

    def test_incremental_scan_and_restart(self):
        self.records("pi", [self.pi()])
        self.ledger.scan()
        self.records("pi", [self.pi("b")], append=True)
        self.assertEqual(self.ledger.scan()[0]["pi"], {"read": 120, "write": 10})
        self.assertEqual(self.ledger.scan()[0]["pi"], {"read": 120, "write": 10})
        other = TokenLedger(self.database, self.roots)
        try:
            self.assertEqual(other.scan()[0]["pi"], {"read": 120, "write": 10})
        finally:
            other.close()

    def test_partial_last_line_waits_for_newline(self):
        path = self.records("pi", [self.pi()])
        with path.open("a") as file:
            file.write(json.dumps(self.pi("b")))
        self.assertEqual(self.ledger.scan()[0]["pi"]["write"], 5)
        with path.open("a") as file:
            file.write("\n")
        self.assertEqual(self.ledger.scan()[0]["pi"]["write"], 10)

    def test_truncated_replaced_deleted_files_do_not_erase_or_double_totals(self):
        path = self.records("pi", [self.pi(), self.pi("b")])
        self.ledger.scan()
        self.records("pi", [self.pi("c")])
        self.assertEqual(self.ledger.scan()[0]["pi"]["write"], 15)
        path.unlink()
        self.assertEqual(self.ledger.scan()[0]["pi"]["write"], 15)
        self.records("pi", [self.pi(), self.pi("b"), self.pi("c")])
        self.assertEqual(self.ledger.scan()[0]["pi"]["write"], 15)

    def test_malformed_record_reports_path_and_rolls_back_offset_and_usage(self):
        path = self.records("pi", [self.pi()])
        with path.open("a") as file:
            file.write('{"usage":bad json}\n')
        totals, errors = self.ledger.scan()
        self.assertEqual(totals["pi"]["read"], 0)
        self.assertIn(str(path), errors[0])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM token_files").fetchone()[0], 0)

    def test_missing_core_token_fields_are_errors_not_zero(self):
        event = self.pi()
        del event["message"]["usage"]["input"]
        self.records("pi", [event])
        self.assertIn("input", self.ledger.scan()[1][0])

    def test_unchanged_files_do_not_rewrite_offsets(self):
        self.records("pi", [self.pi()])
        self.ledger.scan()
        before = self.ledger.connection.total_changes
        self.ledger.scan()
        self.assertEqual(self.ledger.connection.total_changes, before)

    def test_negative_usage_is_error(self):
        self.records("pi", [self.pi(output=-1)])
        self.assertIn("invalid output", self.ledger.scan()[1][0])

    def test_archived_codex_sessions_count_and_copies_deduplicate(self):
        items = [{"type": "session_meta", "payload": {"id": "thread-a"}}, self.codex()]
        self.records("codex", items)
        archive = self.roots["codex"] / "archived_sessions"
        archive.mkdir()
        (archive / "copy.jsonl").write_text("".join(json.dumps(item) + "\n" for item in items))
        self.assertEqual(self.ledger.scan()[0]["codex"], {"read": 100, "write": 20})

    def test_hourly_combines_all_providers_without_repeated_cumulative_counts(self):
        self.records("pi", [self.pi()])
        self.records("claude", [self.claude(), self.claude()], "copy.jsonl")
        self.records("codex", [{"type": "session_meta", "payload": {"id": "thread-a"}},
                               self.codex(), self.codex(), self.codex(200, 40)])
        self.ledger.scan()
        self.assertEqual(sum(self.hourly().values()), 65 + 65 + 240)

    def test_codex_distinct_cumulative_updates_can_share_a_timestamp(self):
        self.records("codex", [{"type": "session_meta", "payload": {"id": "thread-a"}},
                               self.codex(100, 20, self.stamp), self.codex(200, 40, self.stamp),
                               self.codex(200, 40, self.stamp)])
        self.ledger.scan()
        self.assertEqual(sum(self.hourly().values()), 240)

    def test_partial_codex_copies_do_not_inflate_hourly_usage(self):
        self.records("codex", [{"type": "session_meta", "payload": {"id": "thread-a"}}, self.codex(200, 40)], "partial.jsonl")
        self.records("codex", [{"type": "session_meta", "payload": {"id": "thread-a"}},
                               self.codex(), self.codex(200, 40), self.codex(200, 40)], "complete.jsonl")
        totals, errors = self.ledger.scan()
        self.assertEqual(errors, [])
        self.assertEqual(sum(self.hourly().values()), 240)
        self.assertEqual(totals["codex"], {"read": 200, "write": 40})

    def test_history_identity_migration_reimports_without_changing_totals(self):
        self.records("pi", [self.pi()])
        self.ledger.scan()
        with self.ledger.connection:
            self.ledger.connection.execute("PRAGMA user_version = 0")
        other = TokenLedger(self.database, self.roots)
        try:
            self.assertEqual(other.connection.execute("SELECT COUNT(*) FROM token_files").fetchone()[0], 0)
            self.assertEqual(other.scan()[0]["pi"], {"read": 60, "write": 5})
            self.assertEqual(other.connection.execute("PRAGMA user_version").fetchone()[0], 2)
        finally:
            other.close()

    def test_timestamps_assign_distinct_hours_and_streaming_updates_deduplicate(self):
        event = self.pi("b")
        event["timestamp"] = "2026-10-05T01:00:00Z"
        self.records("pi", [self.pi(), event])
        self.records("claude", [self.claude(2), self.claude(8)])
        self.records("claude", [self.claude(5)], "subagents/copy.jsonl")
        self.ledger.scan()
        values = self.hourly()
        first = bucket_for(datetime.fromisoformat(self.stamp).timestamp())
        second = bucket_for(datetime.fromisoformat(event["timestamp"]).timestamp())
        self.assertEqual(values[first], 65 + 68)
        self.assertEqual(values[second], 65)

    def test_hourly_survives_restart_and_source_deletion(self):
        path = self.records("pi", [self.pi()])
        self.ledger.scan()
        path.unlink()
        other = TokenLedger(self.database, self.roots)
        try:
            other.scan()
            with patch("agent_analytics.time.time", return_value=datetime.fromisoformat(self.stamp).timestamp() + 3600):
                self.assertEqual(sum(other.hourly().values()), 65)
        finally:
            other.close()

    def test_history_migration_backfills_without_changing_lifetime_totals(self):
        self.records("pi", [self.pi()])
        self.ledger.scan()
        with self.ledger.connection:
            self.ledger.connection.execute("DROP TABLE token_hourly_usage")
        other = TokenLedger(self.database, self.roots)
        try:
            totals, errors = other.scan()
            self.assertEqual(errors, [])
            self.assertEqual(totals["pi"], {"read": 60, "write": 5})
            with patch("agent_analytics.time.time", return_value=datetime.fromisoformat(self.stamp).timestamp() + 3600):
                self.assertEqual(sum(other.hourly().values()), 65)
        finally:
            other.close()

    def test_missing_usage_timestamp_is_reported(self):
        event = self.claude()
        del event["timestamp"]
        self.records("claude", [event])
        self.assertIn("timestamp", self.ledger.scan()[1][0])

    def test_only_usage_metadata_is_persisted(self):
        event = self.pi()
        event["message"]["content"] = "SECRET-CONVERSATION-TEXT"
        self.records("pi", [event])
        self.ledger.scan()
        self.ledger.connection.commit()
        self.assertNotIn(b"SECRET-CONVERSATION-TEXT", self.database.read_bytes())


class ActiveAgentTests(unittest.TestCase):
    def setUp(self):
        self.counters = Counters({}, {})
        self.analytics = AgentAnalytics(self.counters, ":memory:", lambda message: None)
        self.start = time.mktime((2026, 10, 5, 10, 0, 0, 0, 0, -1))

    def observe(self, wall, monotonic, working=4):
        self.analytics.observe({"working": working, "idle": 3}, self.start + wall, monotonic)

    def seconds(self):
        return self.counters.snapshot()[0].get("agent_working_seconds", 0)

    def test_first_sample_is_only_baseline(self):
        self.observe(0, 0)
        self.assertEqual(self.seconds(), 0)

    def test_integrates_prior_working_count_not_idle(self):
        self.observe(0, 0)
        self.observe(5, 5, working=2)
        self.observe(10, 10)
        self.assertEqual(self.seconds(), 30)

    def test_sleep_short_suspend_and_downtime_count_zero(self):
        for wall, monotonic in ((3600, 5), (10, 5), (3600, 3600)):
            with self.subTest(wall=wall, monotonic=monotonic):
                self.analytics._previous = None
                self.observe(0, 0)
                self.observe(wall, monotonic)
                self.assertEqual(self.seconds(), 0)

    def test_clock_moved_backwards_counts_zero(self):
        self.observe(10, 0)
        self.observe(5, 5)
        self.assertEqual(self.seconds(), 0)

    def test_hour_crossing_splits_seconds(self):
        self.observe(3598, 0)
        self.observe(3603, 5)
        hourly = self.counters.snapshot()[1]
        self.assertEqual(hourly[(bucket_for(self.start), "agent_working_seconds")], 8)
        self.assertEqual(hourly[(bucket_for(self.start + 3600), "agent_working_seconds")], 12)

    def test_fractional_hour_crossing_uses_exact_boundary(self):
        self.observe(3599.75, 0)
        self.observe(3604.75, 5)
        hourly = self.counters.snapshot()[1]
        self.assertEqual(hourly[(bucket_for(self.start), "agent_working_seconds")], 1)
        self.assertEqual(hourly[(bucket_for(self.start + 3600), "agent_working_seconds")], 19)

    def state_at(self, counters, moment):
        daemon = Daemon.__new__(Daemon)
        daemon.counters = counters
        daemon.network = type("Network", (), {"snapshot": lambda self: {}})()
        daemon.agents = self.analytics
        daemon._revision, daemon._instance = 0, "test"
        with patch("kdeclickd.time.time", return_value=moment):
            return json.loads(daemon.build_state())

    def test_current_hour_uses_elapsed_wall_time_including_unobserved_time(self):
        self.observe(0, 0)
        self.observe(5, 5)
        state = self.state_at(self.counters, self.start + 1800)
        self.assertAlmostEqual(state["agents"]["history"]["values"][-1], 20 / 1800)

    def test_state_exposes_hourly_tokens_with_empty_hours_zero_filled(self):
        self.analytics._update(token_hourly={bucket_for(self.start): 12345})
        state = self.state_at(self.counters, self.start + 1800)
        history = state["agents"]["token_history"]
        self.assertEqual(len(history["labels"]), 24)
        self.assertEqual(history["values"], [0] * 23 + [12345])

    def test_invalid_lookup_breaks_interval_and_reports_failure(self):
        self.observe(0, 0)
        with patch("agent_analytics.dbus.SessionBus", side_effect=OSError("missing count service")):
            self.analytics.sample()
        self.observe(10, 10)
        self.assertEqual(self.seconds(), 0)
        self.assertEqual(self.analytics.snapshot()["status"], "ok")

    def test_invalid_counts_rejected(self):
        for working in (-1, 1.5, True, None):
            with self.assertRaises(ValueError):
                self.analytics.observe({"working": working, "idle": 0}, 0, 0)

    def test_storage_roundtrip_and_full_hour_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "stats.db"
            self.observe(0, 0)
            self.observe(5, 5)
            storage = Storage(database)
            storage.write(self.counters.drain_pending())
            storage.close()
            with patch("kdeclickd.time.time", return_value=self.start + 7200):
                totals, hourly = Storage.load(database)
            state = self.state_at(Counters(totals, hourly), self.start + 7200)
            self.assertEqual(state["schema"], 5)
            self.assertEqual(len(state["agents"]["history"]["values"]), 24)
            self.assertAlmostEqual(state["agents"]["history"]["values"][-3], 20 / 3600)
            self.assertEqual(state["agents"]["history"]["values"][-1], 0)


if __name__ == "__main__":
    unittest.main()
