import unittest

from scripts.generate_profile import ScanRegressionError, validate_scan


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
