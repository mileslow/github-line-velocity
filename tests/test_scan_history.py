import argparse
from collections import Counter
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import generate_profile as generator
from scripts.scan_history import reconcile_history


def history(day, lines, commits=1):
    return {"days": {day: {"languages": {"Python": lines}, "commits": commits, "truncated": 0}}}


class HistoryRecoveryTests(unittest.TestCase):
    def test_repeated_day_replaces_counts_and_late_commits_are_added_once(self):
        start, end = dt.date(2026, 9, 1), dt.date(2026, 9, 8)
        totals, repos, legacy = reconcile_history(None, {"a": history("2026-09-08", 10)}, start, end)
        previous = {**totals, "repository_snapshots": repos, "legacy_carry": legacy}
        totals, repos, legacy = reconcile_history(previous, {"a": history("2026-09-08", 25, 2)}, start, end)
        self.assertEqual(totals["daily_lines_changed"], {"2026-09-08": 25})
        self.assertEqual(totals["authored_commits"], 2)
        again, _, _ = reconcile_history({**totals, "repository_snapshots": repos, "legacy_carry": legacy},
                                        {"a": history("2026-09-08", 25, 2)}, start, end)
        self.assertEqual(again, totals)

    def test_missing_repository_is_carried_and_returning_history_is_replaced(self):
        start, end = dt.date(2026, 9, 1), dt.date(2026, 9, 8)
        totals, repos, legacy = reconcile_history(None, {"a": history("2026-09-08", 10),
                                                        "b": history("2026-09-08", 20)}, start, end)
        previous = {**totals, "repository_snapshots": repos, "legacy_carry": legacy}
        totals, repos, legacy = reconcile_history(previous, {"a": history("2026-09-08", 15)}, start, end)
        self.assertEqual(totals["daily_lines_changed"]["2026-09-08"], 35)
        previous = {**totals, "repository_snapshots": repos, "legacy_carry": legacy}
        totals, _, _ = reconcile_history(previous, {"a": history("2026-09-08", 15),
                                                  "b": history("2026-09-08", 40)}, start, end)
        self.assertEqual(totals["daily_lines_changed"]["2026-09-08"], 55)

    def test_legacy_recovery_does_not_double_count_historical_repository(self):
        start, end = dt.date(2026, 9, 1), dt.date(2026, 9, 8)
        old = {"daily_lines_changed": {"2026-09-08": 100}, "languages": {"Python": 100},
               "authored_commits": 10}
        totals, repos, legacy = reconcile_history(old, {"a": history("2026-09-08", 40, 4)}, start, end)
        self.assertEqual(totals["daily_lines_changed"]["2026-09-08"], 100)
        previous = {**totals, "repository_snapshots": repos, "legacy_carry": legacy,
                    "coverage_baseline": {"repository_hashes": ["a", "b"]}}
        totals, _, legacy = reconcile_history(previous, {"a": history("2026-09-08", 40, 4),
                                                        "b": history("2026-09-08", 70, 7)}, start, end)
        self.assertEqual(totals["daily_lines_changed"]["2026-09-08"], 110)
        self.assertEqual(totals["authored_commits"], 11)
        self.assertEqual(legacy["daily_lines_changed"], {})

    def test_inaccessible_repository_counts_age_out_with_the_window(self):
        previous = {"repository_snapshots": {"a": history("2025-09-08", 40)},
                    "legacy_carry": {"daily_lines_changed": {}, "languages": {},
                                     "authored_commits": 0, "commits_with_truncated_file_lists": 0}}
        totals, _, _ = reconcile_history(previous, {}, dt.date(2025, 9, 9), dt.date(2026, 9, 8))
        self.assertEqual(totals["authored_commits"], 0)
        self.assertEqual(totals["daily_lines_changed"], {})


class GeneratorIntegrationTests(unittest.TestCase):
    def test_rerun_recovers_late_commits_uses_cache_and_failed_scan_preserves_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.json"
            model.write_text(json.dumps({"total_tokens": 100, "models": [{"name": "model", "tokens": 100}]}))
            args = argparse.Namespace(username="test", profile_repo="test/profile", generator_repo="test/generator",
                                      days=3, stats_path=str(root / "latest.json"), model_usage_path=str(model),
                                      organization=[], workers=2, cache_dir=root / "cache", output_dir=str(root))
            yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
            date = yesterday.isoformat()
            commits = [{"sha": "one", "commit": {"author": {"date": date + "T12:00:00Z"}}}]
            with patch.object(generator, "parse_args", return_value=args), \
                 patch.dict("os.environ", {"GH_TOKEN": "test"}), \
                 patch.object(generator, "list_repositories", return_value=[{"full_name": "test/source"}]), \
                 patch.object(generator, "list_authored_commits", side_effect=lambda *a: list(commits)) as listing, \
                 patch.object(generator, "commit_detail_stats", return_value=(date, Counter(Python=10), 0)) as detail:
                self.assertEqual(generator.main(), 0)
                self.assertEqual(json.loads((root / "latest.json").read_text())["end_date"], date)
                commits.append({"sha": "two", "commit": {"author": {"date": date + "T20:00:00Z"}}})
                self.assertEqual(generator.main(), 0)
                saved = (root / "latest.json").read_bytes()
                svg = (root / "github-line-velocity.svg").read_bytes()
                self.assertEqual(json.loads(saved)["daily_lines_changed"][date], 20)
                self.assertEqual(detail.call_count, 2)
                self.assertEqual(generator.main(), 0)
                self.assertEqual(detail.call_count, 2)
                for cached in args.cache_dir.glob("*.json"):
                    cached.unlink()
                self.assertEqual(generator.main(), 0)
                self.assertEqual(detail.call_count, 2)
                saved = (root / "latest.json").read_bytes()
                listing.side_effect = generator.ApiError("temporary server error", status=503)
                with self.assertRaisesRegex(SystemExit, "SCAN_BLOCKED"):
                    generator.main()
                self.assertEqual((root / "latest.json").read_bytes(), saved)
                self.assertEqual((root / "github-line-velocity.svg").read_bytes(), svg)

    @patch.object(generator, "github_request")
    def test_large_commit_reads_all_file_pages(self, request):
        file = {"filename": "app.py", "additions": 1, "deletions": 1}
        request.side_effect = [{"files": [file] * 100}, {"files": [file] * 100}, {"files": [file]}]
        day, languages, truncated = generator.commit_detail_stats(
            "token", "test/repo", {"sha": "abc", "commit": {"author": {"date": "2026-09-08T12:00:00Z"}}})
        self.assertEqual(languages["Python"], 402)
        self.assertEqual(truncated, 0)
        self.assertEqual(request.call_count, 3)

    @patch.object(generator, "paged_request")
    def test_empty_repository_is_valid_but_other_listing_errors_are_failures(self, request):
        request.side_effect = generator.ApiError("Git Repository is empty.", status=409)
        day = dt.date(2026, 9, 8)
        self.assertEqual(generator.list_authored_commits("token", "repo", "user", day, day), [])
        request.side_effect = generator.ApiError("Forbidden", status=403)
        with self.assertRaises(generator.ApiError):
            generator.list_authored_commits("token", "repo", "user", day, day)
