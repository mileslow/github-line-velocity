"""Durable, anonymous daily aggregates and a disposable commit-stat cache.

Only repository fingerprints, dates, languages and counts are persisted. No
repository names, commit messages, file paths or source code are stored.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Callable


def collect_history(
    commits: list[dict],
    repo_name: str,
    token: str,
    start: dt.date,
    end: dt.date,
    workers: int,
    cache_dir: Path,
    detail: Callable,
    write_json: Callable,
    previous_history: dict | None = None,
) -> dict:
    def read_commit(commit: dict):
        sha = commit.get("sha")
        if not isinstance(sha, str) or not sha:
            raise ValueError("Commit is missing its SHA")
        key = hashlib.sha256(f"{repo_name.casefold()}:{sha}".encode()).hexdigest()
        path = cache_dir / f"{key}.json"
        try:
            cached = json.loads(path.read_text())
            day, languages, truncated = cached
            dt.date.fromisoformat(day)
            if not isinstance(languages, dict) or any(
                not isinstance(v, int) or v < 0 for v in languages.values()
            ) or truncated not in (0, 1):
                raise ValueError("Invalid cached counts")
            return day, Counter(languages), truncated
        except (OSError, ValueError, TypeError):
            result = detail(token, repo_name, commit)
            write_json(path, result)
            return result

    days: dict[str, dict] = {}
    # Deduplicate API pagination overlaps. A commit contributes once per repo.
    unique = {commit["sha"]: commit for commit in commits}
    by_day: dict[str, list[str]] = {}
    for sha, commit in unique.items():
        day = commit["commit"]["author"]["date"][:10]
        if start <= dt.date.fromisoformat(day) <= end:
            by_day.setdefault(day, []).append(sha)
    fingerprints = {day: hashlib.sha256("\n".join(sorted(shas)).encode()).hexdigest()
                    for day, shas in by_day.items()}
    previous_days = (previous_history or {}).get("days", {})
    pending_commits = []
    for day, shas in by_day.items():
        old = previous_days.get(day, {})
        if old.get("commit_fingerprint") == fingerprints[day]:
            # Durable aggregate reuse survives an evicted Actions cache. A
            # changed or rewritten commit set invalidates this entire day.
            days[day] = dict(old)
        else:
            pending_commits.extend(unique[sha] for sha in shas)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(read_commit, commit) for commit in pending_commits]
        for future in as_completed(futures):
            try:
                day, languages, truncated = future.result()
            except Exception:
                for pending in futures:
                    pending.cancel()
                raise
            if not start <= dt.date.fromisoformat(day) <= end:
                continue
            row = days.setdefault(day, {"languages": {}, "commits": 0, "truncated": 0})
            totals = Counter(row["languages"])
            totals.update(languages)
            row["languages"] = dict(totals)
            row["commits"] += 1
            row["truncated"] += truncated
    for day, row in days.items():
        row["commit_fingerprint"] = fingerprints[day]
    return {"days": dict(sorted(days.items()))}


def trim_history(history: dict, start: dt.date, end: dt.date) -> dict:
    return {"days": {
        day: row for day, row in history["days"].items()
        if start <= dt.date.fromisoformat(day) <= end
    }}


def summarize(histories: list[dict]) -> dict:
    daily: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    commits = truncated = 0
    for history in histories:
        for day, row in history["days"].items():
            daily[day] += sum(row["languages"].values())
            languages.update(row["languages"])
            commits += row["commits"]
            truncated += row["truncated"]
    return {
        "daily_lines_changed": dict(daily), "languages": dict(languages),
        "authored_commits": commits, "commits_with_truncated_file_lists": truncated,
    }


def subtract_legacy(legacy: dict, observed: dict) -> dict:
    """Remove recoverable history from the old aggregate without counting twice.

    Older snapshots have no repository provenance. The non-negative difference
    is a conservative legacy remainder, not an invented per-repository split.
    """
    result = {}
    for key in ("daily_lines_changed", "languages"):
        result[key] = dict(Counter(legacy.get(key, {})) - Counter(observed.get(key, {})))
    for key in ("authored_commits", "commits_with_truncated_file_lists"):
        result[key] = max(0, legacy.get(key, 0) - observed.get(key, 0))
    return result


def reconcile_history(
    previous: dict | None,
    scanned: dict[str, dict],
    start: dt.date,
    end: dt.date,
) -> tuple[dict, dict, dict]:
    previous = previous or {}
    old_repositories = previous.get("repository_snapshots", {})
    repositories = {
        key: trim_history(history, start, end)
        for key, history in old_repositories.items()
    }
    if "legacy_carry" in previous:
        legacy = previous["legacy_carry"]
        baseline = set(previous.get("coverage_baseline", {}).get("repository_hashes", []))
        recovered = [history for key, history in scanned.items()
                     if key in baseline and key not in old_repositories]
        legacy = subtract_legacy(legacy, summarize(recovered))
    else:
        legacy = {
            "daily_lines_changed": previous.get("daily_lines_changed", previous.get("daily_additions", {})),
            "languages": previous.get("languages", {}),
            "authored_commits": previous.get("authored_commits", 0),
            "commits_with_truncated_file_lists": previous.get("commits_with_truncated_file_lists", 0),
        }
        legacy = subtract_legacy(legacy, summarize(list(scanned.values())))
    legacy["daily_lines_changed"] = {
        day: count for day, count in legacy["daily_lines_changed"].items()
        if start <= dt.date.fromisoformat(day) <= end
    }
    repositories.update(scanned)
    totals = summarize(list(repositories.values()))
    for key in ("daily_lines_changed", "languages"):
        combined = Counter(totals[key])
        combined.update(legacy[key])
        totals[key] = dict(combined)
    for key in ("authored_commits", "commits_with_truncated_file_lists"):
        totals[key] += legacy[key]
    return totals, repositories, legacy
