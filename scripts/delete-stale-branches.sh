#!/usr/bin/env bash
#
# Delete head branches of CLOSED or MERGED pull requests across every active repo
# in the org. This is the part GitHub does NOT do natively (the native setting
# only fires on MERGE, never on close-without-merge, and never retroactively).
#
# SAFE BY DEFAULT: DRY_RUN=true logs what it WOULD delete and touches nothing.
#
# A branch is deleted only when ALL of these hold:
#   - it is the head of >=1 PR that is now CLOSED or MERGED  (we only ever act on
#     branches that demonstrably had a PR — never random WIP branches)
#   - the PR's head is in THIS repo, not a fork (fork branches aren't ours)
#   - it is not the repo's default branch
#   - its name doesn't match a protected pattern (main/master/release/* ...)
#   - it has NO currently-open PR
#   - the ref still exists (already-auto-deleted branches are skipped)
#   - it is not protected by a branch-protection rule / ruleset
#   - its tip commit is older than GRACE_DAYS (spare freshly-merged work)
#
# Env:
#   ORG            org login                                   (default: iomete)
#   DRY_RUN        true|false                                  (default: true)
#   GRACE_DAYS     spare branches whose tip is newer than N    (default: 7; 0 disables the check)
#   LOOKBACK_DAYS  only consider PRs closed within N days       (default: 90; bounds API calls)
#   REPOS          limit to these repo names (space/newline)    (default: all active repos)
#
# Requires: gh authenticated with Contents: write + Pull requests: read across
#           the org's repos (classic PAT `repo` scope, or a fine-grained PAT /
#           GitHub App install token). The default Actions GITHUB_TOKEN cannot
#           cross repositories, so a cross-repo token is mandatory here.
set -euo pipefail

ORG="${ORG:-iomete}"
DRY_RUN="${DRY_RUN:-true}"
GRACE_DAYS="${GRACE_DAYS:-7}"
LOOKBACK_DAYS="${LOOKBACK_DAYS:-90}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"

# Branch names we never delete, even if a closed PR points at them.
PROTECTED_REGEX='^(main|master|develop|dev|trunk|staging|stage|production|prod|release([/_-].*)?|hotfix([/_-].*)?)$'

# Date helpers: GNU date in CI, BSD date fallback for local macOS runs.
iso_since="$(date -u -d "-${LOOKBACK_DAYS} days" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
            || date -u -v-"${LOOKBACK_DAYS}"d +%Y-%m-%dT%H:%M:%SZ)"
now_epoch="$(date -u +%s)"
grace_epoch=$(( now_epoch - GRACE_DAYS * 86400 ))
epoch_of() { date -u -d "$1" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$1" +%s 2>/dev/null || echo 0; }

mode="APPLY"; [[ "$DRY_RUN" == "true" ]] && mode="DRY-RUN"
echo "Mode: $mode  org=$ORG  grace=${GRACE_DAYS}d  lookback=${LOOKBACK_DAYS}d  since=$iso_since"
{
  echo "## Branch cleanup — $mode"
  echo "org \`$ORG\` · grace ${GRACE_DAYS}d · lookback ${LOOKBACK_DAYS}d (since $iso_since)"
  echo ""
} >> "$SUMMARY"

# REPOS (optional): space/newline-separated repo names to limit the run to.
# Unset = every active repo in the org.
if [[ -n "${REPOS:-}" ]]; then
  repos="$(printf '%s\n' $REPOS)"
else
  repos="$(gh repo list "$ORG" --limit 1000 --json name,isArchived \
    --jq '.[] | select(.isArchived | not) | .name')"
fi

total_del=0; total_would=0; total_skip=0
while IFS= read -r repo <&3; do
  [[ -z "$repo" ]] && continue
  full="$ORG/$repo"
  default_branch="$(gh repo view "$full" --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null || echo '')"

  prs="$(gh pr list -R "$full" --state closed --limit 1000 \
        --json state,headRefName,isCrossRepository,closedAt 2>/dev/null || echo '[]')"

  branches="$(printf '%s' "$prs" | jq -r --arg since "$iso_since" '
    [ .[]
      | select(.isCrossRepository | not)   # fork head branches are not ours to delete
      | select(.closedAt >= $since)
      | .headRefName ] | unique | .[]')"

  while IFS= read -r br <&4; do
    [[ -z "$br" || "$br" == "$default_branch" ]] && { total_skip=$((total_skip+1)); continue; }
    if [[ "$br" =~ $PROTECTED_REGEX ]]; then total_skip=$((total_skip+1)); continue; fi

    open_ct="$(gh pr list -R "$full" --state open --head "$br" --json number --jq 'length' 2>/dev/null || echo 0)"
    [[ "$open_ct" != "0" ]] && { total_skip=$((total_skip+1)); continue; }

    ref_json="$(gh api "repos/$full/git/refs/heads/$br" 2>/dev/null || true)"
    [[ -z "$ref_json" ]] && { total_skip=$((total_skip+1)); continue; }

    protected="$(gh api "repos/$full/branches/$br" --jq '.protected' 2>/dev/null || echo false)"
    [[ "$protected" == "true" ]] && { total_skip=$((total_skip+1)); continue; }

    if [[ "$GRACE_DAYS" -gt 0 ]]; then
      sha="$(printf '%s' "$ref_json" | jq -r '.object.sha')"
      cdate="$(gh api "repos/$full/commits/$sha" --jq '.commit.committer.date' 2>/dev/null || echo '')"
      if [[ -n "$cdate" && "$(epoch_of "$cdate")" -gt "$grace_epoch" ]]; then
        total_skip=$((total_skip+1)); continue
      fi
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
      echo "  would delete: $full @ $br"
      echo "- would delete \`$full@$br\`" >> "$SUMMARY"
      total_would=$((total_would+1))
    else
      if gh api --method DELETE "repos/$full/git/refs/heads/$br" >/dev/null 2>&1; then
        echo "  deleted: $full @ $br"
        echo "- deleted \`$full@$br\`" >> "$SUMMARY"
        total_del=$((total_del+1))
      else
        echo "  FAILED:  $full @ $br"
        echo "- FAILED \`$full@$br\`" >> "$SUMMARY"
      fi
    fi
  done 4<<< "$branches"
done 3<<< "$repos"

echo "Summary: deleted=$total_del would_delete=$total_would skipped=$total_skip"
{ echo ""; echo "**deleted=$total_del · would_delete=$total_would · skipped=$total_skip**"; } >> "$SUMMARY"
