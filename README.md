# GitHub Line Velocity

This project regenerates the aggregate code-activity graphic embedded in
[`mileslow/mileslow`](https://github.com/mileslow/mileslow), Miles Low's GitHub
profile README.

It scans the authenticated user's accessible, non-fork, non-archived default
branches through GitHub's commit and commit-stat APIs for authored commits in
the last 365 days. It counts added lines in code-like files, excludes
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
token's repository-list quota is temporarily exhausted, it falls back to
public repositories instead of failing the refresh. GitHub caps the file list
returned for an individual very large commit; the aggregate snapshot records
how many such commits were encountered in `data/latest.json`.

## Audit notes

The model panel is backed by the checked-in `data/model_usage.json` snapshot.
The current snapshot covers a 365-day window using exact local Codex session
token records plus the exact recoverable portion of the historical Cursor CSV
export. The original Cursor account and raw CSV rows are no longer available,
so Cursor usage is shown as a separate historical source and the snapshot
metadata documents the incomplete Cursor coverage. GitHub Actions cannot read
the local Codex records, so the daily job refreshes the 365-day GitHub-derived
metrics while preserving the latest audited model snapshot.
