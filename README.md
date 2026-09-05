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
forward as a backfill, while each newly scanned day adds additions plus deletions
from the repositories that remain accessible. If another repository is temporarily
incomplete, the generator keeps the previous daily totals and backfills newly
available repository history when access returns. The first changed-lines refresh
can also carry forward the old additions-only snapshot as historical backfill;
it never invents deletions for an inaccessible repository. A malformed baseline or
failed commit-detail request remains a blocked safe no-op rather than a corrupt update.

The regression tests run in the scheduled workflow before the scan:

```bash
python -m unittest discover --start-directory tests --verbose
```

## Audit notes

The model panel is backed by the checked-in `data/model_usage.json` snapshot.
The current snapshot covers a 365-day window using exact local Codex session
token records plus the exact recoverable portion of the historical Cursor CSV
export. The original Cursor account and raw CSV rows are no longer available,
so the exact Cursor subtotal is allocated across model names using the
proportions from the preserved, previously published 365-day model breakdown.
The snapshot metadata documents that allocation and the incomplete Cursor
coverage. GitHub Actions cannot read the local Codex records, so the daily job
refreshes the 365-day GitHub-derived metrics while preserving the latest
audited model snapshot.
