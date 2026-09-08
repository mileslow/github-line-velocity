# GitHub Line Velocity

This project regenerates the aggregate code-activity graphic embedded in
[`mileslow/mileslow`](https://github.com/mileslow/mileslow), Miles Low's GitHub
profile README.

It scans the authenticated user's accessible, non-fork, non-archived default
branches through GitHub's commit and commit-stat APIs for authored commits in
the last 365 completed UTC days (through yesterday). It counts changed lines in code-like files—GitHub additions
plus deletions—so editing one existing line counts as two changed lines. It excludes
documentation/data/media/generated artifacts, and publishes only aggregate
totals, languages, and daily activity. Repository names are never written to
the generated public files.

The daily workflow runs at 17:17 UTC and can also be started manually. It
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
and the built-in Actions token for public-repository reads.

Every run lists the complete window again, including manual reruns. Completed
commit statistics are cached by fingerprint in `.cache/commit-stats-v2`, and
GitHub Actions restores this disposable cache between runs. Each saved daily
aggregate also fingerprints its commit set, so unchanged days require no detail
requests even if the disposable cache is evicted. Changed commit sets are counted
again, including rewritten history. The scanner follows
commit-file pagination up to GitHub's 3,000-file limit. Commits at that limit are
reported in `commits_with_truncated_file_lists`.

`data/latest.json` contains anonymous per-repository daily aggregates under
`repository_snapshots`. Repository fingerprints connect successive scans; no
repository names, file paths, commit messages, or code are saved. Each successful
scan replaces the accessible repository's aggregate, so reruns, newly discovered
repositories, restored access, and late-arriving commits do not double count.
Unavailable repositories retain their last observed history, trimmed to the
rolling window. The older aggregate's unrecoverable portion is kept separately
in `legacy_carry`; recovery subtracts overlapping legacy counts conservatively.
Overlap remains permanently inaccessible. The pre-existing headline backfill
remains separate from the real daily activity series.

A failed repository listing or commit-detail request leaves the last published
snapshot intact and **fails the workflow**, rather than reporting a successful
refresh with stale data. Subsequent daily runs retry and reuse successful cached
requests. The snapshot includes `last_successful_scan_at` for auditing freshness.
The publisher retries transient errors and refetches the file SHA on conflicts.

Regression tests run on code pushes and pull requests (Python 3.12 and 3.14),
and again in the scheduled workflow before the scan:

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
counts completed local token-count records and deduplicates repeated status
updates using cumulative token counters. For Claude Code, it scans both the
`~/.claude` project records and Claude Desktop's `local-agent-mode-sessions`
records, preferring Claude's latest per-session `cost-state` summary when one
exists because that is the local record that lines up with Claude Code spend. If
a Claude session does not have a usable `cost-state` row, the updater falls back
to completed message usage and deduplicates streaming rows. Raw prompts,
responses, and session contents are never written to the repository. Per-source
watermarks are kept only as audit metadata; they do not prevent the updater from
backfilling older local records that were missed by an earlier run. The preserved Cursor baseline is fixed; missing local records never increase
that baseline. If a previously observed source loses records, the updater leaves
the last complete snapshot untouched until records return. An explicit
`--reconcile` run can repair an older overcount from complete local records; the
daily runner never enables that option.

GitHub Actions cannot read local home-directory records. The installed macOS
LaunchAgent in `launchd/com.mileslow.github-line-velocity.plist` runs at **08:17
local time daily**. Login provides a catch-up opportunity, guarded by a saved
completion date so a successful daily sync runs only once per local day. The
LaunchAgent uses a dedicated checkout under
`~/Library/Application Support/github-line-velocity/repo` to avoid macOS Desktop
folder restrictions. That checkout fetches the current code before syncing.

The local runner locks concurrent runs, recovers interrupted snapshot commits,
pushes previously unpushed commits even when no new tokens are found, and retries
push races. Ordinary local syncs leave publishing to the later daily GitHub
workflow. `--refresh` requests an immediate refresh and saves a pending dispatch
if the request fails. `--scheduled` enables the once-per-day guard; omit that flag
for an explicit manual run:

```bash
scripts/run_local_model_usage_sync.sh --refresh
```

Local logs: `~/Library/Logs/github-line-velocity.log`. To inspect the installed
schedule and latest exit status:

```bash
launchctl print gui/$(id -u)/com.mileslow.github-line-velocity
```
