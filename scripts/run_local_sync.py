#!/usr/bin/env python3
"""Run the daily local sync, recovering interrupted commits and pushes."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def command(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=capture, timeout=1800)
    return result.stdout.rstrip("\n") if capture else ""


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state) + "\n")
    temporary.replace(path)


def push_pending() -> None:
    # A prior run may have committed successfully but lost its network before
    # pushing. Always drain that commit before considering a no-change exit.
    for attempt in range(3):
        if command("git", "rev-list", "--count", "origin/main..HEAD", capture=True) == "0":
            return
        try:
            command("git", "push", "origin", "HEAD:main")
            return
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
            command("git", "fetch", "origin", "main")
            try:
                command("git", "rebase", "origin/main")
            except subprocess.CalledProcessError:
                command("git", "rebase", "--abort")
                raise


def sync(repo: Path, scheduled: bool = False, refresh: bool = False) -> None:
    os.chdir(repo)
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    git_dir = Path(command("git", "rev-parse", "--absolute-git-dir", capture=True))
    with (git_dir / "velocity-sync.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another model sync is already running.", flush=True)
            return
        state_path = git_dir / "velocity-sync-state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        today = dt.date.today().isoformat()
        if (scheduled and state.get("completed_date") == today
                and not state.get("pending_refresh") and not state.get("pending_snapshot")):
            print(f"Daily model sync already completed for {today}.", flush=True)
            return

        changes = command("git", "status", "--porcelain", "--untracked-files=all", capture=True)
        if changes:
            paths = {line[3:] for line in changes.splitlines()}
            if paths != {"data/model_usage.json"} or not state.get("pending_snapshot"):
                raise RuntimeError("Local edits are present; commit the edits before the automatic sync.")
            command("git", "add", "data/model_usage.json")
            command("git", "commit", "-m", "Sync local model usage")

        command("git", "fetch", "origin", "main")
        try:
            command("git", "rebase", "origin/main")
        except subprocess.CalledProcessError:
            command("git", "rebase", "--abort")
            raise
        push_pending()
        state["pending_snapshot"] = True
        if refresh:
            state["pending_refresh"] = True
        save_state(state_path, state)
        command(sys.executable, "scripts/sync_model_usage.py", "--snapshot", "data/model_usage.json")
        if command("git", "diff", "--name-only", "--", "data/model_usage.json", capture=True):
            command("git", "add", "data/model_usage.json")
            command("git", "commit", "-m", "Sync local model usage")
            push_pending()
        else:
            print("No new local model usage.", flush=True)
        state["pending_snapshot"] = False
        save_state(state_path, state)
        if state.get("pending_refresh"):
            command("gh", "workflow", "run", "refresh-profile.yml", "--repo",
                    "mileslow/github-line-velocity", "--ref", "main")
            state["pending_refresh"] = False
        state["completed_date"] = today
        save_state(state_path, state)
        print(f"Daily model sync completed for {today}.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduled", action="store_true", help="Skip if today's sync already completed")
    parser.add_argument("--refresh", action="store_true", help="Also dispatch an immediate GitHub scan")
    args = parser.parse_args()
    repo = Path(os.environ.get("GITHUB_LINE_VELOCITY_REPO_DIR", Path(__file__).resolve().parents[1]))
    try:
        sync(repo, args.scheduled, args.refresh)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"github-line-velocity: {error}; the next run will retry.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
