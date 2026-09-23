# Org-wide branch cleanup policy

Automatically remove head branches once their pull request is **merged** or
**closed**, across every active repo in the `iomete` org.

## What it does

| Case | How it's handled |
|------|------------------|
| **PR merged** | Native GitHub setting `delete_branch_on_merge` (a.k.a. "Automatically delete head branches"). Deletes the head branch instantly on merge. Enabled on every active repo and re-asserted nightly so new repos onboard automatically. |
| **PR closed without merging** | Not native. The nightly `org-branch-cleanup` workflow deletes the head branch with safety guards. |
| **Merged before the setting was on** | The native toggle is not retroactive. The nightly job also sweeps these stragglers. |

There is **no org-level default** for the native setting, so new repos are
covered by the nightly enforcement step rather than by an org switch.

## Components

- `.github/workflows/branch-cleanup.yml` — scheduled (03:00 UTC) + manual workflow.
- `scripts/enforce-delete-on-merge.sh` — idempotently turns on `delete_branch_on_merge` for every active repo.
- `scripts/delete-stale-branches.sh` — deletes head branches of closed/merged PRs, guarded and dry-run-able.

## Safety guards (a branch is only deleted when all hold)

- It had ≥1 PR that is now **closed or merged** (never touches branches that never had a PR).
- The PR head is in **this** repo, not a fork (fork branches can't be deleted from here).
- It is **not** the default branch.
- Its name doesn't match a protected pattern: `main`, `master`, `develop`, `release/*`, `hotfix/*`, `staging`, `production`, …
- It has **no open PR**.
- It is **not** covered by a branch-protection rule / ruleset.
- Its tip commit is older than `GRACE_DAYS` (default 7).

> Deleted branches can be restored from the closed PR in the GitHub UI, but
> GitHub publishes **no guaranteed restore window** — treat deletion as permanent.

## One-time setup

1. **Create a cross-repo token.** The default Actions `GITHUB_TOKEN` cannot act on
   other repos, so the job needs one of:
   - **GitHub App** (recommended): permissions `Administration: write` (for the
     setting), `Contents: write` (for ref deletion), `Pull requests: read`,
     `Metadata: read`. Install org-wide. In the workflow, mint an install token
     with `actions/create-github-app-token` and assign it to `GH_TOKEN`.
   - **Org-admin PAT** (quick start): a classic PAT with `repo` scope (or a
     fine-grained PAT scoped to the org's repos with the four permissions above).
2. **Store it** as the repo secret `BRANCH_CLEANUP_TOKEN` on this `.github` repo
   (Settings → Secrets and variables → Actions).
3. **Stay in dry-run** for the first scheduled runs: leave the repo variable
   `BRANCH_CLEANUP_DRY_RUN` unset or `true`. Review the run summaries.
4. **Go live:** set repo variable `BRANCH_CLEANUP_DRY_RUN=false`.

## Manual runs

Actions → **org-branch-cleanup** → **Run workflow**:
- `dry_run` — `true` (default, log only) or `false` (actually delete).
- `grace_days` / `lookback_days` — widen `lookback_days` for a one-time historical
  backfill (e.g. `3650`), keep it small for nightly runs.

## Tuning

| Setting | Default | Where |
|---------|---------|-------|
| Schedule | `0 3 * * *` (03:00 UTC nightly) | `cron` in the workflow |
| Dry-run (scheduled) | `true` | repo variable `BRANCH_CLEANUP_DRY_RUN` |
| Grace period | `7` days | `GRACE_DAYS` (input/env) |
| PR lookback | `90` days | `LOOKBACK_DAYS` (input/env) |
| Protected names | `main\|master\|develop\|release/*\|…` | `PROTECTED_REGEX` in the GC script |
