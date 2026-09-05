import unittest
import datetime as dt
from collections import Counter

from scripts.generate_profile import (
    ScanRegressionError,
    carried_daily_additions,
    merge_rolling_additions,
    partial_scan_start,
    repository_name_hash,
    validate_repository_inventory,
    validate_scan,
)


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
            "daily_additions": {"2026-09-04": 100},
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
            "daily_additions": {"2026-09-04": 100},
        }

        with self.assertRaisesRegex(ScanRegressionError, "no repository inventory"):
            validate_repository_inventory(59, previous, ["mileslow/repository"])

    def test_additive_partial_refresh_keeps_old_data_and_adds_new_lines(self):
        previous = {
            "end_date": "2026-09-04",
            "daily_additions": {
                "2026-09-03": 100,
                "2026-09-04": 200,
            },
        }
        merged = merge_rolling_additions(
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
            "daily_additions": {
                "2025-09-05": 50,
                "2025-09-06": 100,
                "2026-09-04": 200,
            },
        }
        carried = carried_daily_additions(
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
