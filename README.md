# GitHub Line Velocity

This project regenerates the aggregate code-activity graphic embedded in
[`mileslow/mileslow`](https://github.com/mileslow/mileslow), Miles Low's GitHub
profile README.

It scans the authenticated user's accessible, non-fork, non-archived default
branches through GitHub's commit and commit-stat APIs for authored commits in
the last 365 days. It counts changed lines in code-like files—GitHub additions
plus deletions—so editing one existing line counts as two changed lines. It excludes
documentation/data/media/generated artifacts, and publishes only aggregate
totals, languages, and daily activity. Repository names are never written to
the generated public files.

The daily workflow runs at 08:17 UTC and can also be started manually. It
refreshes the tracked aggregate snapshot in this repo and updates
`assets/github-line-velocity.svg` in the profile repo.

## Local run

```bash
GH_TOKEN="$(gh auth token)" \
python3 scripts/generate_profile.py \
  --output-dir generated \
  --stats-path data/latest.json \
  --username mileslow \
  --profile-repo mileslow/mileslow \
  --generator-repo mileslow/github-line-velocity
```

The token needs read access to the repositories being scanned. The automated
workflow uses `PROFILE_REPO_TOKEN` for private-repository reads and publishing,
and the built-in Actions token for public-repository reads. If the personal
token's repository-list quota is temporarily exhausted, it can fall back to
public repositories, but an existing authenticated baseline rejects that
downgrade instead of publishing a partial refresh. Before writing a new
snapshot, the generator also rejects a sudden drop in repository coverage or
active days and rejects any failed commit-detail requests. GitHub caps the file
list returned for an individual very large commit; the aggregate snapshot
records how many such commits were encountered in `data/latest.json`.
Repository coverage is checked immediately after repository discovery. The
coverage baseline stores one-way fingerprints of the last complete repository
inventory, so a missing repository is remembered without publishing its name.
Overlap is permanently inaccessible: its last known history is always carried
forward as a headline-total backfill, while each newly scanned day adds additions
plus deletions from the repositories that remain accessible. That additive
baseline is not written into `daily_lines_changed`, so the active-day count and
activity graph continue to reflect the real carried/scanned daily series. If
another repository is temporarily incomplete, the generator keeps the previous
daily totals and backfills newly available repository history when access
returns. The first changed-lines refresh can also carry forward the old
additions-only snapshot as historical backfill; it never invents deletions for an
inaccessible repository. A malformed baseline or failed commit-detail request
remains a blocked safe no-op rather than a corrupt update.

The regression tests run in the scheduled workflow before the scan:

```bash
python -m unittest discover --start-directory tests --verbose
```

## Audit notes

The model panel is backed by the checked-in `data/model_usage.json` snapshot.
The current snapshot covers a 365-day window using exact local Codex and Claude
Code session token records plus the exact recoverable portion of the historical
Cursor CSV export. The original Cursor account and raw CSV rows are no longer
available, so the preserved Cursor subtotal is carried as a historical baseline.
When archived model-mix metadata exists, the rendered model breakdown allocates
that baseline across the archived model names so historical Claude usage remains
visible; exact local Codex and Claude Code records continue to use their recorded
model names.

The local `scripts/sync_model_usage.py` updater rebuilds aggregate token usage
from the machine's full model-session JSONL history on each run. For Codex, it
counts completed local token-count records. For Claude Code, it scans both the
`~/.claude` project records and Claude Desktop's `local-agent-mode-sessions`
records, preferring Claude's latest per-session `cost-state` summary when one
exists because that is the local record that lines up with Claude Code spend. If
a Claude session does not have a usable `cost-state` row, the updater falls back
to completed message usage and deduplicates streaming rows. Raw prompts,
responses, and session contents are never written to the repository. Per-source
watermarks are kept only as audit metadata; they do not prevent the updater from
backfilling older local records that were missed by an earlier run. The headline
token total is monotonic: exact local Codex and Claude Code totals are added to
preserved unallocated baselines, and the updater leaves the previous snapshot
untouched if local records are temporarily unavailable.

GitHub Actions cannot read local home-directory records. The installed macOS
LaunchAgent in `launchd/com.mileslow.github-line-velocity.plist` runs the local
updater at login and hourly, commits and pushes only changed aggregate data,
and dispatches the profile workflow when new tokens are found. The GitHub
workflow still runs daily as a fallback and preserves the latest synced model
snapshot when no local machine is available.
