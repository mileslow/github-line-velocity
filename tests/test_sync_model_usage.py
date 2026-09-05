import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.sync_model_usage import (
    claude_usage_events,
    codex_usage_events,
    main,
    snapshot_after,
)


def write_json_lines(path: Path, rows: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) if not isinstance(row, str) else row for row in rows)
        + "\n",
        encoding="utf-8",
    )


class LocalUsageParsingTests(unittest.TestCase):
    def test_claude_events_dedupe_streaming_rows_and_skip_malformed_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project" / "session.jsonl"
            write_json_lines(
                path,
                [
                    {
                        "timestamp": "2026-09-05T09:00:00Z",
                        "requestId": "request-1",
                        "message": {
                            "id": "message-1",
                            "model": "claude-opus-5",
                            "usage": {"input_tokens": 2, "output_tokens": 3},
                        },
                    },
                    "{not valid json",
                    {
                        "timestamp": "2026-09-05T09:00:01Z",
                        "requestId": "request-1",
                        "message": {
                            "id": "message-1",
                            "model": "claude-opus-5",
                            "stop_reason": "end_turn",
                            "usage": {
                                "input_tokens": 10,
                                "cache_creation_input_tokens": 20,
                                "cache_read_input_tokens": 30,
                                "output_tokens": 40,
                            },
                        },
                    },
                    {
                        "timestamp": "2026-09-05T09:00:02Z",
                        "requestId": "request-2",
                        "message": {
                            "id": "message-2",
                            "model": "claude-haiku-4-5-20251001",
                            "stop_reason": "tool_use",
                            "usage": {"total_tokens": 7},
                        },
                    },
                    {
                        "timestamp": "2026-09-05T09:00:03Z",
                        "requestId": "request-3",
                        "message": {
                            "id": "message-3",
                            "model": "claude-opus-5",
                            "usage": {"total_tokens": 999},
                        },
                    },
                    {
                        "timestamp": "2026-09-05T09:00:04Z",
                        "requestId": "request-4",
                        "message": {
                            "id": "message-4",
                            "model": "<synthetic>",
                            "stop_reason": "end_turn",
                            "usage": {"total_tokens": 0},
                        },
                    },
                ],
            )

            events = list(
                claude_usage_events(
                    Path(directory), dt.datetime(2026, 9, 5, 8, tzinfo=dt.timezone.utc)
                )
            )

        self.assertEqual(
            [(model, tokens) for model, _, tokens in events],
            [("claude-opus-5", 100), ("claude-haiku-4-5-20251001", 7)],
        )

    def test_claude_events_prefer_latest_cost_state_and_fallback_to_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            start_time = int(
                dt.datetime(2026, 9, 5, 9, tzinfo=dt.timezone.utc).timestamp() * 1000
            )
            write_json_lines(
                root / "project" / "session.jsonl",
                [
                    {
                        "timestamp": "2026-09-05T09:00:00Z",
                        "sessionId": "costed-session",
                        "message": {
                            "id": "ignored-message",
                            "model": "claude-opus-5",
                            "stop_reason": "end_turn",
                            "usage": {"total_tokens": 5},
                        },
                    },
                    {
                        "type": "cost-state",
                        "sessionId": "costed-session",
                        "startTime": start_time,
                        "totalDuration": 1_000,
                        "modelUsage": {
                            "claude-opus-5": {
                                "inputTokens": 1,
                                "cacheCreationInputTokens": 2,
                                "cacheReadInputTokens": 3,
                                "outputTokens": 4,
                                "thinkingTokens": 5,
                            }
                        },
                    },
                    {
                        "type": "cost-state",
                        "sessionId": "costed-session",
                        "startTime": start_time,
                        "totalDuration": 2_000,
                        "modelUsage": {
                            "claude-opus-5": {
                                "inputTokens": 10,
                                "cacheCreationInputTokens": 20,
                                "cacheReadInputTokens": 30,
                                "outputTokens": 40,
                                "thinkingTokens": 50,
                            }
                        },
                    },
                    {
                        "timestamp": "2026-09-05T09:01:00Z",
                        "sessionId": "message-only-session",
                        "message": {
                            "id": "fallback-message",
                            "model": "claude-haiku-4-5-20251001",
                            "stop_reason": "end_turn",
                            "usage": {"total_tokens": 7},
                        },
                    },
                ],
            )

            events = list(claude_usage_events(root))

        self.assertEqual(
            [(model, tokens) for model, _, tokens in events],
            [("claude-opus-5", 150), ("claude-haiku-4-5-20251001", 7)],
        )

    def test_codex_events_scan_active_and_archived_style_subdirectories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions" / "rollout.jsonl"
            write_json_lines(
                path,
                [
                    {
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.6-sol"},
                    },
                    {
                        "timestamp": "2026-09-05T10:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 4,
                                    "cached_input_tokens": 5,
                                    "output_tokens": 6,
                                    "total_tokens": 15,
                                }
                            },
                        },
                    },
                    {
                        "timestamp": "2026-09-05T10:01:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {"last_token_usage": {"total_tokens": 0}},
                        },
                    },
                ],
            )

            events = list(
                codex_usage_events(
                    Path(directory), dt.datetime(2026, 9, 5, 8, tzinfo=dt.timezone.utc)
                )
            )

        self.assertEqual([(model, tokens) for model, _, tokens in events], [("gpt-5.6-sol", 15)])


