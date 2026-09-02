#!/usr/bin/env bash
# Adds the shared secret-scan caller stub to every org repo that does not call
# it yet, one PR per repo. Idempotent: a repo that already calls the shared
# workflow, or already carries the distribution branch, is left untouched.
set -euo pipefail

BRANCH="${BRANCH:-chore/add-secret-scan}"
STUB_PATH="${STUB_PATH:-.github/workflows/common-pr-checks.yml}"
# Any workflow calling the shared workflow counts as covered, whatever the file
# is named: infra calls it from pr-checks.yml, ui-monorepo from a file that has
# extra jobs of its own.
MARKER="${MARKER:-common-repository-pr-checks.yml}"
DRY_RUN="${DRY_RUN:-true}"
REPOS="${REPOS:-}"
LIST_LIMIT="${LIST_LIMIT:-1000}"

: "${GH_TOKEN:?a GitHub App installation token is required}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
templates="$repo_root/distribution/templates"
skip_file="$repo_root/distribution/skip-repos.txt"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

summary=()
failed=0

record() {
  summary+=("$1|$2")
  echo "[$2] $1"
}

target_repos() {
  local names

  if [[ -n "$REPOS" ]]; then
    tr ' ' '\n' <<<"$REPOS" | sed '/^$/d'
    return 0
  fi

  # Discovery rather than an allowlist: a repo created next month is in scope
  # without anyone remembering to add it.
  names="$(gh repo list iomete --limit "$LIST_LIMIT" --no-archived --json name --jq '.[].name')" || return 1

  # Refuse to run on a truncated listing: silently covering all but the last
  # few repos is the one failure this script must not have.
  if [[ "$(grep -c . <<<"$names")" -ge "$LIST_LIMIT" ]]; then
    echo "Repo listing hit the $LIST_LIMIT cap; raise LIST_LIMIT so no repo is missed" >&2
    return 1
  fi

  grep -vxF -f <(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$skip_file") <<<"$names"
}

repo_url() {
  echo "https://x-access-token:${GH_TOKEN}@github.com/iomete/$1.git"
}

# Every step returns non-zero on trouble rather than relying on errexit, which
# bash disables for a function called as an `if` condition.
process_repo() {
  local repo="$1" meta default_branch visibility template dir heads

  meta="$(gh api "repos/iomete/$repo" --jq '[.default_branch, .visibility] | @tsv')" || return 1
  IFS=$'\t' read -r default_branch visibility <<<"$meta"

  # Branches decide both questions below. The API's `size` cannot: it is in
  # whole KB, so a small repo with real content reports 0 like an empty one.
  heads="$(git ls-remote --heads "$(repo_url "$repo")")" || return 1

  if [[ -z "$heads" || -z "$default_branch" ]]; then
    record "$repo" empty
    return 0
  fi

  if awk -v ref="refs/heads/$BRANCH" '$2 == ref { found = 1 } END { exit !found }' <<<"$heads"; then
    record "$repo" branch-exists
    return 0
  fi

  # Only the workflow directory is needed to decide, so fetch nothing else.
  dir="$work/$repo"
  git clone --quiet --depth 1 --filter=blob:none --sparse "$(repo_url "$repo")" "$dir" || return 1
  git -C "$dir" sparse-checkout set .github/workflows >/dev/null || return 1

  if grep -rqsF "$MARKER" "$dir/.github/workflows"; then
    record "$repo" present
    return 0
  fi

  # Something else already owns that filename. Leave it for a human rather than
  # overwriting a workflow we know nothing about.
  if [[ -e "$dir/$STUB_PATH" ]]; then
    record "$repo" conflict
    return 0
  fi

  # Anything but an explicit "false" stays a dry run, so a mistyped value
  # cannot open PRs across the org.
  if [[ "$DRY_RUN" != "false" ]]; then
    record "$repo" would-add
    return 0
  fi

  template="$templates/common-pr-checks.yml"
  # The self-hosted runners are private-repo only.
  if [[ "$visibility" == "public" ]]; then
    template="$templates/common-pr-checks.public.yml"
  fi

  git -C "$dir" switch --quiet -c "$BRANCH" || return 1
  mkdir -p "$dir/$(dirname "$STUB_PATH")"
  cp "$template" "$dir/$STUB_PATH"
  git -C "$dir" add -- "$STUB_PATH" || return 1
  git -C "$dir" -c user.name='iomete-ci-distributor[bot]' \
    -c user.email='iomete-ci-distributor[bot]@users.noreply.github.com' \
    commit --quiet -m 'ci: run the shared secret scan on pull requests' || return 1
  git -C "$dir" push --quiet origin "$BRANCH" || return 1

  gh pr create --repo "iomete/$repo" --base "$default_branch" --head "$BRANCH" \
    --title 'ci: run the shared secret scan on pull requests' \
    --body 'Every pull request in this repo is now scanned for committed secrets.

- Only the commits a pull request adds are scanned, so findings already in history do not fail unrelated work.
- The check is the org-wide shared one, so it stays in step with every other repo.

Opened automatically for ENG-8692. Comment here if this repo needs an exception.' >/dev/null || return 1

  record "$repo" pr-opened
}

if ! target_repos >"$work/repos.txt"; then
  echo "Could not determine which repositories to process" >&2
  exit 1
fi

repos=()
while IFS= read -r line; do
  [[ -n "$line" ]] && repos+=("$line")
done <"$work/repos.txt"

if [[ "${#repos[@]}" -eq 0 ]]; then
  echo "No repositories to process" >&2
  exit 1
fi

for repo in "${repos[@]}"; do
  if ! process_repo "$repo"; then
    record "$repo" failed
    failed=1
  fi
done

{
  echo "## Secret scan distribution"
  echo
  if [[ "$DRY_RUN" != "false" ]]; then
    echo "Dry run: no branches or pull requests were created."
    echo
  fi
  echo "| Repo | Outcome |"
  echo "| --- | --- |"
  for row in ${summary[@]+"${summary[@]}"}; do
    echo "| ${row%%|*} | ${row##*|} |"
  done
} >>"${GITHUB_STEP_SUMMARY:-/dev/stdout}"

exit "$failed"
