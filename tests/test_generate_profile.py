import unittest
import datetime as dt
import json
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from scripts.generate_profile import (
    ScanRegressionError,
    carried_daily_changed,
    commit_detail_stats,
    load_model_usage,
    merge_rolling_changed,
    partial_scan_start,
    render_svg,
    repository_name_hash,
    validate_repository_inventory,
    validate_scan,
)


class GraphRenderingTests(unittest.TestCase):
    def test_graph_buckets_reconcile_to_total_and_use_linear_scale(self):
        daily = Counter(
            {
                "2026-09-01": 10,
                "2026-09-02": 5,
                "2026-09-03": 20,
            }
        )

        svg = render_svg(
            dt.date(2026, 9, 1),
            dt.date(2026, 9, 3),
            daily,
            Counter({"Python": 35}),
            3,
            [],
            0,
            500,
        )

        self.assertIn("35 lines / 3 days", svg)
        self.assertIn("500 Claude Code", svg)
        self.assertIn("2026-09-01 to 2026-09-02: 15 lines", svg)
        self.assertIn("2026-09-03: 20 lines", svg)
        self.assertIn('x1="54.00" y1="284.0" x2="54.00" y2="224.0"', svg)
        self.assertIn('x1="964.00" y1="284.0" x2="964.00" y2="204.0"', svg)
        self.assertNotIn("additions", svg)
        self.assertNotIn("lines changed", svg)


class ModelUsageTests(unittest.TestCase):
    def test_loads_claude_code_spend_alongside_reconciled_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model_usage.json"
            path.write_text(
                json.dumps(
                    {
                        "total_tokens": 100,
                        "claude_code_spend_usd": 500,
                        "models": [{"name": "Claude", "tokens": 100}],
                    }
                ),
                encoding="utf-8",
            )

            segments, total_tokens, spend_usd = load_model_usage(path)

        self.assertEqual(total_tokens, 100)
        self.assertEqual(spend_usd, 500)
        self.assertEqual(segments[0][0], "Claude")


class CommitDetailStatsTests(unittest.TestCase):
    @patch("scripts.generate_profile.github_request")
    def test_counts_additions_and_deletions_as_changed_lines(self, request):
        request.return_value = {
            "files": [
                {"filename": "src/app.py", "additions": 3, "deletions": 4},
                {"filename": "README.md", "additions": 100, "deletions": 100},
                {"filename": "src/removed.py", "additions": 0, "deletions": 2},
            ]
        }
        commit = {
            "sha": "abc123",
            "commit": {"author": {"date": "2026-09-05T12:00:00Z"}},
        }

        day, changed_by_language, truncated = commit_detail_stats(
            "token", "mileslow/example", commit
        )

        self.assertEqual(day, "2026-09-05")
        self.assertEqual(changed_by_language["Python"], 9)
        self.assertEqual(truncated, 0)


