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
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


API_ROOT = "https://api.github.com"
API_TIMEOUT_SECONDS = 20
DEFAULT_PROFILE_REPO = "mileslow/mileslow"
DEFAULT_GENERATOR_REPO = "mileslow/github-line-velocity"

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
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise ApiError(f"GitHub API {method} {path} returned {error.code}: {detail[:400]}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ApiError(f"GitHub API {method} {path} failed: {error}") from error
    return json.loads(raw.decode("utf-8")) if raw else None


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
    max_value = max((daily[day.isoformat()] for day in days), default=0)
    log_max = math.log1p(max_value) if max_value else 1

    language_rows = sorted(languages.items(), key=lambda item: (-item[1], item[0]))
    named_languages = [(name, value) for name, value in language_rows if name != "Other"]
    top_languages = named_languages[:6]
    other = languages.get("Other", 0) + sum(value for _, value in named_languages[6:])
    if other:
        top_languages.append(("Other", other))
    language_max = max((value for _, value in top_languages), default=1)

    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    def lang_line(index: int, name: str, value: int) -> str:
        y = 138 + index * 18
        bar_width = max(3, round(164 * value / language_max))
        return (
            f'<text x="54" y="{y}" class="small">{esc(name)}</text>'
            f'<text x="160" y="{y}" class="small">{compact_number(value)}</text>'
            f'<rect x="224" y="{y - 9}" width="{bar_width}" height="8" fill="#111"/>'
        )

    lines = [
        '<svg width="1000" height="320" viewBox="0 0 1000 320" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc">',
        '<title id="title">Miles Low GitHub code line velocity</title>',
        f'<desc id="desc">Code-only additions over the last 365 days, refreshed {esc(end.isoformat())}. Documentation, data, media, and generated artifacts are excluded.</desc>',
        "<style>",
        '  text { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; fill: #111; }',
        "  .title { font-size: 28px; font-weight: 700; }",
        "  .body { font-size: 14px; }",
        "  .small { font-size: 12px; }",
        "  .tiny { font-size: 11px; fill: #666; }",
        "  .axis { stroke: #111; stroke-width: 1; shape-rendering: crispEdges; }",
        "  .bar { stroke: #111; stroke-width: 2.2; opacity: 0.46; shape-rendering: crispEdges; }",
        "</style>",
        '<rect width="1000" height="320" fill="#fff"/>',
        f'<text x="54" y="52" class="title">{compact_number(total)} code lines / 365 days</text>',
        f'<text x="54" y="84" class="body">{commits:,} authored commits · {active_days} active days · refreshed {esc(end.isoformat())}</text>',
        '<text x="54" y="112" class="small">languages by added lines</text>',
    ]
    lines.extend(lang_line(index, name, value) for index, (name, value) in enumerate(top_languages))
    lines.extend(
        [
            '<text x="660" y="62" class="body">code-only · last 365 days</text>',
            f'<text x="660" y="86" class="small">{esc(start.isoformat())} → {esc(end.isoformat())}</text>',
            '<text x="660" y="110" class="small">generated from authored commits</text>',
            '<text x="660" y="134" class="tiny">hover bars for daily additions</text>',
            '<line x1="54" y1="284" x2="964" y2="284" class="axis"/>',
        ]
    )

    x0, x1, baseline = 54.0, 964.0, 284.0
    step = (x1 - x0) / max(1, len(days) - 1)
    for index, day in enumerate(days):
        value = daily[day.isoformat()]
        height = 0 if not value else 8 + 66 * math.log1p(value) / log_max
        x = x0 + index * step
        y = baseline - height
        lines.append(
            f'<line x1="{x:.2f}" y1="{baseline:.1f}" x2="{x:.2f}" y2="{y:.1f}" class="bar">'
            f'<title>{esc(day.isoformat())}: {value:,} code lines added</title></line>'
        )
    lines.extend(
        [
            f'<text x="54" y="305" class="tiny">{esc(start.isoformat())}</text>',
            f'<text x="886" y="305" class="tiny">{esc(end.isoformat())}</text>',
            "</svg>",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="mileslow")
    parser.add_argument("--profile-repo", default=DEFAULT_PROFILE_REPO)
    parser.add_argument("--generator-repo", default=DEFAULT_GENERATOR_REPO)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--workers", type=int, default=8)
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
    repos = list_repositories(token, excluded)
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
            repo_daily, repo_languages, repo_commits, repo_truncated, repo_failed = collect_repo_stats(
                repo, start, end, args.username, token, args.workers
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
