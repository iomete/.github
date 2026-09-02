# Secret scan distribution

`distribute.sh` adds the shared secret-scan caller stub
(`templates/common-pr-checks.yml`) to every org repo that does not call
`common-repository-pr-checks.yml` yet, one PR per repo. Run it from the
**Distribute secret scan** workflow, which defaults to a dry run.

A repo is skipped when it already calls the shared workflow under any filename,
when it already carries the `chore/add-secret-scan` branch, or when it is listed
in `skip-repos.txt`. So a rerun after a partial pass is safe, and an open PR is
never duplicated.

A repo reported as `conflict` already has a different workflow at the stub's
filename. Nothing is overwritten; sort that repo out by hand.

Distribute in batches with the `repos` input rather than opening every PR at
once: each PR runs the scan, and the private repos share one self-hosted runner
pool that already queues when busy.

## One-time setup

The distributor needs its own GitHub App, because `GITHUB_TOKEN` reaches only
the repo running the workflow and cannot write files under `.github/workflows`
at all.

1. Register a GitHub App named `iomete-ci-distributor`, installed on all org
   repos, with **Contents: write**, **Pull requests: write**, **Workflows:
   write** and **Metadata: read**.
2. Add `CI_DISTRIBUTOR_APP_ID` and `CI_DISTRIBUTOR_APP_PRIVATE_KEY` as org
   secrets available to this repo.
3. Keep the App's login (`iomete-ci-distributor[bot]`) in iom-github-bot's
   approved bot list, so its PRs are approved and merged once checks pass.

If the App ends up with a different slug, update the login in iom-github-bot and
the commit identity in `distribute.sh` to match.
