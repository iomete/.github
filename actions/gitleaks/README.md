# Gitleaks action

Scans for secrets and fails the job when it finds one. Usable from public and
private repos in the org. Found values are redacted in the job log.

Most repos want the [reusable workflow](../../.github/workflows/gitleaks.yml)
instead, which wires this up for pull requests. Use the action directly when you
need to scan something that is not a commit range, such as an artifact about to
be published.

## Scan a commit range

```yaml
- uses: iomete/.github/actions/gitleaks@main
  with:
    log-opts: ${{ github.event.pull_request.base.sha }}..${{ github.event.pull_request.head.sha }}
```

Requires `fetch-depth: 0` on the checkout. Omit `log-opts` to scan all history.

## Scan a directory before publishing it

```yaml
- uses: iomete/.github/actions/gitleaks@main
  with:
    mode: dir
    path: /tmp/chart-scan
```

## Inputs

| Input | Default | Description |
|-----------|------------|-------------------------------------------------------|
| `mode` | `git` | `git` for a commit range, `dir` for a path on disk |
| `path` | `.` | Directory to scan when `mode: dir` |
| `log-opts`| `""` | git log options selecting the range when `mode: git` |
| `config` | `""` | Path to a gitleaks config, for a repo-specific allowlist |
| `version` | `8.30.1` | gitleaks release to install |

## Allowlisting a false positive

Add a `.gitleaks.toml` to the calling repo and pass it as `config`, or mark the
line itself with a `gitleaks:allow` comment.
