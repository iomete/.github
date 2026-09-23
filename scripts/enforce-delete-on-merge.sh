#!/usr/bin/env bash
#
# Ensure the native "Automatically delete head branches" setting
# (delete_branch_on_merge) is ON for every active (non-archived) repo in the org.
#
# This is the NATIVE GitHub behaviour and only affects MERGED PRs. It is
# idempotent, reversible, and deletes nothing retroactively — it only changes a
# repo setting so that FUTURE merges auto-delete their head branch. New repos
# created since the last run are picked up automatically (there is no org-level
# default for this setting, so it must be (re)applied per repo).
#
# Env:
#   ORG       org login            (default: iomete)
#   DRY_RUN   true|false           (default: true — log only)
#
# Requires: gh authenticated with repo-admin rights on the org's repos
#           (classic PAT `repo` scope, or a fine-grained PAT / GitHub App with
#            Administration: write + Metadata: read).
set -euo pipefail

ORG="${ORG:-iomete}"
DRY_RUN="${DRY_RUN:-true}"

repos="$(gh repo list "$ORG" --limit 1000 --json name,isArchived,deleteBranchOnMerge \
  --jq '.[] | select(.isArchived | not) | select(.deleteBranchOnMerge | not) | .name')"
count="$(printf '%s\n' "$repos" | grep -c . || true)"

echo "delete_branch_on_merge is OFF on $count active repo(s) (DRY_RUN=$DRY_RUN)"
while IFS= read -r r; do
  [[ -z "$r" ]] && continue
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  would enable: $ORG/$r"
  else
    # -F (typed) sends a real JSON boolean, not the string "true".
    if gh api --method PATCH "repos/$ORG/$r" -F delete_branch_on_merge=true >/dev/null 2>&1; then
      echo "  enabled: $ORG/$r"
    else
      echo "  FAILED:  $ORG/$r"
    fi
  fi
done <<< "$repos"
echo "Done (enforce delete_branch_on_merge)."
