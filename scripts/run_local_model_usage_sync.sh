#!/bin/sh

set -eu

REPO_DIR="${GITHUB_LINE_VELOCITY_REPO_DIR:-/Users/miles/Documents/Codex/2026-08-31/https-github-com-mileslow-tab-overview/work/github-line-velocity}"
GIT="/usr/bin/git"
PYTHON="/opt/homebrew/bin/python3"
GH="/opt/homebrew/bin/gh"

log() {
    printf '%s\n' "github-line-velocity: $*"
}

cd "$REPO_DIR"

# Never mix an automatic snapshot commit with an in-progress manual change.
if [ -n "$("$GIT" status --porcelain --untracked-files=all)" ]; then
    log "worktree has local edits; waiting for a clean worktree"
    exit 0
fi

export GIT_TERMINAL_PROMPT=0
if ! "$GIT" fetch origin main; then
    log "could not fetch origin/main; will retry on the next interval"
    exit 1
fi
if ! "$GIT" rebase origin/main; then
    "$GIT" rebase --abort >/dev/null 2>&1 || true
    log "could not rebase onto origin/main; will retry on the next interval"
    exit 1
fi

"$PYTHON" scripts/sync_model_usage.py --snapshot data/model_usage.json
if "$GIT" diff --quiet -- data/model_usage.json; then
    log "no new local model usage"
    exit 0
fi

"$GIT" add data/model_usage.json
if "$GIT" diff --cached --quiet -- data/model_usage.json; then
    log "model snapshot did not produce a meaningful change"
    exit 0
fi
"$GIT" commit -m "Sync local model usage"

if ! "$GIT" push origin HEAD:main; then
    # The GitHub refresh can commit its aggregate files at the same time.
    # Rebase the local-only model snapshot and retry once instead of losing it.
    "$GIT" fetch origin main
    "$GIT" rebase origin/main
    "$GIT" push origin HEAD:main
fi

# Publish the new token total immediately; the daily schedule remains a fallback.
if ! "$GH" workflow run refresh-profile.yml --repo mileslow/github-line-velocity --ref main; then
    log "snapshot pushed, but the profile refresh could not be dispatched"
    exit 1
fi
log "snapshot pushed and profile refresh dispatched"
