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