class LocalUsageSyncTests(unittest.TestCase):
    def test_main_updates_snapshot_and_second_run_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "data" / "model_usage.json"
            claude_root = root / "claude"
            codex_root = root / "codex"
            snapshot_path.parent.mkdir()
            snapshot_path.write_text(
                json.dumps(
                    {
                        "window_days": 365,
                        "start_date": "2026-09-04",
                        "end_date": "2026-09-04",
                        "total_tokens": 10,
                        "unallocated_token_baseline": 10,
                        "models": [],
                        "usage_sync": {
                            "last_processed_at": "2026-09-04T23:59:59Z"
                        },
                    }
                ),
                encoding="utf-8",
            )
            write_json_lines(
                claude_root / "session.jsonl",
                [
                    {
                        "timestamp": "2026-09-05T09:00:00Z",
                        "message": {
                            "id": "claude-message",
                            "model": "claude-opus-5",
                            "stop_reason": "end_turn",
                            "usage": {"total_tokens": 20},
                        },
                    }
                ],
            )
            write_json_lines(
                codex_root / "session.jsonl",
                [
                    {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
                    {
                        "timestamp": "2026-09-05T09:01:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {"last_token_usage": {"total_tokens": 30}},
                        },
                    },
                ],
            )

            argv = [
                "sync_model_usage.py",
                "--snapshot",
                str(snapshot_path),
                "--claude-root",
                str(claude_root),
                "--codex-root",
                str(codex_root),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(main(), 0)
            updated = json.loads(snapshot_path.read_text(encoding="utf-8"))

            self.assertEqual(updated["total_tokens"], 60)
            self.assertEqual(updated["end_date"], "2026-09-05")
            self.assertEqual(updated["usage_sync"]["last_processed_at"], "2026-09-05T09:01:00Z")
            self.assertEqual(
                updated["usage_sync"]["source_watermarks"],
                {
                    "claude_code": "2026-09-05T09:00:00Z",
                    "codex": "2026-09-05T09:01:00Z",
                },
            )
            self.assertEqual(
                {
                    source["name"]: source["total_tokens"]
                    for source in updated["sources"]
                },
                {
                    "local Claude Code token records": 20,
                    "local Codex session token records": 30,
                },
            )

            with patch.object(sys, "argv", argv):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads(snapshot_path.read_text(encoding="utf-8")), updated)
            self.assertEqual(snapshot_after(updated), dt.datetime(2026, 9, 5, 9, 1, tzinfo=dt.timezone.utc))

    def test_main_preserves_unallocated_token_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "data" / "model_usage.json"
            codex_root = root / "codex"
            snapshot_path.parent.mkdir()
            snapshot_path.write_text(
                json.dumps(
                    {
                        "window_days": 365,
                        "start_date": "2026-09-04",
                        "end_date": "2026-09-04",
                        "total_tokens": 100,
                        "unallocated_token_baseline": 100,
                        "models": [],
                        "usage_sync": {
                            "last_processed_at": "2026-09-04T23:59:59Z"
                        },
                    }
                ),
                encoding="utf-8",
            )
            write_json_lines(
                codex_root / "session.jsonl",
                [
                    {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
                    {
                        "timestamp": "2026-09-05T09:01:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {"last_token_usage": {"total_tokens": 30}},
                        },
                    },
                ],
            )

            argv = [
                "sync_model_usage.py",
                "--snapshot",
                str(snapshot_path),
                "--claude-root",
                str(root / "claude"),
                "--codex-root",
                str(codex_root),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(main(), 0)
            updated = json.loads(snapshot_path.read_text(encoding="utf-8"))

        self.assertEqual(updated["unallocated_token_baseline"], 100)
        self.assertEqual(sum(model["tokens"] for model in updated["models"]), 30)
        self.assertEqual(updated["total_tokens"], 130)

    def test_main_combines_multiple_claude_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "data" / "model_usage.json"
            first_claude_root = root / "claude-primary"
            second_claude_root = root / "claude-desktop"
            snapshot_path.parent.mkdir()
            snapshot_path.write_text(
                json.dumps(
                    {
                        "window_days": 365,
                        "start_date": "2026-09-04",
                        "end_date": "2026-09-04",
                        "total_tokens": 10,
                        "unallocated_token_baseline": 10,
                        "models": [],
                        "usage_sync": {
                            "last_processed_at": "2026-09-04T23:59:59Z"
                        },
                    }
                ),
                encoding="utf-8",
            )
            write_json_lines(
                first_claude_root / "session.jsonl",
                [
                    {
                        "timestamp": "2026-09-05T09:00:00Z",
                        "sessionId": "first-session",
                        "message": {
                            "id": "first-message",
                            "model": "claude-opus-5",
                            "stop_reason": "end_turn",
                            "usage": {"total_tokens": 20},
                        },
                    }
                ],
            )
            write_json_lines(
                second_claude_root / "session.jsonl",
                [
                    {
                        "timestamp": "2026-09-05T09:01:00Z",
                        "sessionId": "second-session",
                        "message": {
                            "id": "second-message",
                            "model": "claude-sonnet-5",
                            "stop_reason": "end_turn",
                            "usage": {"total_tokens": 30},
                        },
                    }
                ],
            )

            argv = [
                "sync_model_usage.py",
                "--snapshot",
                str(snapshot_path),
                "--claude-root",
                str(first_claude_root),
                "--claude-root",
                str(second_claude_root),
                "--codex-root",
                str(root / "missing-codex"),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(main(), 0)
            updated = json.loads(snapshot_path.read_text(encoding="utf-8"))

        self.assertEqual(updated["total_tokens"], 60)
        self.assertEqual(
            {
                model["name"]: model["tokens"]
                for model in updated["models"]
            },
            {
                "claude-opus-5": 20,
                "claude-sonnet-5": 30,
            },
        )
        self.assertEqual(
            next(
                source
                for source in updated["sources"]
                if source["name"] == "local Claude Code token records"
            )["session_files"],
            2,
        )

    def test_main_full_rescan_counts_records_before_a_bad_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "data" / "model_usage.json"
            codex_root = root / "codex"
            snapshot_path.parent.mkdir()
            snapshot_path.write_text(
                json.dumps(
                    {
                        "window_days": 365,
                        "start_date": "2026-09-04",
                        "end_date": "2026-09-05",
                        "total_tokens": 10,
                        "unallocated_token_baseline": 10,
                        "models": [],
                        "usage_sync": {
                            "last_processed_at": "2026-09-05T23:59:59Z",
                            "source_watermarks": {
                                "codex": "2026-09-05T23:59:59Z",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            write_json_lines(
                codex_root / "session.jsonl",
                [
                    {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
                    {
                        "timestamp": "2026-09-05T09:01:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {"last_token_usage": {"total_tokens": 30}},
                        },
                    },
                ],
            )

            argv = [
                "sync_model_usage.py",
                "--snapshot",
                str(snapshot_path),
                "--claude-root",
                str(root / "claude"),
                "--codex-root",
                str(codex_root),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(main(), 0)
            updated = json.loads(snapshot_path.read_text(encoding="utf-8"))

        self.assertEqual(updated["total_tokens"], 40)
        self.assertEqual(updated["usage_sync"]["sync_mode"], "full_local_rescan")
        self.assertEqual(updated["usage_sync"]["source_watermarks"]["codex"], "2026-09-05T09:01:00Z")

    def test_main_keeps_snapshot_when_local_records_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "data" / "model_usage.json"
            snapshot_path.parent.mkdir()
            original = {
                "window_days": 365,
                "start_date": "2026-09-04",
                "end_date": "2026-09-05",
                "total_tokens": 110,
                "unallocated_token_baseline": 100,
                "models": [{"name": "Existing model", "tokens": 10}],
                "usage_sync": {
                    "last_processed_at": "2026-09-05T23:59:59Z",
                },
            }
            snapshot_path.write_text(json.dumps(original), encoding="utf-8")

            argv = [
                "sync_model_usage.py",
                "--snapshot",
                str(snapshot_path),
                "--claude-root",
                str(root / "missing-claude"),
                "--codex-root",
                str(root / "missing-codex"),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(main(), 0)

            self.assertEqual(json.loads(snapshot_path.read_text(encoding="utf-8")), original)


if __name__ == "__main__":
    unittest.main()
