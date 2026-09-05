#!/usr/bin/env python3
"""Merge new local Claude Code and Codex usage into the model snapshot.

Only aggregate token totals are written. Raw prompts, responses, and session
contents never leave the local machine.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterator


DEFAULT_SNAPSHOT = Path("data/model_usage.json")
DEFAULT_CLAUDE_ROOTS = (
    Path("/Users/miles/.claude"),
    Path("/Users/miles/Library/Application Support/Claude/local-agent-mode-sessions"),
)
DEFAULT_CODEX_ROOT = Path("/Users/miles/.codex")
CLAUDE_SOURCE_KEY = "claude_code"
CODEX_SOURCE_KEY = "codex"
CLAUDE_SOURCE_NAME = "local Claude Code token records"
CODEX_SOURCE_NAME = "local Codex session token records"


def parse_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def usage_tokens(usage: dict[str, Any]) -> int:
    if "total_tokens" in usage:
        try:
            total = int(usage["total_tokens"] or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("usage total_tokens must be an integer") from error
    else:
        fields = (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
        )
        try:
            total = sum(int(usage.get(field, 0) or 0) for field in fields)
        except (TypeError, ValueError) as error:
            raise ValueError("usage token fields must be integers") from error
    if total < 0:
        raise ValueError("usage tokens must not be negative")
    return total


def load_json_lines(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def changed_jsonl_paths(root: Path, after: dt.datetime | None = None) -> Iterator[Path]:
    """Yield JSONL files, optionally limited by a source watermark."""
    after_epoch = after.timestamp() if after is not None else None
    for path in sorted(root.glob("**/*.jsonl")):
        if after_epoch is not None:
            try:
                if path.stat().st_mtime <= after_epoch:
                    continue
            except OSError:
                # A file can disappear while a session is being rotated. Let the
                # reader handle that case instead of making the whole sync fail.
                pass
        yield path


def timestamp_from_epoch_millis(value: object) -> dt.datetime | None:
    if isinstance(value, bool):
        return None
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(millis / 1000, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def claude_cost_state_token_total(usage: dict[str, Any]) -> int:
    fields = (
        "inputTokens",
        "cacheCreationInputTokens",
        "cacheReadInputTokens",
        "outputTokens",
        "thinkingTokens",
    )
    try:
        total = sum(int(usage.get(field, 0) or 0) for field in fields)
    except (TypeError, ValueError) as error:
        raise ValueError("Claude cost-state token fields must be integers") from error
    if total < 0:
        raise ValueError("Claude cost-state tokens must not be negative")
    return total


def claude_cost_state_events(
    root: Path, after: dt.datetime | None = None
) -> tuple[list[tuple[str, dt.datetime, int]], set[str]]:
    latest_by_session: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
    for path in changed_jsonl_paths(root, after):
        for item in load_json_lines(path):
            if item.get("type") != "cost-state":
                continue
            session_id = item.get("sessionId") or item.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            timestamp = timestamp_from_epoch_millis(item.get("startTime"))
            if timestamp is None or (after is not None and timestamp <= after):
                continue
            try:
                marker = (int(item.get("startTime", 0) or 0), int(item.get("totalDuration", 0) or 0))
            except (TypeError, ValueError):
                marker = (int(timestamp.timestamp() * 1000), 0)
            previous = latest_by_session.get(session_id)
            if previous is None or marker > previous[0]:
                latest_by_session[session_id] = (marker, item)

    events: list[tuple[str, dt.datetime, int]] = []
    counted_session_ids: set[str] = set()
    for _, item in latest_by_session.values():
        timestamp = timestamp_from_epoch_millis(item.get("startTime"))
        if timestamp is None:
            continue
        session_id = item.get("sessionId") or item.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        model_usage = item.get("modelUsage")
        if not isinstance(model_usage, dict):
            continue
        session_tokens = 0
        for model, usage in model_usage.items():
            if not isinstance(model, str) or not model or not isinstance(usage, dict):
                continue
            tokens = claude_cost_state_token_total(usage)
            if tokens:
                events.append((model, timestamp, tokens))
                session_tokens += tokens
        if session_tokens:
            counted_session_ids.add(session_id)
    return sorted(events, key=lambda value: value[1]), counted_session_ids


def claude_message_usage_events(
    root: Path, after: dt.datetime | None = None
) -> Iterator[tuple[str, dt.datetime, int, str | None]]:
    latest_by_request: dict[str, tuple[str, dt.datetime, dict[str, Any], str | None]] = {}
    for path in changed_jsonl_paths(root, after):
        for item in load_json_lines(path):
            message = item.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            # Claude writes several streaming rows for one response. Only a
            # terminal response is safe to count across separate sync runs;
            # an unfinished row may later be replaced with a larger total.
            if message.get("stop_reason") is None:
                continue
            timestamp = parse_timestamp(item.get("timestamp"))
            if timestamp is None or (after is not None and timestamp <= after):
                continue
            request_id = message.get("id") or item.get("requestId")
            if not isinstance(request_id, str) or not request_id:
                continue
            model = message.get("model") or "Unattributed Claude Code"
            if not isinstance(model, str) or not model:
                model = "Unattributed Claude Code"
            session_id = item.get("sessionId") or item.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                session_id = None
            previous = latest_by_request.get(request_id)
            if previous is None or timestamp > previous[1]:
                latest_by_request[request_id] = (model, timestamp, usage, session_id)
    for model, timestamp, usage, session_id in sorted(
        latest_by_request.values(), key=lambda value: value[1]
    ):
        tokens = usage_tokens(usage)
        if tokens:
            yield model, timestamp, tokens, session_id


def claude_usage_events(
    root: Path, after: dt.datetime | None = None
) -> Iterator[tuple[str, dt.datetime, int]]:
    cost_events, cost_state_session_ids = claude_cost_state_events(root, after)
    yield from cost_events
    for model, timestamp, tokens, session_id in claude_message_usage_events(root, after):
        if session_id is not None and session_id in cost_state_session_ids:
            continue
        yield model, timestamp, tokens


def combined_claude_usage_events(
    roots: tuple[Path, ...], after: dt.datetime | None = None
) -> list[tuple[str, dt.datetime, int]]:
    events: list[tuple[str, dt.datetime, int]] = []
    seen_roots: set[Path] = set()
    for root in roots:
        canonical_root = root.expanduser().resolve(strict=False)
        if canonical_root in seen_roots:
            continue
        seen_roots.add(canonical_root)
        events.extend(claude_usage_events(canonical_root, after))
    return sorted(events, key=lambda value: value[1])


def codex_usage_events(
    root: Path, after: dt.datetime | None = None
) -> Iterator[tuple[str, dt.datetime, int]]:
    for path in changed_jsonl_paths(root, after):
        current_model = "Unattributed Codex"
        for item in load_json_lines(path):
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            if item.get("type") == "turn_context":
                model = payload.get("model")
                if isinstance(model, str) and model:
                    current_model = model
                continue
            if payload.get("type") != "token_count":
                continue
            timestamp = parse_timestamp(item.get("timestamp"))
            if timestamp is None or (after is not None and timestamp <= after):
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            usage = info.get("last_token_usage")
            if not isinstance(usage, dict):
                continue
            tokens = usage_tokens(usage)
            if tokens:
                yield current_model, timestamp, tokens


def snapshot_after(snapshot: dict[str, Any]) -> dt.datetime:
    sync = snapshot.get("usage_sync")
    if isinstance(sync, dict):
        timestamp = parse_timestamp(sync.get("last_processed_at"))
        if timestamp is not None:
            return timestamp
    end_date = snapshot.get("end_date")
    parsed_end = parse_timestamp(f"{end_date}T23:59:59Z")
    if parsed_end is None:
        raise ValueError("model snapshot must contain an ISO end_date")
    return parsed_end


def unallocated_token_baseline(snapshot: dict[str, Any]) -> int:
    try:
        baseline = int(snapshot.get("unallocated_token_baseline", 0) or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("model snapshot unallocated_token_baseline must be an integer") from error
    if baseline < 0:
        raise ValueError("model snapshot unallocated_token_baseline must not be negative")
    return baseline


def jsonl_file_count(root: Path) -> int:
    return sum(1 for _ in root.glob("**/*.jsonl"))


def jsonl_file_count_many(roots: tuple[Path, ...]) -> int:
    count = 0
    seen_roots: set[Path] = set()
    for root in roots:
        canonical_root = root.expanduser().resolve(strict=False)
        if canonical_root in seen_roots:
            continue
        seen_roots.add(canonical_root)
        count += jsonl_file_count(canonical_root)
    return count


def event_model_totals(
    events: list[tuple[str, dt.datetime, int]]
) -> Counter[str]:
    models: Counter[str] = Counter()
    for model, _, tokens in events:
        models[model] += tokens
    return models


def source_record(
    name: str,
    events: list[tuple[str, dt.datetime, int]],
    session_files: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not events:
        return None
    dates = [timestamp.date() for _, timestamp, _ in events]
    record: dict[str, Any] = {
        "name": name,
        "start_date": min(dates).isoformat(),
        "end_date": max(dates).isoformat(),
        "session_files": session_files,
        "token_events": len(events),
        "total_tokens": sum(tokens for _, _, tokens in events),
    }
    if extra:
        record.update(extra)
    return record


def non_local_sources(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    sources = snapshot.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("model snapshot sources must be a list")
    return [
        source
        for source in sources
        if isinstance(source, dict)
        and source.get("name") not in {CLAUDE_SOURCE_NAME, CODEX_SOURCE_NAME}
    ]


def rebuild_from_local_sources(
    snapshot: dict[str, Any],
    claude_events: list[tuple[str, dt.datetime, int]],
    codex_events: list[tuple[str, dt.datetime, int]],
    claude_session_files: int,
    codex_session_files: int,
    previous_after: dt.datetime,
) -> int:
    events = claude_events + codex_events
    if not events:
        return 0
    previous_total = int(snapshot.get("total_tokens", 0) or 0)
    baseline = unallocated_token_baseline(snapshot)
    models = event_model_totals(claude_events + codex_events)
    model_total = sum(models.values())
    rebuilt_total = baseline + model_total
    if previous_total > rebuilt_total:
        baseline += previous_total - rebuilt_total
        snapshot["unallocated_token_baseline"] = baseline
        rebuilt_total = previous_total
    elif baseline:
        snapshot["unallocated_token_baseline"] = baseline
    elif "unallocated_token_baseline" in snapshot:
        del snapshot["unallocated_token_baseline"]

    snapshot["models"] = [
        {"name": name, "tokens": tokens}
        for name, tokens in sorted(models.items(), key=lambda item: (-item[1], item[0]))
    ]
    snapshot["total_tokens"] = rebuilt_total

    newest = max(timestamp for _, timestamp, _ in events)
    oldest = min(timestamp for _, timestamp, _ in events)
    end = max(dt.date.fromisoformat(snapshot["end_date"]), newest.date())
    snapshot["end_date"] = end.isoformat()
    snapshot["start_date"] = min(
        dt.date.fromisoformat(snapshot["start_date"]),
        oldest.date(),
    ).isoformat()

    sources = non_local_sources(snapshot)
    codex_source = source_record(
        CODEX_SOURCE_NAME,
        codex_events,
        codex_session_files,
        {"malformed_lines": 0},
    )
    if codex_source is not None:
        codex_source["unattributed_model_events"] = sum(
            1 for model, _, _ in codex_events if model == "Unattributed Codex"
        )
        sources.insert(0, codex_source)
    claude_source = source_record(
        CLAUDE_SOURCE_NAME,
        claude_events,
        claude_session_files,
        {
            "token_count_method": (
                "Claude cost-state input + cache creation + cache read + output + thinking tokens "
                "when available; otherwise completed request usage deduplicated by request"
            ),
        },
    )
    if claude_source is not None:
        insert_at = 1 if codex_source is not None else 0
        sources.insert(insert_at, claude_source)
    snapshot["sources"] = sources
    snapshot["source"] = (
        "exact local Codex token records, Claude Code cost-state/request token records, "
        "plus a preserved unallocated Cursor export subtotal"
    )
    snapshot["coverage_status"] = (
        "Exact known Codex records and Claude Code cost-state/request records plus a preserved Cursor "
        "subtotal; Cursor coverage is incomplete because the original account and raw CSV rows are no "
        "longer available."
    )
    snapshot["allocation_method"] = (
        "Codex last-token usage and Claude Code cost-state summaries are grouped by recorded model; "
        "Claude Code sessions without cost-state summaries fall back to completed request usage. The "
        "preserved Cursor subtotal contributes only to the headline token total because the raw rows "
        "are unavailable."
    )

    snapshot["usage_sync"] = {
        "last_processed_at": newest.isoformat().replace("+00:00", "Z"),
        "sync_mode": "full_local_rescan",
        "sources": ["local Claude Code session records", "local Codex session records"],
        "previous_processed_at": previous_after.isoformat().replace("+00:00", "Z"),
        "source_watermarks": {
            CLAUDE_SOURCE_KEY: (
                max((timestamp for _, timestamp, _ in claude_events), default=previous_after)
                .isoformat()
                .replace("+00:00", "Z")
            ),
            CODEX_SOURCE_KEY: (
                max((timestamp for _, timestamp, _ in codex_events), default=previous_after)
                .isoformat()
                .replace("+00:00", "Z")
            ),
        },
    }
    return max(0, rebuilt_total - previous_total)


def comparable_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    comparable = json.loads(json.dumps(snapshot))
    sync = comparable.get("usage_sync")
    if isinstance(sync, dict):
        sync.pop("previous_processed_at", None)
    return comparable


def write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    """Replace the snapshot atomically so an interrupted sync cannot corrupt it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(snapshot, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument(
        "--claude-root",
        dest="claude_roots",
        action="append",
        type=Path,
        help=(
            "Claude JSONL root to scan. May be passed more than once; defaults to both "
            "~/.claude and Claude Desktop local-agent-mode sessions."
        ),
    )
    parser.add_argument("--codex-root", type=Path, default=DEFAULT_CODEX_ROOT)
    parser.add_argument("--since", type=str)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        raise SystemExit("model snapshot must contain an object")
    before = comparable_snapshot(snapshot)
    after = parse_timestamp(args.since) if args.since else snapshot_after(snapshot)
    if after is None:
        raise SystemExit("--since must be an ISO timestamp")
    claude_roots = tuple(args.claude_roots or DEFAULT_CLAUDE_ROOTS)
    claude_events = combined_claude_usage_events(claude_roots)
    codex_events = list(codex_usage_events(args.codex_root))
    added = rebuild_from_local_sources(
        snapshot,
        claude_events,
        codex_events,
        jsonl_file_count_many(claude_roots),
        jsonl_file_count(args.codex_root),
        after,
    )
    if comparable_snapshot(snapshot) == before:
        print("No local model usage changes found.")
        return 0
    print(
        "Rebuilt local model usage from full local records; "
        f"headline token total increased by {added:,} tokens."
    )
    if args.dry_run:
        return 0
    write_snapshot(args.snapshot, snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
