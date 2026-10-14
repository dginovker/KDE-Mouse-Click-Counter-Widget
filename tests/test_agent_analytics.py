from datetime import datetime
import json
import struct
import threading
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daemon"))
from agent_analytics import AgentAnalytics, TokenFileWatcher, TokenLedger
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

    def test_hourly_combines_output_across_providers_without_input_or_duplicate_counts(self):
        self.records("pi", [self.pi()])
        self.records("claude", [self.claude(), self.claude()], "copy.jsonl")
        self.records("codex", [{"type": "session_meta", "payload": {"id": "thread-a"}},
                               self.codex(), self.codex(), self.codex(200, 40)])
        self.ledger.scan()
        self.assertEqual(sum(self.hourly().values()), 5 + 5 + 40)

    def test_codex_distinct_cumulative_updates_can_share_a_timestamp(self):
        self.records("codex", [{"type": "session_meta", "payload": {"id": "thread-a"}},
                               self.codex(100, 20, self.stamp), self.codex(200, 40, self.stamp),
                               self.codex(200, 40, self.stamp)])
        self.ledger.scan()
        self.assertEqual(sum(self.hourly().values()), 40)

    def test_partial_codex_copies_do_not_inflate_hourly_usage(self):
        self.records("codex", [{"type": "session_meta", "payload": {"id": "thread-a"}}, self.codex(200, 40)], "partial.jsonl")
        self.records("codex", [{"type": "session_meta", "payload": {"id": "thread-a"}},
                               self.codex(), self.codex(200, 40), self.codex(200, 40)], "complete.jsonl")
        totals, errors = self.ledger.scan()
        self.assertEqual(errors, [])
        self.assertEqual(sum(self.hourly().values()), 40)
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
        self.assertEqual(values[first], 5 + 8)
        self.assertEqual(values[second], 5)

    def test_hourly_survives_restart_and_source_deletion(self):
        path = self.records("pi", [self.pi()])
        self.ledger.scan()
        path.unlink()
        other = TokenLedger(self.database, self.roots)
        try:
            other.scan()
            with patch("agent_analytics.time.time", return_value=datetime.fromisoformat(self.stamp).timestamp() + 3600):
                self.assertEqual(sum(other.hourly().values()), 5)
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
                self.assertEqual(sum(other.hourly().values()), 5)
        finally:
            other.close()

    def test_missing_usage_timestamp_is_reported(self):
        event = self.claude()
        del event["timestamp"]
        self.records("claude", [event])
        self.assertIn("timestamp", self.ledger.scan()[1][0])

    def test_live_rate_uses_deduplicated_incremental_usage(self):
        analytics = AgentAnalytics(Counters({}, {}), self.database, lambda message: None)
        self.records("pi", [self.pi()])
        totals, errors = self.ledger.scan()
        analytics.observe_tokens(totals, errors, self.hourly(), 100, 0)
        self.assertIsNone(analytics.snapshot()["token_tps"])
        self.records("pi", [self.pi(), self.pi("b")], append=True)
        totals, errors = self.ledger.scan()
        analytics.observe_tokens(totals, errors, self.hourly(), 105, 5)
        self.assertEqual(analytics.snapshot()["token_tps"], 5 / 5)

    def test_only_usage_metadata_is_persisted(self):
        event = self.pi()
        event["message"]["content"] = "SECRET-CONVERSATION-TEXT"
        self.records("pi", [event])
        self.ledger.scan()
        self.ledger.connection.commit()
        self.assertNotIn(b"SECRET-CONVERSATION-TEXT", self.database.read_bytes())


class TokenWatcherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.roots = {provider: self.root / provider for provider in ("pi", "claude", "codex")}
        self.ledger = TokenLedger(self.root / "stats.db", self.roots)
        self.addCleanup(self.ledger.close)
        self.watcher = TokenFileWatcher(self.ledger.directories)
        self.addCleanup(self.watcher.close)

    def file(self, provider="pi", name="project/log.jsonl"):
        directory = self.roots[provider] / "sessions" if provider == "codex" else self.roots[provider]
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"session"}\n')
        return path

    def test_startup_discovers_existing_history_once(self):
        paths = {self.file(provider) for provider in self.roots}
        watcher = TokenFileWatcher(self.ledger.directories)
        self.addCleanup(watcher.close)
        self.assertEqual({path for _, path in watcher.changes()}, paths)
        self.assertEqual(watcher.changes(), set())

    def test_new_roots_and_nested_directories_are_discovered(self):
        paths = {self.file(provider) for provider in self.roots}
        self.assertEqual({path for _, path in self.watcher.changes()}, paths)
        self.assertEqual(self.watcher.changes(), set())

    def test_only_changed_jsonl_files_are_queued_and_events_coalesce(self):
        path = self.file()
        self.watcher.changes()
        for _ in range(3):
            with path.open("a") as file:
                file.write('{"type":"message"}\n')
        (path.parent / "unrelated.txt").write_text("not usage")
        (self.root / "unrelated.jsonl").write_text("not a session")
        self.assertEqual(self.watcher.changes(), {("pi", path)})
        self.assertEqual(self.watcher.changes(), set())

    def test_atomic_replacement_and_file_moves_are_detected(self):
        path = self.file()
        self.watcher.changes()
        replacement = path.with_suffix(".tmp")
        replacement.write_text('{"new":"content"}\n')
        replacement.replace(path)
        self.assertEqual(self.watcher.changes(), {("pi", path)})
        archive = self.roots["codex"] / "archived_sessions"
        archive.mkdir(parents=True)
        moved = archive / path.name
        path.replace(moved)
        self.assertEqual(self.watcher.changes(), {("codex", moved)})

    def test_existing_directory_moved_into_root_is_imported_and_keeps_watching(self):
        self.file()
        self.watcher.changes()
        incoming = self.root / "incoming"
        incoming.mkdir()
        (incoming / "moved.jsonl").write_text("{}\n")
        target = self.roots["pi"] / "incoming"
        incoming.rename(target)
        path = target / "moved.jsonl"
        self.assertEqual(self.watcher.changes(), {("pi", path)})
        path.write_text('{"changed":true}\n')
        self.assertEqual(self.watcher.changes(), {("pi", path)})

    def test_directory_rename_remaps_watches_to_new_paths(self):
        path = self.file()
        self.watcher.changes()
        target = path.parent.with_name("renamed")
        path.parent.rename(target)
        renamed = target / path.name
        self.assertEqual(self.watcher.changes(), {("pi", renamed)})
        renamed.write_text('{"changed":true}\n')
        self.assertEqual(self.watcher.changes(), {("pi", renamed)})

    def test_removed_root_can_be_recreated(self):
        path = self.file(name="log.jsonl")
        self.watcher.changes()
        path.unlink()
        path.parent.rmdir()
        self.watcher.changes()
        recreated = self.file(name="log.jsonl")
        self.assertEqual(self.watcher.changes(), {("pi", recreated)})

    def test_queue_overflow_is_an_explicit_error_not_a_polling_fallback(self):
        overflow = struct.pack("iIII", -1, TokenFileWatcher.OVERFLOW, 0, 0)
        with patch("agent_analytics.os.read", return_value=overflow):
            with self.assertRaisesRegex(RuntimeError, "watcher overflowed"):
                self.watcher.changes()

    def test_changed_file_scans_never_walk_history(self):
        path = self.file()
        changes = self.watcher.changes()
        with patch.object(Path, "rglob", side_effect=AssertionError("full scan attempted")):
            self.assertEqual(self.ledger.scan(paths=changes)[1], [])
            self.assertEqual(self.ledger.scan(paths=set())[1], [])
        path.unlink()
        self.assertEqual(self.ledger.scan(paths={("pi", path)})[1], [])

    def test_malformed_changed_files_are_identified_for_retry(self):
        path = self.file()
        path.write_text('{"usage": invalid}\n')
        _, errors = self.ledger.scan(paths=self.watcher.changes())
        self.assertTrue(errors)
        self.assertEqual(self.ledger.failed_paths, {("pi", path)})
        path.write_text('{}\n')
        self.assertEqual(self.ledger.scan(paths=self.ledger.failed_paths)[1], [])
        self.assertEqual(self.ledger.failed_paths, set())


