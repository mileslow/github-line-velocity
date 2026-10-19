import datetime as dt
import unittest
from unittest.mock import Mock

from scripts.contributions import ContributionError, fetch_contributions


def response(commits=7, total=10, restricted=2, login="mileslow"):
    return {"data": {"user": {
        "login": login,
        "contributionsCollection": {
            "totalCommitContributions": commits,
            "restrictedContributionsCount": restricted,
            "contributionCalendar": {"totalContributions": total},
        },
    }}}


class ContributionTests(unittest.TestCase):
    def setUp(self):
        self.start = dt.date(2025, 10, 19)
        self.end = dt.date(2026, 10, 18)

    def fetch(self, value):
        return fetch_contributions("token", "mileslow", self.start, self.end,
                                   Mock(return_value=value))

    def test_reads_both_counts_from_one_collection_for_exact_utc_window(self):
        request = Mock(return_value=response(login="MilesLow"))
        result = fetch_contributions("token", "mileslow", self.start, self.end, request)
        self.assertEqual(result, {
            "start_date": "2025-10-19", "end_date": "2026-10-18",
            "commit_contributions": 7, "total_contributions": 10,
            "restricted_contributions_count": 2,
        })
        request.assert_called_once()
        self.assertEqual(request.call_args.args, ("token", "POST", "/graphql"))
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["variables"], {
            "username": "mileslow", "from": "2025-10-19T00:00:00Z",
            "to": "2026-10-18T23:59:59Z",
        })
        self.assertIn("totalCommitContributions", payload["query"])
        self.assertIn("totalContributions", payload["query"])
        self.assertIn("restrictedContributionsCount", payload["query"])

    def test_zero_counts_are_valid(self):
        result = self.fetch(response(commits=0, total=0, restricted=0))
        self.assertEqual(result["commit_contributions"], 0)
        self.assertEqual(result["total_contributions"], 0)
        self.assertEqual(result["restricted_contributions_count"], 0)

    def test_long_range_is_split_without_gaps_or_double_counted_dates(self):
        start, end = dt.date(2024, 1, 1), dt.date(2025, 1, 1)
        request = Mock(side_effect=[response(4, 6), response(2, 3)])
        result = fetch_contributions("token", "mileslow", start, end, request)
        self.assertEqual(result["commit_contributions"], 6)
        self.assertEqual(result["total_contributions"], 9)
        self.assertEqual(result["restricted_contributions_count"], 4)
        windows = [call.kwargs["payload"]["variables"] for call in request.call_args_list]
        self.assertEqual(windows, [
            {"username": "mileslow", "from": "2024-01-01T00:00:00Z",
             "to": "2024-12-30T23:59:59Z"},
            {"username": "mileslow", "from": "2024-12-31T00:00:00Z",
             "to": "2025-01-01T23:59:59Z"},
        ])

    def test_partial_graphql_error_is_rejected_even_with_valid_counts(self):
        value = response()
        value["errors"] = [{"message": "A requested field was unavailable"}]
        with self.assertRaisesRegex(ContributionError, "returned errors"):
            self.fetch(value)

    def test_missing_or_malformed_response_is_rejected(self):
        cases = [None, [], {}, {"data": None}, {"data": {"user": None}},
                 {"data": {"user": {"login": "mileslow"}}},
                 {"data": {"user": {"login": "mileslow", "contributionsCollection": {}}}}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ContributionError):
                self.fetch(value)

    def test_response_for_a_different_user_is_rejected(self):
        with self.assertRaisesRegex(ContributionError, "requested user"):
            self.fetch(response(login="someone-else"))

    def test_counts_are_exact_non_negative_integers(self):
        for value in (True, False, 7.0, "7", -1, None):
            for field in ("commits", "total", "restricted"):
                with self.subTest(field=field, value=value), self.assertRaises(ContributionError):
                    self.fetch(response(**{field: value}))

    def test_commit_count_cannot_exceed_total_or_be_clamped(self):
        with self.assertRaisesRegex(ContributionError, "exceed total"):
            self.fetch(response(commits=11, total=10))

    def test_restricted_count_cannot_exceed_total(self):
        with self.assertRaisesRegex(ContributionError, "restricted contributions exceed total"):
            self.fetch(response(restricted=11, total=10))

    def test_restricted_activity_is_preserved_without_inventing_commit_counts(self):
        result = self.fetch(response(commits=284, total=6476, restricted=6192))
        self.assertEqual(result["commit_contributions"], 284)
        self.assertEqual(result["total_contributions"], 6476)
        self.assertEqual(result["restricted_contributions_count"], 6192)

    def test_missing_restricted_count_is_rejected(self):
        value = response()
        del value["data"]["user"]["contributionsCollection"]["restrictedContributionsCount"]
        with self.assertRaisesRegex(ContributionError, "restrictedContributionsCount"):
            self.fetch(value)

    def test_later_window_failure_does_not_return_a_partial_total(self):
        request = Mock(side_effect=[response(), response(commits=11, total=10)])
        with self.assertRaises(ContributionError):
            fetch_contributions("token", "mileslow", dt.date(2024, 1, 1),
                                dt.date(2025, 1, 1), request)

    def test_transport_errors_propagate(self):
        request = Mock(side_effect=RuntimeError("request failed"))
        with self.assertRaisesRegex(RuntimeError, "request failed"):
            fetch_contributions("token", "mileslow", self.start, self.end, request)

    def test_invalid_range_fails_before_any_request(self):
        request = Mock()
        for start, end in ((self.end, self.start), ("2025-10-19", self.end),
                           (dt.datetime(2025, 10, 19), self.end)):
            with self.subTest(start=start, end=end), self.assertRaises(ContributionError):
                fetch_contributions("token", "mileslow", start, end, request)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
