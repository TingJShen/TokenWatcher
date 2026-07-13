from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "token_watcher.py"
SPEC = importlib.util.spec_from_file_location("token_watcher", MODULE_PATH)
assert SPEC and SPEC.loader
token_watcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = token_watcher
SPEC.loader.exec_module(token_watcher)


class FakeWatcher:
    def __init__(self) -> None:
        self.available = True
        self.changes: set[Path] = set()

    def emit(self, path: Path) -> None:
        self.changes.add(path)

    def drain(self) -> set[Path]:
        changes = set(self.changes)
        self.changes.clear()
        return changes

    def close(self) -> None:
        return


class TokenWatcherTests(unittest.TestCase):
    @staticmethod
    def _codex_lines(
        session_id: str,
        total: int,
        timestamp: datetime,
        cumulative: int | None = None,
    ) -> str:
        info = {"last_token_usage": {"total_tokens": total}}
        if cumulative is not None:
            info["total_token_usage"] = {"total_tokens": cumulative}
        return "\n".join(
            (
                json.dumps({"type": "session_meta", "payload": {"id": session_id}}),
                json.dumps(
                    {"type": "turn_context", "payload": {"model": "gpt-test"}}
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": timestamp.isoformat(),
                        "payload": {
                            "type": "token_count",
                            "info": info,
                        },
                    }
                ),
            )
        ) + "\n"

    @staticmethod
    def _claude_line(message_id: str, total: int, timestamp: datetime) -> str:
        return json.dumps(
            {
                "type": "assistant",
                "timestamp": timestamp.isoformat(),
                "sessionId": "claude-session",
                "message": {
                    "id": message_id,
                    "model": "claude-test",
                    "usage": {"input_tokens": total, "output_tokens": 0},
                },
            }
        ) + "\n"

    def test_format_tokens_uses_exact_grouped_value(self) -> None:
        self.assertEqual(token_watcher.format_tokens(20_043_264_243), "20,043,264,243")

    def test_active_periods(self) -> None:
        now = date(2026, 7, 13)
        self.assertEqual(
            token_watcher.active_periods(now, now),
            ("cumulative", "month", "week", "today"),
        )

    def test_missing_report_creates_empty_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_dir = Path(temporary_directory)
            baseline = token_watcher.load_baseline(report_dir)
            self.assertEqual(baseline.report_dir, report_dir)
            self.assertEqual(baseline.report_mtime, 0.0)
            self.assertFalse(baseline.periods["cumulative"])

    def test_report_lookup_walks_up_from_directory_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "TokenWatcher" / "TokenWatcher.runtime"
            runtime.mkdir(parents=True)
            report_dir = root / "outputs" / token_watcher.REPORT_FOLDER_NAME
            report_dir.mkdir(parents=True)
            (report_dir / "model_total.csv").write_text(
                "platform,model,total_tokens\nCodex,gpt-test,1\n",
                encoding="utf-8",
            )
            with patch.object(token_watcher.sys, "frozen", True, create=True), patch.object(
                token_watcher.sys, "executable", str(runtime / "TokenWatcher.exe")
            ), patch.object(token_watcher.Path, "cwd", return_value=runtime):
                self.assertEqual(token_watcher.find_report_dir(), report_dir)

    def test_usage_snapshot_cache_is_loaded_before_background_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "usage_snapshot_cache.json"
            now = datetime.now(timezone.utc)
            periods = {period: {} for period in token_watcher.PERIODS}
            call_periods = {period: {} for period in token_watcher.PERIODS}
            periods["cumulative"][("Codex", "gpt-test")] = 123
            call_periods["cumulative"][("Codex", "gpt-test")] = 4
            snapshot = token_watcher.UsageSnapshot(
                periods=periods,
                call_periods=call_periods,
                updated_at=now,
                report_time=now,
                source_status=("cached",),
            )
            writer = token_watcher.UsageEngine(
                report_dir=root,
                snapshot_cache_path=cache_path,
            )
            writer.snapshot = snapshot
            writer.stop()

            reader = token_watcher.UsageEngine(
                report_dir=root,
                snapshot_cache_path=cache_path,
            )
            loaded = reader.get_snapshot()
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(
                loaded.periods["cumulative"][("Codex", "gpt-test")],
                123,
            )
            self.assertEqual(
                loaded.call_periods["cumulative"][("Codex", "gpt-test")],
                4,
            )

    def test_usage_snapshot_cache_does_not_rewrite_unchanged_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "usage_snapshot_cache.json"
            now = datetime.now(timezone.utc)
            snapshot = token_watcher.UsageSnapshot(
                periods={period: {} for period in token_watcher.PERIODS},
                call_periods={period: {} for period in token_watcher.PERIODS},
                updated_at=now,
                report_time=now,
                source_status=(),
            )
            first = token_watcher.UsageEngine(
                report_dir=root,
                snapshot_cache_path=cache_path,
            )
            first.snapshot = snapshot
            first.stop()

            second = token_watcher.UsageEngine(
                report_dir=root,
                snapshot_cache_path=cache_path,
            )
            with patch.object(token_watcher.os, "replace") as replace:
                second.stop()
            replace.assert_not_called()

    def test_usage_snapshot_cache_is_scoped_to_report_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_report = root / "first"
            second_report = root / "second"
            first_report.mkdir()
            second_report.mkdir()
            cache_path = root / "usage_snapshot_cache.json"
            now = datetime.now(timezone.utc)
            snapshot = token_watcher.UsageSnapshot(
                periods={period: {} for period in token_watcher.PERIODS},
                call_periods={period: {} for period in token_watcher.PERIODS},
                updated_at=now,
                report_time=now,
                source_status=(),
            )
            writer = token_watcher.UsageEngine(
                report_dir=first_report,
                snapshot_cache_path=cache_path,
            )
            writer.snapshot = snapshot
            writer.stop()

            reader = token_watcher.UsageEngine(
                report_dir=second_report,
                snapshot_cache_path=cache_path,
            )
            self.assertIsNone(reader.get_snapshot())

    def test_background_loop_saves_snapshot_only_after_first_refresh(self) -> None:
        class StopAfterThreeWaits:
            def __init__(self) -> None:
                self.waits = 0

            def is_set(self) -> bool:
                return self.waits >= 3

            def wait(self, _timeout: float) -> bool:
                self.waits += 1
                return False

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            engine = token_watcher.UsageEngine(
                report_dir=root,
                snapshot_cache_path=root / "usage_snapshot_cache.json",
            )
            engine.stop_event = StopAfterThreeWaits()
            with patch.object(engine, "refresh_once"), patch.object(
                engine, "_save_snapshot_cache"
            ) as save:
                engine._run()
            self.assertEqual(save.call_count, 1)

    def test_codex_cold_files_are_not_polled_and_changes_are_event_driven(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            session_dir = home / ".codex" / "sessions" / "2026" / "07" / "13"
            session_dir.mkdir(parents=True)
            now = datetime.now(timezone.utc)
            cold = session_dir / "cold.jsonl"
            hot = session_dir / "hot.jsonl"
            cold.write_text(self._codex_lines("cold", 1, now), encoding="utf-8")
            hot.write_text(self._codex_lines("hot", 10, now), encoding="utf-8")
            old = time.time() - 3600
            os.utime(cold, (old, old))
            watcher = FakeWatcher()
            with patch.object(token_watcher.Path, "home", return_value=home):
                tracker = token_watcher.CodexTailTracker(
                    now - timedelta(days=2), watcher=watcher
                )

            self.assertFalse(tracker.states[cold].watching)
            self.assertTrue(tracker.states[hot].watching)
            tracker.states[hot].next_check = 0
            original_stat = token_watcher.Path.stat
            touched: list[Path] = []

            def counted_stat(path: Path, *args, **kwargs):
                touched.append(path)
                return original_stat(path, *args, **kwargs)

            with patch.object(token_watcher.Path, "rglob", side_effect=AssertionError), patch.object(
                token_watcher.Path, "stat", counted_stat
            ):
                tracker.poll()
            self.assertNotIn(cold, touched)
            self.assertIn(hot, touched)

            with cold.open("a", encoding="utf-8") as handle:
                handle.write(self._codex_lines("cold", 20, now + timedelta(seconds=1)))
            watcher.emit(cold)
            tracker.poll()
            self.assertTrue(tracker.states[cold].watching)
            self.assertEqual(
                tracker.periods["cumulative"][("Codex", "gpt-test")], 31
            )

    def test_new_empty_codex_file_remains_watched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            session_dir = home / ".codex" / "sessions" / "2026" / "07" / "13"
            session_dir.mkdir(parents=True)
            now = datetime.now(timezone.utc)
            watcher = FakeWatcher()
            with patch.object(token_watcher.Path, "home", return_value=home):
                tracker = token_watcher.CodexTailTracker(
                    now - timedelta(days=1), watcher=watcher
                )
            new_file = session_dir / "new-empty.jsonl"
            new_file.touch()
            watcher.emit(new_file)
            tracker.poll()
            self.assertTrue(tracker.states[new_file].watching)

            new_file.write_text(
                self._codex_lines("new", 30, now + timedelta(seconds=1)),
                encoding="utf-8",
            )
            watcher.emit(new_file)
            tracker.poll()
            self.assertEqual(
                tracker.periods["cumulative"][("Codex", "gpt-test")], 30
            )

    def test_codex_repeated_cumulative_snapshots_are_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            session_dir = home / ".codex" / "sessions" / "2026" / "07" / "14"
            session_dir.mkdir(parents=True)
            now = datetime.now(timezone.utc)
            original = session_dir / "original.jsonl"
            forked = session_dir / "forked.jsonl"
            original.write_text(
                self._codex_lines("shared-session", 10, now, cumulative=10),
                encoding="utf-8",
            )
            forked.write_text(
                self._codex_lines(
                    "shared-session",
                    10,
                    now + timedelta(seconds=1),
                    cumulative=10,
                )
                + self._codex_lines(
                    "shared-session",
                    20,
                    now + timedelta(seconds=2),
                    cumulative=30,
                ),
                encoding="utf-8",
            )
            watcher = FakeWatcher()
            with patch.object(token_watcher.Path, "home", return_value=home):
                tracker = token_watcher.CodexTailTracker(
                    now - timedelta(days=1), watcher=watcher
                )

            self.assertEqual(
                tracker.periods["cumulative"][("Codex", "gpt-test")], 30
            )
            self.assertEqual(
                tracker.call_periods["cumulative"][("Codex", "gpt-test")], 2
            )

    def test_codex_fork_rewritten_history_is_seeded_without_counting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            session_dir = home / ".codex" / "sessions" / "2026" / "07" / "14"
            session_dir.mkdir(parents=True)
            now = datetime.now(timezone.utc)
            baseline_time = now - timedelta(hours=1)
            root_session = "root-session"
            original = session_dir / "rollout-2026-07-14T00-00-00-original.jsonl"
            forked = session_dir / "rollout-2026-07-14T01-00-00-forked.jsonl"
            original.write_text(
                self._codex_lines(
                    root_session,
                    10,
                    now - timedelta(hours=2),
                    cumulative=10,
                ),
                encoding="utf-8",
            )
            child_meta = json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "child-session",
                        "forked_from_id": root_session,
                    },
                }
            ) + "\n"
            forked.write_text(
                child_meta
                + self._codex_lines(
                    root_session,
                    10,
                    now,
                    cumulative=10,
                )
                + self._codex_lines(
                    root_session,
                    20,
                    now + timedelta(seconds=1),
                    cumulative=30,
                ),
                encoding="utf-8",
            )
            watcher = FakeWatcher()
            with patch.object(token_watcher.Path, "home", return_value=home):
                tracker = token_watcher.CodexTailTracker(
                    baseline_time, watcher=watcher
                )

            self.assertEqual(
                tracker.periods["cumulative"][("Codex", "gpt-test")], 20
            )
            self.assertEqual(
                tracker.call_periods["cumulative"][("Codex", "gpt-test")], 1
            )

    def test_codex_cache_skips_unchanged_jsonl_files_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            session_dir = home / ".codex" / "sessions" / "2026" / "07" / "14"
            session_dir.mkdir(parents=True)
            cache_path = home / ".tokenwatcher" / "codex_fingerprint_cache.json"
            now = datetime.now(timezone.utc)
            log_path = session_dir / "rollout-2026-07-14T00-00-00-cache.jsonl"
            log_path.write_text(
                self._codex_lines("cache-session", 10, now, cumulative=10),
                encoding="utf-8",
            )
            with patch.object(token_watcher.Path, "home", return_value=home):
                first = token_watcher.CodexTailTracker(
                    now - timedelta(days=1),
                    watcher=FakeWatcher(),
                    cache_path=cache_path,
                )
            first.close()
            self.assertTrue(cache_path.exists())

            original_open = token_watcher.Path.open

            def guarded_open(path: Path, *args, **kwargs):
                if path.suffix.lower() == ".jsonl":
                    raise AssertionError(f"unexpected JSONL read: {path}")
                return original_open(path, *args, **kwargs)

            with patch.object(token_watcher.Path, "home", return_value=home), patch.object(
                token_watcher.Path, "open", guarded_open
            ):
                second = token_watcher.CodexTailTracker(
                    now,
                    watcher=FakeWatcher(),
                    cache_path=cache_path,
                )
            self.assertEqual(second.states[log_path].offset, log_path.stat().st_size)
            second.close()

    def test_codex_cache_tails_only_bytes_appended_after_cached_offset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            session_dir = home / ".codex" / "sessions" / "2026" / "07" / "14"
            session_dir.mkdir(parents=True)
            cache_path = home / ".tokenwatcher" / "codex_fingerprint_cache.json"
            now = datetime.now(timezone.utc)
            log_path = session_dir / "rollout-2026-07-14T00-00-00-tail.jsonl"
            log_path.write_text(
                self._codex_lines("tail-session", 10, now, cumulative=10),
                encoding="utf-8",
            )
            with patch.object(token_watcher.Path, "home", return_value=home):
                first = token_watcher.CodexTailTracker(
                    now - timedelta(days=1),
                    watcher=FakeWatcher(),
                    cache_path=cache_path,
                )
            first.close()
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    self._codex_lines(
                        "tail-session",
                        20,
                        now + timedelta(seconds=1),
                        cumulative=30,
                    )
                )

            with patch.object(token_watcher.Path, "home", return_value=home):
                second = token_watcher.CodexTailTracker(
                    now,
                    watcher=FakeWatcher(),
                    cache_path=cache_path,
                )
            self.assertEqual(
                second.periods["cumulative"][("Codex", "gpt-test")], 20
            )
            self.assertEqual(
                second.call_periods["cumulative"][("Codex", "gpt-test")], 1
            )
            second.close()

    def test_claude_runtime_changes_do_not_rescan_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            project_dir = home / ".claude" / "projects" / "project"
            project_dir.mkdir(parents=True)
            now = datetime.now(timezone.utc)
            cold = project_dir / "cold.jsonl"
            cold.write_text(self._claude_line("cold", 1, now), encoding="utf-8")
            old = time.time() - 3600
            os.utime(cold, (old, old))
            watcher = FakeWatcher()
            with patch.object(token_watcher.Path, "home", return_value=home):
                tracker = token_watcher.ClaudeTailTracker(
                    now - timedelta(days=1), watcher=watcher
                )
            self.assertFalse(tracker.states[cold].watching)
            with patch.object(token_watcher.Path, "rglob", side_effect=AssertionError):
                tracker.poll()

            new_file = project_dir / "new.jsonl"
            new_file.touch()
            watcher.emit(new_file)
            tracker.poll()
            self.assertTrue(tracker.states[new_file].watching)
            new_file.write_text(
                self._claude_line("new", 20, now + timedelta(seconds=1)),
                encoding="utf-8",
            )
            watcher.emit(new_file)
            tracker.poll()
            self.assertEqual(
                tracker.periods["cumulative"][("Claude Code", "claude-test")],
                21,
            )

    def test_cline_only_stats_changed_task_files_after_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            history = root / "state" / "taskHistory.json"
            tasks_root = root / "tasks"
            history.parent.mkdir()
            tasks_root.mkdir()
            now = datetime.now(timezone.utc)
            ts = int(now.timestamp() * 1000)
            tasks = {}
            task_paths = []
            for index in range(5):
                task_id = f"task-{index}"
                path = tasks_root / task_id / "ui_messages.json"
                path.parent.mkdir()
                event = {
                    "type": "say",
                    "say": "api_req_started",
                    "text": json.dumps({"tokensIn": 1}),
                    "ts": ts,
                    "modelInfo": {"modelId": "cline-test"},
                }
                path.write_text(json.dumps([event]), encoding="utf-8")
                tasks[task_id] = ("cline-test", 2, now)
                task_paths.append(path)
            watcher = FakeWatcher()
            with patch.object(token_watcher, "CLINE_HISTORY", history), patch.object(
                token_watcher, "CLINE_TASKS", tasks_root
            ):
                counter = token_watcher.ClineRequestCounter(
                    now - timedelta(days=1), watcher=watcher
                )
                counter.poll(tasks)
                original_stat = token_watcher.Path.stat
                touched: list[Path] = []

                def counted_stat(path: Path, *args, **kwargs):
                    touched.append(path)
                    return original_stat(path, *args, **kwargs)

                with patch.object(token_watcher.Path, "stat", counted_stat):
                    counter.poll(tasks)
                self.assertEqual(touched, [])

                changed = task_paths[0]
                data = json.loads(changed.read_text(encoding="utf-8"))
                data.append({**data[0], "ts": ts + 1})
                changed.write_text(json.dumps(data), encoding="utf-8")
                watcher.emit(changed)
                touched.clear()
                with patch.object(token_watcher.Path, "stat", counted_stat):
                    counter.poll(tasks)
                self.assertEqual(touched, [changed])
                self.assertEqual(
                    counter.periods["cumulative"][("Cline", "cline-test")], 6
                )


if __name__ == "__main__":
    unittest.main()