class TokenCadenceTests(unittest.TestCase):
    def setUp(self):
        self.analytics = AgentAnalytics(Counters({}, {}), ":memory:", lambda message: None)

    def test_closed_popup_uses_sixty_seconds(self):
        self.assertEqual(self.analytics.token_interval(), 60)

    def test_open_popup_wakes_scanner_and_heartbeats_extend_fast_cadence(self):
        with patch("agent_analytics.time.monotonic", return_value=100):
            self.analytics.request_tokens()
            self.assertEqual(self.analytics.token_interval(), 5)
        self.assertTrue(self.analytics._token_refresh.is_set())
        self.analytics._token_refresh.clear()
        with patch("agent_analytics.time.monotonic", return_value=110):
            self.analytics.request_tokens()
        self.assertFalse(self.analytics._token_refresh.is_set())
        with patch("agent_analytics.time.monotonic", return_value=124):
            self.assertEqual(self.analytics.token_interval(), 5)
        with patch("agent_analytics.time.monotonic", return_value=125):
            self.assertEqual(self.analytics.token_interval(), 60)

    def test_reopening_popup_wakes_scanner_again(self):
        with patch("agent_analytics.time.monotonic", return_value=100):
            self.analytics.request_tokens()
        self.analytics._token_refresh.clear()
        with patch("agent_analytics.time.monotonic", return_value=120):
            self.analytics.request_tokens()
        self.assertTrue(self.analytics._token_refresh.is_set())

    def test_shutdown_wakes_closed_popup_scanner(self):
        self.analytics.stop()
        self.assertTrue(self.analytics._token_refresh.is_set())

    def test_live_request_interrupts_sixty_second_wait_and_shutdown_is_prompt(self):
        stop = threading.Event()
        scanned = threading.Event()
        ledger = Mock(directories=(), failed_paths=set())
        ledger.scan.return_value = ({"pi": {"read": 0, "write": 0}}, [])
        ledger.hourly.return_value = {}
        watcher = Mock()
        watcher.changes.return_value = set()
        self.analytics.log = lambda message: scanned.set()
        with patch.object(self.analytics, "sample"), \
                patch("agent_analytics.TokenLedger", return_value=ledger), \
                patch("agent_analytics.TokenFileWatcher", return_value=watcher):
            self.analytics.start(stop, lambda error: self.fail(str(error)))
            try:
                self.assertTrue(scanned.wait(1))
                scanned.clear()
                self.analytics.request_tokens()
                self.assertTrue(scanned.wait(1))
                self.assertEqual(ledger.scan.call_count, 2)
                ledger.scan.assert_called_with(stop, set())
            finally:
                stop.set()
                self.analytics.stop()
        watcher.close.assert_called_once()
        ledger.close.assert_called_once()


