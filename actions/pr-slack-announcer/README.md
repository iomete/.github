# PR Slack announcer

The logic behind the shared `PR Slack announcer` workflow (`.github/workflows/pr-slack-announcer.yml`),
usable from public and private repos in the org. It is the public copy of the announcer in
`iomete/infra`, which private repos call today. `pr_slack_announcer.py` is kept identical to
infra's, so a fix there is copied here as-is.

## Turning it on

```yaml
name: PR Slack announcer

on:
  pull_request:
    types: [opened, reopened, ready_for_review, converted_to_draft, closed, review_requested, review_request_removed, assigned, unassigned, edited]
  pull_request_review:
    types: [submitted, dismissed]
  schedule:
    - cron: '0 6 * * 1-5'   # daily reminders, 13:00 Asia/Ho_Chi_Minh
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: read

jobs:
  announce:
    uses: iomete/.github/.github/workflows/pr-slack-announcer.yml@main
    with:
      opened-after: 'YYYY-MM-DD'   # the day you turn it on, so PRs already open aren't posted
      runs-on: ubuntu-latest       # public repos; private ones can keep the self-hosted default
      # channel: C0123456789       # Slack channel ID, default #pr-reviews
      # authors: login1,login2     # empty announces every human PR
    secrets:
      SLACK_NOTIFY_BOT_TOKEN: ${{ secrets.SLACK_NOTIFY_BOT_TOKEN }}
```

The org secret `SLACK_NOTIFY_BOT_TOKEN` must be visible to the calling repo. For a public repo
that means adding it to the secret's repository access.

## Behaviour

- A human PR is posted once it is ready for review, as its author (Slack name and avatar when
  matched, else their GitHub name and avatar), with its reviewers below, marked by their latest
  review: ✅ approved, ⛔ changes requested, 💬 commented only.
- PRs in a stack (same author, each based on another's branch) share the thread of whichever was
  posted first, while that thread is under a day old.
- The message follows the PR: merged gets :pr-merged:, new reviewers are added, ✅ is on it while
  the PR is approved. Back to draft or closed unmerged deletes it and the bot's reminders.
- On weekdays the daily run replies in the thread for each PR ready for at least 24 hours with no
  approval and no changes requested, and again each later weekday while it keeps waiting.
- PRs opened before `opened-after` are never posted, updated or reminded.
- Bot PRs, and PRs from forks (which get no secrets), are not announced.
- State lives in each message's block ID, so nothing else is stored.

## Settings

- `PR_SLACK_ANNOUNCER_DRY_RUN` variable set to `true` on a repo, or the org: runs only log what
  they would do.
- `PR_SLACK_ANNOUNCER_REMINDERS_PAUSED_UNTIL` variable set to a `YYYY-MM-DD` date on a repo or the
  org: reminders are skipped until that day. Announcements keep running.
- `PR_SLACK_ANNOUNCER_SLACK_USERS` variable on the org: people matched by neither their GitHub
  public email nor their name, one `github-login=SLACKUSERID` or `github-login=slack-email` per
  line. A variable rather than a file because this repo is public. Keep it in step with
  `github/actions/pr-slack-announcer/slack-users.txt` in `iomete/infra`.
- `slack-teams.txt`: `team=login1,login2`, teams without GitHub auto-assignment whose review
  requests should mention every member.
- `excluded-authors.txt`: GitHub logins whose PRs are never announced.

The Slack app needs `chat:write`, `chat:write.customize`, `channels:history`, `reactions:write`,
`users:read` and `users:read.email`, and must be a member of the channel.

## Tests

Stdlib `unittest`:

```bash
python3 -m unittest discover -s actions/pr-slack-announcer/tests
```