class ScanValidationTests(unittest.TestCase):
    def test_rejects_the_august_31_partial_repository_scan(self):
        previous = {
            "scan_mode": "authenticated",
            "repositories_scanned": 153,
            "active_days": 292,
        }
        current = {
            "scan_mode": "authenticated",
            "repositories_scanned": 59,
            "active_days": 93,
            "commit_detail_failures": 0,
        }

        with self.assertRaisesRegex(ScanRegressionError, "repositories dropped"):
            validate_scan(current, previous)

    def test_rejects_incomplete_repository_access_before_detail_scanning(self):
        previous = {
            "scan_mode": "authenticated",
            "repositories_scanned": 153,
        }

        with self.assertRaisesRegex(ScanRegressionError, "repositories dropped"):
            validate_repository_inventory(59, previous)

    def test_remembers_a_missing_known_repository_for_carry_forward(self):
        previous = {
            "daily_lines_changed": {"2026-09-04": 100},
            "coverage_baseline": {
                "repositories_scanned": 2,
                "repository_hashes": [
                    repository_name_hash("Vastly-Podcasts/Overlap"),
                    repository_name_hash("mileslow/another-repository"),
                ],
            }
        }

        missing = validate_repository_inventory(
            2,
            previous,
            ["mileslow/another-repository", "mileslow/new-repository"],
        )
        self.assertEqual(missing, {repository_name_hash("Vastly-Podcasts/Overlap")})

    def test_blocks_an_old_snapshot_without_inventory_when_coverage_drops(self):
        previous = {
            "repositories_scanned": 153,
            "daily_lines_changed": {"2026-09-04": 100},
        }

        with self.assertRaisesRegex(ScanRegressionError, "no repository inventory"):
            validate_repository_inventory(59, previous, ["mileslow/repository"])

    def test_allows_legacy_additions_to_backfill_a_permanently_missing_repository(self):
        previous = {
            "coverage_baseline": {
                "repositories_scanned": 2,
                "repository_hashes": [
                    repository_name_hash("Vastly-Podcasts/Overlap"),
                    repository_name_hash("mileslow/another-repository"),
                ],
            },
            "daily_additions": {"2026-09-04": 100},
        }

        missing = validate_repository_inventory(
            1,
            previous,
            ["mileslow/another-repository"],
        )
        self.assertEqual(missing, {repository_name_hash("Vastly-Podcasts/Overlap")})

    def test_legacy_additions_are_only_used_when_explicitly_backfilling(self):
        previous = {
            "end_date": "2026-09-04",
            "daily_additions": {"2026-09-04": 100},
        }

        merged = merge_rolling_changed(
            previous,
            Counter({"2026-09-05": 300}),
            dt.date(2025, 9, 6),
            dt.date(2026, 9, 5),
            allow_legacy_backfill=True,
        )

        self.assertEqual(sum(merged.values()), 400)
        self.assertEqual(merged["2026-09-04"], 100)

    def test_additive_partial_refresh_keeps_old_data_and_adds_changed_lines(self):
        previous = {
            "end_date": "2026-09-04",
            "daily_lines_changed": {
                "2026-09-03": 100,
                "2026-09-04": 200,
            },
        }
        merged = merge_rolling_changed(
            previous,
            Counter({"2026-09-05": 300_000}),
            dt.date(2025, 9, 6),
            dt.date(2026, 9, 5),
        )
        self.assertEqual(sum(merged.values()), 300_300)
        self.assertEqual(merged["2026-09-04"], 200)
        self.assertEqual(merged["2026-09-05"], 300_000)

    def test_partial_refresh_trims_only_days_that_left_the_window(self):
        previous = {
            "end_date": "2026-09-04",
            "daily_lines_changed": {
                "2025-09-05": 50,
                "2025-09-06": 100,
                "2026-09-04": 200,
            },
        }
        carried = carried_daily_changed(
            previous,
            dt.date(2025, 9, 6),
            dt.date(2026, 9, 5),
        )
        self.assertNotIn("2025-09-05", carried)
        self.assertEqual(carried["2025-09-06"], 100)
        self.assertEqual(carried["2026-09-04"], 200)

    def test_partial_refresh_starts_after_the_previous_snapshot(self):
        previous = {"end_date": "2026-09-04"}
        self.assertEqual(
            partial_scan_start(previous, dt.date(2025, 9, 6), dt.date(2026, 9, 5)),
            dt.date(2026, 9, 5),
        )

    def test_validation_allows_a_partial_snapshot_with_carried_forward_coverage(self):
        previous = {
            "scan_mode": "authenticated",
            "repositories_scanned": 153,
            "active_days": 292,
        }
        current = {
            "scan_mode": "authenticated-partial",
            "repositories_scanned": 153,
            "repositories_accessible": 59,
            "repositories_carried_forward": 94,
            "active_days": 293,
            "commit_detail_failures": 0,
        }

        validate_scan(current, previous)

    def test_rejects_commit_detail_failures_even_without_a_baseline(self):
        current = {"commit_detail_failures": 1}

        with self.assertRaisesRegex(ScanRegressionError, "refusing to publish"):
            validate_scan(current, None)

    def test_rejects_repeating_a_bad_scan_after_the_bad_scan_was_saved(self):
        previous = {
            "scan_mode": "authenticated",
            "repositories_scanned": 59,
            "active_days": 93,
            "coverage_baseline": {"repositories_scanned": 153},
        }
        current = {
            "scan_mode": "authenticated",
            "repositories_scanned": 59,
            "active_days": 93,
            "commit_detail_failures": 0,
        }

        with self.assertRaisesRegex(ScanRegressionError, "repositories dropped"):
            validate_scan(current, previous)

    def test_allows_a_normal_rolling_window_refresh(self):
        previous = {
            "scan_mode": "authenticated",
            "repositories_scanned": 153,
            "active_days": 292,
        }
        current = {
            "scan_mode": "authenticated",
            "repositories_scanned": 153,
            "active_days": 291,
            "commit_detail_failures": 0,
        }

        validate_scan(current, previous)

    def test_rejects_a_sudden_active_day_drop_without_a_repository_drop(self):
        previous = {
            "scan_mode": "authenticated",
            "repositories_scanned": 153,
            "active_days": 292,
        }
        current = {
            "scan_mode": "authenticated",
            "repositories_scanned": 153,
            "active_days": 93,
            "commit_detail_failures": 0,
        }

        with self.assertRaisesRegex(ScanRegressionError, "active days dropped"):
            validate_scan(current, previous)

    def test_rejects_an_authenticated_to_public_fallback_downgrade(self):
        previous = {"scan_mode": "authenticated"}
        current = {
            "scan_mode": "public-fallback",
            "commit_detail_failures": 0,
        }

        with self.assertRaisesRegex(ScanRegressionError, "scan downgraded"):
            validate_scan(current, previous)


if __name__ == "__main__":
    unittest.main()
