"""Read profile contribution counts for an explicit UTC date range.

Repository commit scans have different eligibility rules from GitHub's profile
calendar. Use GitHub's contribution collection for both public headline counts
so the commit count and total contribution count describe the same activity.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable


CONTRIBUTIONS_QUERY = """
query ProfileContributions($username: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $username) {
    login
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
      }
    }
  }
}
""".strip()


class ContributionError(ValueError):
    """The API did not return a complete, internally consistent collection."""


def _count(value: Any, field: str) -> int:
    # Do not coerce booleans, floats or numeric strings into plausible totals.
    if type(value) is not int or value < 0:
        raise ContributionError(f"GitHub contribution field {field} must be a non-negative integer")
    return value


def _collection_counts(response: Any, username: str) -> tuple[int, int, int]:
    if not isinstance(response, dict):
        raise ContributionError("GitHub contribution response must be an object")
    # GraphQL can return partial data alongside errors. Partial results must
    # never replace a complete published snapshot.
    if "errors" in response and response["errors"] != []:
        raise ContributionError("GitHub contribution query returned errors")
    data = response.get("data")
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        raise ContributionError("GitHub contribution response is missing the requested user")
    login = user.get("login")
    if not isinstance(login, str) or login.casefold() != username.casefold():
        raise ContributionError("GitHub contribution response does not match the requested user")
    collection = user.get("contributionsCollection")
    if not isinstance(collection, dict):
        raise ContributionError("GitHub contribution response is missing the collection")
    calendar = collection.get("contributionCalendar")
    if not isinstance(calendar, dict):
        raise ContributionError("GitHub contribution response is missing the calendar")
    commits = _count(collection.get("totalCommitContributions"), "totalCommitContributions")
    total = _count(calendar.get("totalContributions"), "totalContributions")
    restricted = _count(collection.get("restrictedContributionsCount"), "restrictedContributionsCount")
    if commits > total:
        raise ContributionError("GitHub commit contributions exceed total contributions")
    if restricted > total:
        raise ContributionError("GitHub restricted contributions exceed total contributions")
    return commits, total, restricted


def fetch_contributions(
    token: str,
    username: str,
    start: dt.date,
    end: dt.date,
    request: Callable[..., Any],
) -> dict[str, Any]:
    """Return profile counts, inclusive of both dates, using a request callback.

    The callback accepts the same arguments as ``github_request``. Transport
    errors propagate to the caller. Longer ranges are split into disjoint
    windows of at most 365 days to respect the collection's one-year limit.
    Only dates and aggregate counts are returned; no repository data is read.
    """
    if type(start) is not dt.date or type(end) is not dt.date or start > end:
        raise ContributionError("Contribution range must contain ordered UTC dates")
    if not isinstance(username, str) or not username or username != username.strip():
        raise ContributionError("Contribution username must be a non-empty login")

    commits = total = restricted = 0
    window_start = start
    while window_start <= end:
        window_end = min(window_start + dt.timedelta(days=364), end)
        response = request(
            token,
            "POST",
            "/graphql",
            payload={
                "query": CONTRIBUTIONS_QUERY,
                "variables": {
                    "username": username,
                    "from": f"{window_start.isoformat()}T00:00:00Z",
                    "to": f"{window_end.isoformat()}T23:59:59Z",
                },
            },
        )
        window_commits, window_total, window_restricted = _collection_counts(response, username)
        commits += window_commits
        total += window_total
        restricted += window_restricted
        if window_end == end:
            break
        window_start = window_end + dt.timedelta(days=1)

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "commit_contributions": commits,
        "total_contributions": total,
        "restricted_contributions_count": restricted,
    }
