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
DEFAULT_CLAUDE_ROOT = Path("/Users/miles/.claude/projects")
DEFAULT_CODEX_ROOT = Path("/Users/miles/.codex")


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


def claude_usage_events(
    root: Path, after: dt.datetime
) -> Iterator[tuple[str, dt.datetime, int]]:
    latest_by_request: dict[str, tuple[str, dt.datetime, dict[str, Any]]] = {}
    for path in sorted(root.glob("**/*.jsonl")):
        for item in load_json_lines(path):
            message = item.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            timestamp = parse_timestamp(item.get("timestamp"))
            if timestamp is None or timestamp <= after:
                continue
            request_id = message.get("id") or item.get("requestId")
            if not isinstance(request_id, str) or not request_id:
                continue
            model = message.get("model") or "Unattributed Claude Code"
            if not isinstance(model, str) or not model:
                model = "Unattributed Claude Code"
            previous = latest_by_request.get(request_id)
            if previous is None or timestamp > previous[1]:
                latest_by_request[request_id] = (model, timestamp, usage)
    for model, timestamp, usage in sorted(latest_by_request.values(), key=lambda value: value[1]):
        yield model, timestamp, usage_tokens(usage)


def codex_usage_events(
    root: Path, after: dt.datetime
) -> Iterator[tuple[str, dt.datetime, int]]:
    for path in sorted(root.glob("**/*.jsonl")):
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
            if timestamp is None or timestamp <= after:
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            usage = info.get("last_token_usage")
            if not isinstance(usage, dict):
                continue
            yield current_model, timestamp, usage_tokens(usage)


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


def update_source(
    snapshot: dict[str, Any],
    name: str,
    events: list[tuple[str, dt.datetime, int]],
) -> None:
    if not events:
        return
    sources = snapshot.setdefault("sources", [])
    if not isinstance(sources, list):
        raise ValueError("model snapshot sources must be a list")
    source = next(
        (item for item in sources if isinstance(item, dict) and item.get("name") == name),
        None,
    )
    if source is None:
        source = {"name": name, "total_tokens": 0, "token_events": 0}
        sources.append(source)
    source["total_tokens"] = int(source.get("total_tokens", 0)) + sum(value for _, _, value in events)
    source["token_events"] = int(source.get("token_events", 0)) + len(events)
    dates = [timestamp.date() for _, timestamp, _ in events]
    existing_start = (
        [dt.date.fromisoformat(source["start_date"])]
        if isinstance(source.get("start_date"), str)
        else []
    )
    existing_end = (
        [dt.date.fromisoformat(source["end_date"])]
        if isinstance(source.get("end_date"), str)
        else []
    )
    source["start_date"] = min(existing_start + dates).isoformat()
    source["end_date"] = max(existing_end + dates).isoformat()


def apply_events(
    snapshot: dict[str, Any],
    events: list[tuple[str, dt.datetime, int]],
    after: dt.datetime,
) -> int:
    if not events:
        return 0
    models: Counter[str] = Counter()
    for item in snapshot.get("models", []):
        if not isinstance(item, dict):
            raise ValueError("model snapshot models must contain objects")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("model snapshot model names must be non-empty strings")
        models[name] += int(item.get("tokens", 0))
    for model, _, tokens in events:
        models[model] += tokens
    snapshot["models"] = [
        {"name": name, "tokens": tokens}
        for name, tokens in sorted(models.items(), key=lambda item: (-item[1], item[0]))
    ]
    snapshot["total_tokens"] = sum(models.values())
    newest = max(timestamp for _, timestamp, _ in events)
    end = max(dt.date.fromisoformat(snapshot["end_date"]), newest.date())
    snapshot["end_date"] = end.isoformat()
    snapshot["start_date"] = (end - dt.timedelta(days=int(snapshot.get("window_days", 365)) - 1)).isoformat()
    snapshot["usage_sync"] = {
        "last_processed_at": newest.isoformat().replace("+00:00", "Z"),
        "sources": ["local Claude Code session records", "local Codex session records"],
        "previous_processed_at": after.isoformat().replace("+00:00", "Z"),
    }
    return sum(tokens for _, _, tokens in events)


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
    parser.add_argument("--claude-root", type=Path, default=DEFAULT_CLAUDE_ROOT)
    parser.add_argument("--codex-root", type=Path, default=DEFAULT_CODEX_ROOT)
    parser.add_argument("--since", type=str)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        raise SystemExit("model snapshot must contain an object")
    after = parse_timestamp(args.since) if args.since else snapshot_after(snapshot)
    if after is None:
        raise SystemExit("--since must be an ISO timestamp")
    claude_events = list(claude_usage_events(args.claude_root, after))
    codex_events = list(codex_usage_events(args.codex_root, after))
    events = sorted(claude_events + codex_events, key=lambda value: value[1])
    added = apply_events(snapshot, events, after)
    if not events:
        print("No new local model usage records found.")
        return 0
    update_source(snapshot, "local Claude Code token records", claude_events)
    update_source(snapshot, "local Codex session token records", codex_events)
    print(f"Found {len(events):,} new usage records totaling {added:,} tokens.")
    if args.dry_run:
        return 0
    write_snapshot(args.snapshot, snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