class TokenRateTests(unittest.TestCase):
    def setUp(self):
        self.analytics = AgentAnalytics(Counters({}, {}), ":memory:", lambda message: None)

    def observe(self, total, wall, monotonic, errors=()):
        self.analytics.observe_tokens({"pi": {"read": 1000000, "write": total}}, errors, {}, wall, monotonic)
        return self.analytics.snapshot()["token_tps"]

    def test_first_scan_does_not_count_lifetime_import_as_current_usage(self):
        self.assertIsNone(self.observe(1000000, 100, 0))

    def test_rate_uses_actual_elapsed_time_and_idle_is_zero(self):
        self.observe(100, 100, 0)
        self.assertEqual(self.observe(160, 106, 6), 10)
        self.assertEqual(self.observe(160, 111, 11), 0)

    def test_input_and_cache_increases_do_not_count_as_inference(self):
        self.analytics.observe_tokens({"pi": {"read": 100, "write": 20}}, [], {}, 100, 0)
        self.analytics.observe_tokens({"pi": {"read": 1000000, "write": 20}}, [], {}, 105, 5)
        self.assertEqual(self.analytics.snapshot()["token_tps"], 0)

    def test_live_inference_sums_output_across_all_providers(self):
        before = {provider: {"read": 1000000, "write": 100} for provider in ("pi", "claude", "codex")}
        after = {provider: {"read": 2000000, "write": 100 + delta}
                 for provider, delta in (("pi", 50), ("claude", 100), ("codex", 150))}
        self.analytics.observe_tokens(before, [], {}, 100, 0)
        self.analytics.observe_tokens(after, [], {}, 105, 5)
        self.assertEqual(self.analytics.snapshot()["token_tps"], 60)

    def test_failed_scan_and_first_recovery_have_no_rate(self):
        self.observe(100, 100, 0)
        self.assertIsNone(self.observe(200, 105, 5, ["broken session log"]))
        self.assertEqual(self.analytics.snapshot()["token_status"], "error")
        self.assertIsNone(self.observe(300, 110, 10))
        self.assertEqual(self.observe(350, 115, 15), 10)

    def test_suspend_long_gaps_and_clock_changes_reset_baseline(self):
        for wall, monotonic in ((3600, 5), (10, 5), (3600, 3600), (-5, 5), (0, 5)):
            with self.subTest(wall=wall, monotonic=monotonic):
                self.analytics._token_previous = None
                self.observe(100, 100, 0)
                self.assertIsNone(self.observe(200, 100 + wall, monotonic))
                self.assertEqual(self.observe(250, 105 + wall, monotonic + 5), 10)

    def test_decreasing_lifetime_total_is_an_error(self):
        self.observe(100, 100, 0)
        with self.assertRaisesRegex(ValueError, "token total decreased"):
            self.observe(99, 105, 5)


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

    def test_state_exposes_hourly_tps_with_empty_hours_zero_filled(self):
        self.analytics._update(token_hourly={bucket_for(self.start): 12345,
                                           bucket_for(self.start - 3600): 7200})
        state = self.state_at(self.counters, self.start + 1800)
        history = state["agents"]["token_history"]
        self.assertEqual(len(history["labels"]), 24)
        self.assertEqual(history["values"], [0] * 22 + [2, 12345 / 1800])

    def test_hour_boundary_token_rate_stays_finite(self):
        self.analytics._update(token_hourly={bucket_for(self.start): 12})
        state = self.state_at(self.counters, self.start)
        self.assertEqual(state["agents"]["token_history"]["values"][-1], 12)

    def test_invalid_lookup_breaks_interval_and_reports_failure(self):
        self.observe(0, 0)
        with patch("agent_analytics.dbus.SessionBus", side_effect=OSError("missing count service")):
            self.analytics.sample()
        self.observe(10, 10)
        self.assertEqual(self.seconds(), 0)
        self.assertEqual(self.analytics.snapshot()["status"], "ok")

    def sample_counts(self, counts):
        bus = Mock()
        interface = Mock()
        interface.GetCounts.return_value = json.dumps(counts)
        with patch("agent_analytics.dbus.SessionBus", return_value=bus), \
                patch("agent_analytics.dbus.Interface", return_value=interface):
            self.analytics.sample()
        bus.close.assert_called_once()

    def test_missing_registration_counts_available_agents_without_widget_error(self):
        logs = []
        self.analytics.log = logs.append
        warning = "Agent counts incomplete: pi PID 123: live presence missing"
        self.sample_counts({"working": 4, "idle": 3, "error": warning})
        state = self.analytics.snapshot()
        self.assertEqual((state["status"], state["working"], state["idle"], state["error"]), ("ok", 4, 3, ""))
        self.assertEqual(state["warning"], warning)
        self.assertEqual(logs, [f"agents warning: {warning}"])
        self.sample_counts({"working": 4, "idle": 3, "error": warning})
        self.assertEqual(len(logs), 1)
        self.sample_counts({"working": 5, "idle": 3})
        self.assertEqual(self.analytics.snapshot()["working"], 5)
        self.assertEqual(self.analytics.snapshot()["warning"], "")

    def test_incomplete_counts_still_require_valid_working_and_idle(self):
        for counts in ({"error": "Agent counts incomplete: missing"},
                       {"working": -1, "idle": 2, "error": "Agent counts incomplete: missing"}):
            self.sample_counts(counts)
            self.assertEqual(self.analytics.snapshot()["status"], "error")
            self.assertIsNone(self.analytics.snapshot()["working"])

    def test_lookup_errors_are_not_hidden_even_if_counts_are_present(self):
        self.sample_counts({"working": 4, "idle": 3, "error": "dashboard unavailable"})
        self.assertEqual(self.analytics.snapshot()["status"], "error")
        self.assertEqual(self.analytics.snapshot()["error"], "dashboard unavailable")

    def test_incomplete_counts_continue_recording_observed_agent_history(self):
        with patch("agent_analytics.time.time", side_effect=[self.start, self.start + 5]), \
                patch("agent_analytics.time.monotonic", side_effect=[0, 5]):
            self.sample_counts({"working": 4, "idle": 3, "error": "Agent counts incomplete: missing"})
            self.sample_counts({"working": 5, "idle": 3})
        self.assertEqual(self.seconds(), 20)

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
            self.assertEqual(state["schema"], 6)
            self.assertEqual(len(state["agents"]["history"]["values"]), 24)
            self.assertAlmostEqual(state["agents"]["history"]["values"][-3], 20 / 3600)
            self.assertEqual(state["agents"]["history"]["values"][-1], 0)


if __name__ == "__main__":
    unittest.main()
