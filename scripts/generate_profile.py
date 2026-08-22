#!/usr/bin/env python3
"""Generate the aggregate GitHub line-velocity profile graphic."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import html
import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


API_ROOT = "https://api.github.com"
API_TIMEOUT_SECONDS = 20
API_MAX_RETRIES = 8
DEFAULT_PROFILE_REPO = "mileslow/mileslow"
DEFAULT_GENERATOR_REPO = "mileslow/github-line-velocity"
DEFAULT_PUBLIC_ORGANIZATIONS = ("Vastly-Podcasts",)

EXCLUDED_EXTENSIONS = {
    ".avif",
    ".bin",
    ".csv",
    ".db",
    ".doc",
    ".docx",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".json",
    ".jsonl",
    ".lock",
    ".map",
    ".md",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pt",
    ".svg",
    ".tar",
    ".tgz",
    ".tsv",
    ".txt",
    ".wav",
    ".webm",
    ".webp",
    ".xlsx",
    ".xml",
    ".zip",
}

EXCLUDED_FILENAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "yarn.lock",
}

EXCLUDED_PATH_PARTS = {
    ".git",
    ".next",
    ".turbo",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "generated",
    "node_modules",
    "output",
    "target",
    "tmp",
    "venv",
    ".venv",
}

LANGUAGE_BY_EXTENSION = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".dart": "Dart",
    ".go": "Go",
    ".h": "C/C++",
    ".hpp": "C++",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".lua": "Lua",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scss": "SCSS",
    ".sh": "Shell",
    ".sql": "SQL",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".vue": "Vue",
    ".yaml": "YAML",
    ".yml": "YAML",
}


class ApiError(RuntimeError):
    pass


def github_request(
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
) -> Any:
    url = f"{API_ROOT}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-line-velocity",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for attempt in range(API_MAX_RETRIES + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
                raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            retryable = error.code in {403, 429, 500, 502, 503, 504}
            remaining = error.headers.get("X-RateLimit-Remaining")
            if not retryable or attempt >= API_MAX_RETRIES or remaining == "0":
                raise ApiError(
                    f"GitHub API {method} {path} returned {error.code}: {detail[:400]}"
                ) from error
            retry_after = error.headers.get("Retry-After")
            delay = min(60.0, float(retry_after)) if retry_after else min(60.0, 2**attempt)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt >= API_MAX_RETRIES:
                raise ApiError(f"GitHub API {method} {path} failed: {error}") from error
            time.sleep(min(60.0, 2**attempt))
    raise AssertionError("unreachable")


def paged_request(token: str, path: str, query: dict[str, str]) -> list[Any]:
    page = 1
    items: list[Any] = []
    while True:
        page_query = dict(query)
        page_query["page"] = str(page)
        response = github_request(token, "GET", path, query=page_query)
        if not response:
            return items
        items.extend(response)
        if len(response) < int(query.get("per_page", "100")):
            return items
        page += 1


def filter_repositories(repos: list[dict[str, Any]], excluded: set[str]) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()
    for repo in repos:
        full_name = repo.get("full_name", "")
        if not full_name or full_name in seen or full_name in excluded:
            continue
        if repo.get("fork") or repo.get("archived") or not repo.get("default_branch"):
            continue
        seen.add(full_name)
        result.append(repo)
    return sorted(result, key=lambda repo: repo["full_name"].lower())


def list_repositories(token: str, excluded: set[str]) -> list[dict[str, Any]]:
    repos = paged_request(
        token,
        "/user/repos",
        {
            "affiliation": "owner,collaborator,organization_member",
            "per_page": "100",
            "sort": "updated",
        },
    )
    return filter_repositories(repos, excluded)


def list_public_repositories(
    token: str,
    username: str,
    excluded: set[str],
    organizations: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """List public owned and public organization repositories without /user/repos."""
    repos = paged_request(
        token,
        f"/users/{username}/repos",
        {"type": "owner", "per_page": "100", "sort": "updated"},
    )
    orgs = paged_request(token, f"/users/{username}/orgs", {"per_page": "100"})
    organization_names = {org.get("login") for org in orgs}
    organization_names.update(organizations)
    for login in sorted(name for name in organization_names if name):
        repos.extend(
            paged_request(
                token,
                f"/orgs/{login}/repos",
                {"type": "all", "per_page": "100", "sort": "updated"},
            )
        )
    return filter_repositories(repos, excluded)


def should_exclude(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = set(normalized.split("/"))
    if parts & EXCLUDED_PATH_PARTS:
        return True
    filename = Path(normalized).name.lower()
    if filename in EXCLUDED_FILENAMES:
        return True
    lower = normalized.lower()
    if lower.endswith(".min.js") or lower.endswith(".min.css"):
        return True
    return Path(filename).suffix in EXCLUDED_EXTENSIONS


def language_for(path: str) -> str:
    suffix = Path(path.lower()).suffix
    return LANGUAGE_BY_EXTENSION.get(suffix, "Other")


def list_authored_commits(
    token: str, repo_name: str, username: str, start: dt.date, end: dt.date
) -> list[dict[str, Any]]:
    return paged_request(
        token,
        f"/repos/{repo_name}/commits",
        {
            "author": username,
            "since": f"{start.isoformat()}T00:00:00Z",
            "until": f"{end.isoformat()}T23:59:59Z",
            "per_page": "100",
        },
    )


def commit_detail_stats(
    token: str, repo_name: str, commit: dict[str, Any]
) -> tuple[str, Counter[str], int]:
    details = github_request(token, "GET", f"/repos/{repo_name}/commits/{commit['sha']}")
    commit_date = (commit.get("commit", {}).get("author", {}).get("date") or "")[:10]
    additions_by_language: Counter[str] = Counter()
    files = details.get("files") or []
    for changed_file in files:
        path = changed_file.get("filename", "")
        added = int(changed_file.get("additions", 0) or 0)
        if added <= 0 or should_exclude(path):
            continue
        additions_by_language[language_for(path)] += added
    return commit_date, additions_by_language, int(len(files) >= 300)


def collect_repo_stats(
    repo: dict[str, Any],
    start: dt.date,
    end: dt.date,
    username: str,
    token: str,
    workers: int,
) -> tuple[Counter[str], Counter[str], int, int, int]:
    daily: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    truncated_file_lists = 0
    failed_commit_details = 0
    authored_commits = list_authored_commits(token, repo["full_name"], username, start, end)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [
            executor.submit(commit_detail_stats, token, repo["full_name"], commit)
            for commit in authored_commits
        ]
        for future in as_completed(futures):
            try:
                commit_date, commit_languages, truncated = future.result()
            except Exception:
                failed_commit_details += 1
                continue
            if not commit_date:
                continue
            if commit_languages:
                daily[commit_date] += sum(commit_languages.values())
                languages.update(commit_languages)
            truncated_file_lists += truncated
    return daily, languages, len(authored_commits), truncated_file_lists, failed_commit_details


def compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,}"


def render_svg(
    start: dt.date,
    end: dt.date,
    daily: Counter[str],
    languages: Counter[str],
    commits: int,
) -> str:
    total = sum(daily.values())
    active_days = sum(1 for value in daily.values() if value)
    days = [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]
    language_rows = sorted(languages.items(), key=lambda item: (-item[1], item[0]))
    named_languages = [(name, value) for name, value in language_rows if name != "Other"]
    top_languages = named_languages[:5]
    other = languages.get("Other", 0) + sum(value for _, value in named_languages[5:])
    if other:
        top_languages.append(("Other", other))
    language_max = max((value for _, value in top_languages), default=1)

    graph_points: list[tuple[dt.date, int]] = []
    for offset in range(0, len(days), 2):
        bucket = days[offset : offset + 2]
        graph_points.append((bucket[0], sum(daily[day.isoformat()] for day in bucket)))
    max_value = max((value for _, value in graph_points), default=0)
    log_max = math.log1p(max_value) if max_value else 1

    model_segments = [
        ("claude 4.5-sonnet-thinking", 28.5, "#111"),
        ("gpt 5.4-medium", 16.9, "#444"),
        ("opus 4-6", 10.8, "#666"),
        ("claude 4-sonnet-thinking", 10.4, "#888"),
        ("codex", 6.3, "#aaa"),
        ("other models", 27.0, "#ccc"),
    ]

    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    def lang_line(x: int, value_x: int, bar_x: int, index: int, name: str, value: int) -> str:
        y = 144 + index * 18
        bar_width = max(3, round(170 * value / language_max))
        return (
            f'<text x="{x}" y="{y}" class="small">{esc(name)}</text>'
            f'<text x="{value_x}" y="{y}" class="small">{compact_number(value)}</text>'
            f'<rect x="{bar_x}" y="{y - 9}" width="{bar_width}" height="8" fill="#111"/>'
        )

    lines = [
        '<svg width="1000" height="320" viewBox="0 0 1000 320" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc">',
        '<title id="title">Miles Low GitHub velocity, languages, and model split</title>',
        f'<desc id="desc">Last 365 days of code additions through {esc(end.isoformat())}, with a language summary and model usage donut chart.</desc>',
        "<style>",
        '  text { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; fill: #111; }',
        "  .title { font-size: 28px; font-weight: 700; }",
        "  .body { font-size: 14px; }",
        "  .small { font-size: 12px; }",
        "  .tiny { font-size: 11px; fill: #666; }",
        "  .axis { stroke: #111; stroke-width: 1; shape-rendering: crispEdges; }",
        "  .bar { stroke: #111; stroke-width: 2; opacity: 0.42; shape-rendering: crispEdges; }",
        "</style>",
        '<rect width="1000" height="320" fill="#fff"/>',
        f'<text x="54" y="52" class="title">{compact_number(total)} lines / 365 days</text>',
        f'<text x="54" y="84" class="body">{commits:,} contributions · {active_days} active days · 10.68B tokens</text>',
        '<text x="54" y="122" class="small">languages</text>',
    ]
    lines.extend(
        lang_line(54, 150, 198, index, name, value)
        for index, (name, value) in enumerate(top_languages[:3])
    )
    lines.extend(
        lang_line(384, 440, 480, index, name, value)
        for index, (name, value) in enumerate(top_languages[3:6])
    )
    lines.append('<text x="660" y="62" class="body">model breakdown</text>')

    offset = 0.0
    for name, percentage, color in model_segments:
        lines.append(
            f'<circle cx="718" cy="132" r="48" fill="none" stroke="{color}" stroke-width="22" '
            f'pathLength="100" stroke-dasharray="{percentage:.1f} {100 - percentage:.1f}" '
            f'stroke-dashoffset="-{offset:.1f}" transform="rotate(-90 718 132)"><title>{esc(name)}: {percentage:.1f}%</title></circle>'
        )
        offset += percentage
    model_labels = [
        ("claude 4.5-sonnet", 28.5),
        ("gpt 5.4-medium", 16.9),
        ("opus 4-6", 10.8),
        ("claude 4-sonnet", 10.4),
        ("codex", 6.3),
        ("other", 27.0),
    ]
    lines.extend(
        f'<text x="806" y="{84 + index * 20}" class="tiny">{esc(name)} {percentage:.1f}%</text>'
        for index, (name, percentage) in enumerate(model_labels)
    )

    x0, x1, baseline = 54.0, 964.0, 284.0
    step = (x1 - x0) / max(1, len(graph_points) - 1)
    points: list[str] = []
    for index, (day, value) in enumerate(graph_points):
        height = 0 if not value else 8 + 66 * math.log1p(value) / log_max
        x = x0 + index * step
        y = baseline - height
        lines.append(
            f'<line x1="{x:.2f}" y1="{baseline:.1f}" x2="{x:.2f}" y2="{y:.1f}" class="bar">'
            f'<title>{esc(day.isoformat())}: {value:,} additions</title></line>'
        )
        points.append(f"{x:.1f},{y:.1f}")
    lines.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="#111" stroke-width="1.4"/>')
    seen_months: set[tuple[int, int]] = set()
    for index, (day, _) in enumerate(graph_points):
        month = (day.year, day.month)
        if month in seen_months:
            continue
        seen_months.add(month)
        x = x0 + index * step
        lines.append(f'<text x="{x:.1f}" y="306" class="tiny">{esc(day.strftime("%b").lower())}</text>')
    lines.extend(
        ['<line x1="54" y1="284" x2="964" y2="284" class="axis"/>', "</svg>"]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="mileslow")
    parser.add_argument("--profile-repo", default=DEFAULT_PROFILE_REPO)
    parser.add_argument("--generator-repo", default=DEFAULT_GENERATOR_REPO)
    parser.add_argument(
        "--organization",
        action="append",
        default=list(DEFAULT_PUBLIC_ORGANIZATIONS),
        help="Organization to include in public-repository fallback (repeatable).",
    )
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output-dir", default="generated")
    parser.add_argument("--stats-path", default="data/latest.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")
    if args.days < 1:
        raise SystemExit("--days must be positive")

    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=args.days - 1)
    excluded = {args.profile_repo, args.generator_repo}
    public_token = os.environ.get("GITHUB_TOKEN") or token
    scan_mode = "authenticated"
    try:
        repos = list_repositories(token, excluded)
    except ApiError as error:
        if public_token == token:
            raise
        scan_mode = "public-fallback"
        print(f"Authenticated repository listing unavailable ({error}); falling back to public repositories.")
        repos = list_public_repositories(
            public_token, args.username, excluded, tuple(args.organization)
        )
    daily: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    commits = 0
    truncated_file_lists = 0
    failed_commit_details = 0
    skipped: list[str] = []
    print(f"Scanning {len(repos)} accessible non-fork repositories from {start} through {end}.")
    for index, repo in enumerate(repos, start=1):
        full_name = repo["full_name"]
        try:
            repo_token = public_token if not repo.get("private", False) else token
            repo_daily, repo_languages, repo_commits, repo_truncated, repo_failed = collect_repo_stats(
                repo, start, end, args.username, repo_token, args.workers
            )
        except RuntimeError as error:
            skipped.append(full_name)
            print(f"[{index}/{len(repos)}] skipped {full_name}: {error}")
            continue
        daily.update(repo_daily)
        languages.update(repo_languages)
        commits += repo_commits
        truncated_file_lists += repo_truncated
        failed_commit_details += repo_failed
        detail_note = f", {repo_failed} detail failures" if repo_failed else ""
        print(f"[{index}/{len(repos)}] {full_name}: {repo_commits} commits{detail_note}")

    stats = {
        "username": args.username,
        "scan_mode": scan_mode,
        "window_days": args.days,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "code_lines_added": sum(daily.values()),
        "authored_commits": commits,
        "active_days": sum(1 for value in daily.values() if value),
        "repositories_scanned": len(repos),
        "repositories_skipped": len(skipped),
        "commits_with_truncated_file_lists": truncated_file_lists,
        "commit_detail_failures": failed_commit_details,
        "languages": dict(sorted(languages.items(), key=lambda item: (-item[1], item[0]))),
        "daily_additions": {day.isoformat(): daily[day.isoformat()] for day in (start + dt.timedelta(days=i) for i in range(args.days))},
    }
    svg = render_svg(start, end, daily, languages, commits)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "github-line-velocity.svg").write_text(svg, encoding="utf-8")
    stats_path = Path(args.stats_path)
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"Generated {compact_number(stats['code_lines_added'])} code lines across "
        f"{commits:,} authored commits; skipped {len(skipped)} repositories."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
