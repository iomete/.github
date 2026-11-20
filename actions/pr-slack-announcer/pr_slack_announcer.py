#!/usr/bin/env python3
"""Announce pull requests in Slack and remind reviewers about the ones left waiting.

Runs from the shared .github/workflows/pr-slack-announcer.yml workflow, once per
pull_request or pull_request_review event, and on a schedule for reminders. Each PR's state lives in
its Slack message's block ID, so there is nothing else to store.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Slack drops custom message metadata unless the app's manifest declares it, so the state rides in
# the block ID instead: kept and returned by Slack, never shown.
STATE_PREFIX = "pra1:"
# Messages posted before the state was stored, matched by their first line.
LEGACY_LINK = re.compile(r"^\*?PR:\*? <?https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
APPROVED_REACTION = "white_check_mark"
# Each reviewer's latest review, shown after their name. A decision outranks later comments.
REVIEW_MARKS = {"APPROVED": ":white_check_mark:", "CHANGES_REQUESTED": ":no_entry:", "COMMENTED": ":speech_balloon:"}
MERGED_REACTION = "pr-merged"
# Marks a message whose thread holds a stack, so lookups read only those threads.
STACK_REACTION = "thread"
BLOCK_ID_LIMIT = 255
HISTORY_DAYS = 60
HTTP_TIMEOUT = 20
ATTEMPTS = 3
RECHECK_SECONDS = 5
# How long a PR posted to the channel waits before checking whether a stack PR posted alongside it.
SETTLE_SECONDS = 10
REMINDER_MARK = ":alarm_clock:"
# The first reminder may come this much before remind_after, so a PR made ready just after a run isn't skipped by the next.
REMIND_GRACE = 2 * 3600
# The longest `reminded` timestamp, used to check the state still fits the block ID limit after a reminder.
REMINDED_PLACEHOLDER = 9_999_999_999
# A reminder's block ID lists the numbers of the PRs it is about.
REMINDER_PREFIX = "prr1:"
# A stacked PR joins the thread of another PR in its stack while that thread is younger than this.
STACK_WINDOW = 86400
# The most PRs of a stack read from GitHub when looking for its thread.
STACK_LIMIT = 10

STATE_OPEN = "open"
STATE_DRAFT = "draft"
STATE_CLOSED = "closed"

PR_ACTIONS = {
    "opened", "reopened", "ready_for_review", "converted_to_draft", "closed",
    "review_requested", "review_request_removed", "assigned", "unassigned", "edited",
}
REVIEW_ACTIONS = {"submitted", "dismissed"}


class APIError(Exception):
    """A call the remote API answered with an error. `code` is Slack's error name, when there is one."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def request_json(url, method="GET", token=None, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)

    # A lost event is never replayed, so rate limits and server errors are retried.
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as err:
            if attempt == ATTEMPTS or (err.code != 429 and err.code < 500):
                raise APIError(f"{method} {url}: {err.code} {err.read()[:300]!r}") from err
            time.sleep(min(int(err.headers.get("Retry-After") or 2 ** attempt), 30))
        except urllib.error.URLError as err:
            if attempt == ATTEMPTS:
                raise APIError(f"{method} {url}: {err.reason}") from err
            time.sleep(2 ** attempt)


class Slack:
    """The Slack Web API calls the announcer makes."""

    def __init__(self, token, base_url="https://slack.com/api/", dry_run=False):
        self.token = token
        self.base_url = base_url
        self.bot_id = None
        self.dry_run = dry_run

    def call(self, method, body=None, params=None, ignore=()):
        # Every write is a POST with a body, so the kill switch stops them all here and lets reads through.
        if self.dry_run and body is not None:
            log(f"dry run, not calling slack {method}: {json.dumps(body)[:500]}")
            return {"ok": True, "channel": body.get("channel"), "ts": "dry-run"}
        url = self.base_url + method
        if params:
            url += "?" + urllib.parse.urlencode(params)
        resp = request_json(url, "POST" if body is not None else "GET", self.token, body)
        if not resp.get("ok") and resp.get("error") not in ignore:
            raise APIError(f"slack {method}: {resp.get('error')}", code=resp.get("error"))
        return resp

    def post(self, channel, text, blocks=None, thread_ts=None, sender=None):
        """`sender` is a name and avatar to post as instead of the app (needs chat:write.customize)."""
        body = {"channel": channel, "text": text, "unfurl_links": False}
        if blocks:
            body["blocks"] = blocks
        if thread_ts:
            body.update(thread_ts=thread_ts)
        if sender:
            body.update(username=sender["name"], icon_url=sender["image"])
        resp = self.call("chat.postMessage", body)
        return resp["channel"], resp["ts"]

    def update(self, channel, ts, text, blocks):
        self.call("chat.update", {"channel": channel, "ts": ts, "text": text, "blocks": blocks})

    def delete(self, channel, ts, number, thread=None):
        """Delete an announcement and the bot's reminders about it. Other PRs' announcements in the thread stay,
        and replies from people stay under Slack's placeholder. `thread` is the parent of a stacked PR's message."""
        # Another run may have deleted it already.
        for reply in self.replies(channel, thread or ts):
            # A stack's reminder about other PRs too stays.
            if reply.get("bot_id") == self.bot_id and reminds_about(reply, number, thread, only=True):
                self.call("chat.delete", {"channel": channel, "ts": reply["ts"]}, ignore=("message_not_found",))

        self.call("chat.delete", {"channel": channel, "ts": ts}, ignore=("message_not_found",))

    def react(self, channel, ts, name, add):
        method, ignore = ("reactions.add", ("already_reacted",)) if add else ("reactions.remove", ("no_reaction",))
        self.call(method, {"channel": channel, "timestamp": ts, "name": name}, ignore=ignore)

    def has_reminder(self, channel, ts, since, number, thread=None):
        """Whether the bot already replied with a reminder about the PR in its thread since `since`."""
        return any(m.get("bot_id") == self.bot_id and reminds_about(m, number, thread) and float(m["ts"]) >= since
                   for m in self.replies(channel, thread or ts, oldest=since))

    def replies(self, channel, ts, oldest=None):
        """Yield the thread's replies, without its parent, across every page."""
        params = {"channel": channel, "ts": ts, "limit": 200}
        if oldest is not None:
            params["oldest"] = f"{oldest:.6f}"
        while True:
            resp = self.call("conversations.replies", params=params, ignore=("thread_not_found", "message_not_found"))
            yield from (m for m in resp.get("messages", []) if m["ts"] != ts)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return
            params["cursor"] = cursor

    def profile(self, user_id):
        """The member's full name and avatar URL. Empty when either is missing."""
        profile = self.call("users.info", params={"user": user_id}).get("user", {}).get("profile", {})
        name, image = profile.get("real_name") or "", profile.get("image_72") or ""
        return {"name": name, "image": image} if name and image else {}

    def user_by_email(self, email):
        resp = self.call("users.lookupByEmail", params={"email": email}, ignore=("users_not_found",))
        return resp.get("user", {}).get("id", "")

    def users(self):
        """Yield (id, names) for every active human member, names being their real and display names."""
        params = {"limit": 200}
        while True:
            resp = self.call("users.list", params=params)
            for member in resp.get("members", []):
                if member.get("deleted") or member.get("is_bot") or member.get("id") == "USLACKBOT":
                    continue
                profile = member.get("profile", {})
                names = {member.get("real_name"), profile.get("real_name"), profile.get("display_name")}
                yield member["id"], {n for n in names if n}
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return
            params["cursor"] = cursor

    def announcements(self, channel, now, since=None, threads_of=None):
        """Yield (ts, payload) for every announcement the bot posted since `since`, or in the last HISTORY_DAYS.

        With `threads_of` a repo, the stacked PRs posted in that repo's threads follow the channel's own."""
        if self.bot_id is None:
            self.bot_id = self.call("auth.test").get("bot_id", "")
        # Threads cost a call each, so they are read only after the channel missed.
        threads = []
        yield from self.channel_announcements(channel, now, since, threads_of, threads)
        for ts in threads:
            yield from self.thread_announcements(channel, ts)

    def channel_announcements(self, channel, now, since, threads_of, threads):
        # Newest first, stopping at the cutoff ourselves: with `oldest` set, Slack pages from the
        # oldest end and has returned empty pages, which reads as "never announced".
        cutoff = since if since is not None else now - HISTORY_DAYS * 86400
        params = {"channel": channel, "limit": 200}
        while True:
            resp = self.call("conversations.history", params=params)
            for message in resp.get("messages", []):
                if float(message["ts"]) < cutoff:
                    return
                # A withdrawn PR's deleted message leaves a tombstone that still holds its stack.
                if threads_of and message.get("subtype") == "tombstone" and message.get("reply_count"):
                    threads.append(message["ts"])
                    continue
                if message.get("bot_id") != self.bot_id:
                    continue
                payload = parse_announcement(message)
                if payload is None:
                    continue
                if payload.get("repo") == threads_of and message.get("reply_count") and has_reaction(message, STACK_REACTION):
                    threads.append(message["ts"])
                yield message["ts"], payload
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return
            params["cursor"] = cursor

    def thread_announcements(self, channel, ts):
        resp = self.call("conversations.replies", params={"channel": channel, "ts": ts, "limit": 200},
                         ignore=("thread_not_found", "message_not_found"))
        for reply in resp.get("messages", [])[1:]:
            if reply.get("bot_id") == self.bot_id and (payload := parse_announcement(reply)) is not None:
                yield reply["ts"], payload


class GitHub:
    """The GitHub API calls the announcer makes."""

    def __init__(self, token, base_url="https://api.github.com/"):
        self.token = token
        self.base_url = base_url
        self.profiles = {}

    def get(self, path):
        return request_json(self.base_url + path, token=self.token,
                            headers={"Accept": "application/vnd.github+json"})

    def pull_request(self, repo, number):
        return self.get(f"repos/{repo}/pulls/{number}")

    def open_pulls_from(self, repo, branch):
        """Open PRs whose head is `branch` in the repo itself."""
        owner = repo.partition("/")[0]
        return self.get(f"repos/{repo}/pulls?state=open&head={urllib.parse.quote(f'{owner}:{branch}')}")

    def open_pulls_into(self, repo, branch):
        """Open PRs whose base is `branch`."""
        return self.get(f"repos/{repo}/pulls?state=open&base={urllib.parse.quote(branch)}")

    def approved(self, repo, number):
        return "APPROVED" in self.decisions(repo, number)

    def changes_requested(self, repo, number):
        return "CHANGES_REQUESTED" in self.decisions(repo, number)

    def decisions(self, repo, number):
        return set(self.reviewer_states(repo, number).values())

    def reviewer_states(self, repo, number):
        """Each reviewer's current state: their latest decision, else COMMENTED if they only commented."""
        states = {}
        page = 1
        while True:
            reviews = self.get(f"repos/{repo}/pulls/{number}/reviews?per_page=100&page={page}")
            for review in reviews:
                login = review["user"]["login"]
                if review["state"] != "COMMENTED" or login not in states:
                    states[login] = review["state"]
            if len(reviews) < 100:
                return states
            page += 1

    def profile(self, login):
        """The user's public name and email. A workflow token can't see org-verified emails."""
        if login not in self.profiles:
            try:
                user = self.get(f"users/{login}")
            except APIError as err:
                log(f"profile of {login}: {err}")
                return {}
            self.profiles[login] = {"name": user.get("name") or "", "email": user.get("email") or ""}
        return self.profiles[login]


@dataclass
class Config:
    channel: str
    authors: set = field(default_factory=set)
    # Logins whose PRs are never announced, even when listed in `authors`.
    excluded_authors: set = field(default_factory=set)
    # PRs opened before this Unix time are left alone entirely. None means no cutoff.
    opened_after: float = None
    overrides: dict = field(default_factory=dict)
    # GitHub team slug -> member logins, for teams whose review request should ping their members.
    teams: dict = field(default_factory=dict)
    remind_after: timedelta = timedelta(hours=24)
    timezone: ZoneInfo = ZoneInfo("Asia/Ho_Chi_Minh")
    # No reminders before this day, in `timezone`. None means never paused.
    reminders_paused_until: date = None


def log(message):
    print(message, file=sys.stderr)


def team_slugs(pr):
    return [team["slug"] for team in pr.get("requested_teams") or []]


def event_teams(event):
    """Requested team slugs from the event's PR, or None when the event doesn't list them."""
    pr = event.get("pull_request") or {}
    return team_slugs(pr) if "requested_teams" in pr else None


def encode_state(payload):
    return STATE_PREFIX + json.dumps(payload, separators=(",", ":"))


def parse_announcement(message):
    """The state of a bot message announcing a PR, or None for any other message."""
    blocks = message.get("blocks") or []
    for block in blocks:
        block_id = block.get("block_id", "")
        if block_id.startswith(STATE_PREFIX):
            return json.loads(block_id[len(STATE_PREFIX):])

    texts = [block.get("text", {}).get("text", "") for block in blocks[:1]] + [message.get("text", "")]
    for text in texts:
        if match := LEGACY_LINK.match(text):
            return {"repo": match.group(1), "number": int(match.group(2)), "state": STATE_OPEN,
                    "ready_at": int(float(message["ts"])), "reminded": False, "approved": False}
    return None


def has_reaction(message, name):
    return any(reaction.get("name") == name for reaction in message.get("reactions") or [])


def reminds_about(message, number, thread=None, only=False):
    """Whether a bot reply is a reminder about PR `number`, or only about it with `only`. Older
    reminders carry no numbers and belong to the thread's own PR."""
    if not message.get("text", "").startswith(REMINDER_MARK):
        return False
    for block in message.get("blocks") or []:
        block_id = block.get("block_id", "")
        if block_id.startswith(REMINDER_PREFIX):
            numbers = json.loads(block_id[len(REMINDER_PREFIX):])
            return numbers == [number] if only else number in numbers
    return thread is None


def parse_time(value):
    """A GitHub timestamp, or a YYYY-MM-DD date taken as midnight UTC, as Unix time."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def distinct(logins, exclude):
    """Logins once each, case-insensitively, without `exclude`."""
    seen, result = {exclude.lower()}, []
    for login in logins:
        if login.lower() not in seen:
            seen.add(login.lower())
            result.append(login)
    return result


def normalize(name):
    return " ".join(name.lower().split())


def escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def human_duration(delta):
    # Hours up to two days, so 38 hours doesn't read as "1 day".
    seconds = delta.total_seconds()
    hours = int(seconds // 3600)
    unit, n = ("day", int((seconds + 43200) // 86400)) if hours >= 48 else ("hour", hours)
    return f"1 {unit}" if n == 1 else f"{n} {unit}s"


def pr_state(pr):
    if pr["state"] == "closed":
        return STATE_CLOSED
    return STATE_DRAFT if pr.get("draft") else STATE_OPEN


def withdrawn(pr):
    """Drafts and PRs closed without merging leave the channel. Merged ones stay as a record."""
    return pr_state(pr) == STATE_DRAFT or (pr_state(pr) == STATE_CLOSED and not pr.get("merged"))


def is_bot(pr):
    return is_bot_user(pr["user"])


def is_bot_user(user):
    return user.get("type") == "Bot" or user["login"].endswith("[bot]")


class Announcer:
    def __init__(self, config, slack, github, now=time.time):
        self.config = config
        self.slack = slack
        self.github = github
        self.now = now
        self.mentions = {}
        self.slack_names = None
        self.profiles = {}

    # People

    def slack_id(self, login):
        key = login.lower()
        override = self.config.overrides.get(key, "")
        if override and "@" not in override:
            return override
        if key not in self.mentions:
            self.mentions[key] = self.lookup_email(login, override) if override else self.lookup(login)
        return self.mentions[key]

    def mention(self, login):
        slack_id = self.slack_id(login)
        return f"<@{slack_id}>" if slack_id else f"`{login}`"

    def profile(self, login):
        """The person's Slack full name and avatar, or empty when they can't be found."""
        slack_id = self.slack_id(login)
        if not slack_id:
            return {}
        if slack_id not in self.profiles:
            try:
                self.profiles[slack_id] = self.slack.profile(slack_id)
            except APIError as err:
                log(f"slack profile of {login}: {err}")
                return {}
        return self.profiles[slack_id]

    def sender(self, user):
        """Who to post a PR as: the author's Slack name and avatar, else their GitHub ones, else nobody."""
        if profile := self.profile(user["login"]):
            return profile
        if not user.get("avatar_url"):
            return {}
        name = self.github.profile(user["login"]).get("name") or user["login"]
        return {"name": name, "image": user["avatar_url"]}

    def lookup(self, login):
        """Match by public email first, then by a GitHub name exactly one Slack member carries."""
        profile = self.github.profile(login)
        try:
            if profile.get("email") and (slack_id := self.slack.user_by_email(profile["email"])):
                return slack_id
            if profile.get("name") and (slack_id := self.user_by_name(profile["name"])):
                return slack_id
        except APIError as err:
            log(f"slack lookup of {login}: {err}")
            return ""
        log(f"no Slack user for {login}")
        return ""

    def lookup_email(self, login, email):
        """An override naming the person's Slack email instead of their Slack ID."""
        try:
            if slack_id := self.slack.user_by_email(email):
                return slack_id
        except APIError as err:
            log(f"slack lookup of {login}: {err}")
            return ""
        log(f"no Slack user with the override email for {login}")
        return ""

    def user_by_name(self, name):
        if self.slack_names is None:
            self.slack_names = list(self.slack.users())
        wanted = normalize(name)
        matches = {slack_id for slack_id, names in self.slack_names if wanted in {normalize(n) for n in names}}
        return matches.pop() if len(matches) == 1 else ""

    def requested(self, pr, payload, reviewed=()):
        """Requested reviewers, with listed teams expanded to their members. Teams not in the file stay
        names, but only when no person was asked or has reviewed, so a CODEOWNERS team isn't shown next to them."""
        people = [u["login"] for u in pr.get("requested_reviewers") or [] if not is_bot_user(u)]
        logins, unlisted = [u["login"] for u in pr.get("requested_reviewers") or []], []
        for slug in team_slugs(pr) or payload.get("teams") or []:
            if members := self.config.teams.get(slug.lower()):
                logins += members
                people += members
            else:
                unlisted.append(f"`@{slug}`")
        if distinct(people + list(reviewed), exclude=pr["user"]["login"]):
            unlisted = []
        return logins, unlisted

    def reviewers(self, pr, payload):
        logins, unlisted = self.requested(pr, payload)
        return [self.mention(login) for login in distinct(logins, exclude=pr["user"]["login"])] + unlisted

    def owners(self, pr):
        logins = [u["login"] for u in pr.get("assignees") or []] or [pr["user"]["login"]]
        return [self.mention(login) for login in logins]

    # Rendering

    def render(self, pr, payload):
        """PR link, author and title, then its reviewers in small text."""
        link = f"<{pr['html_url']}|{payload['repo'].rpartition('/')[2]}#{pr['number']}>"
        # Mentioned even when posted as the author: the avatar alone doesn't notify them or make them
        # follow the thread.
        lines = [f"*{link}* ({self.mention(pr['user']['login'])}) {escape(pr['title'])}"]

        text = f"PR: {pr['html_url']} {escape(pr['title'])}"
        blocks = [{"type": "section", "block_id": encode_state(payload),
                   "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
        if reviewers := self.reviewer_line(pr, payload):
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": reviewers}]})
        return text, blocks

    def reviewer_line(self, pr, payload):
        """Requested reviewers, then those who reviewed marked by their review, once each and without the author."""
        author = pr["user"]["login"]
        # A failed read only loses the marks, never the post.
        try:
            states = self.github.reviewer_states(payload["repo"], pr["number"])
        except APIError as err:
            log(f"reviews of {pr['html_url']}: {err}")
            states = {}
        humans = [login for login in states if not login.endswith("[bot]") and states[login] in REVIEW_MARKS]
        requested, unlisted = self.requested(pr, payload, reviewed=humans)
        requested = distinct(requested, exclude=author)
        # A reviewer asked again after reviewing is waiting on them, so shows without the mark.
        waiting = {login.lower() for login in requested}
        reviewed = [login for login in distinct(states, exclude=author)
                    if login.lower() not in waiting and not login.endswith("[bot]") and states[login] in REVIEW_MARKS]
        return " ".join([self.mention(login) for login in requested] + unlisted
                        + [f"{self.mention(login)} {REVIEW_MARKS[states[login]]}" for login in reviewed])

    def reminder(self, pr, payload, waited):
        owners = ", ".join(self.owners(pr))
        reviewers = self.reviewers(pr, payload)
        if not reviewers:
            return (f"{REMINDER_MARK} This PR has been waiting {human_duration(waited)} with no reviewer requested. "
                    f"{owners}, please request a review, or mark it as draft if it isn't ready.")
        return (f"{REMINDER_MARK} This PR has been waiting {human_duration(waited)} for review. "
                f"{', '.join(reviewers)}, please take a look. "
                f"{owners}, if it isn't ready for review, please mark it as draft.")

    def stack_reminder(self, waiting):
        """One reminder for the PRs of a stack's thread that are waiting, mentioning everyone they wait on."""
        each = [self.reviewers(pr, payload) for pr, payload in waiting]
        reviewers = distinct([r for pr_reviewers in each for r in pr_reviewers], exclude="")
        owners = ", ".join(distinct([o for pr, _ in waiting for o in self.owners(pr)], exclude=""))
        unrequested = not all(each)
        text = f"{REMINDER_MARK} Some PRs in this thread are still waiting for review."
        if reviewers:
            text += f" {', '.join(reviewers)}, please take a look."
        if unrequested:
            return text + f" {owners}, please request a review where none is set, and mark any that aren't ready as draft."
        return text + f" {owners}, if any isn't ready for review, please mark it as draft."

    # State

    def find(self, repo, number, pr):
        # A PR's message can't predate the PR, nor its stack's thread by more than STACK_WINDOW.
        since = parse_time(pr["created_at"]) - 3600 - STACK_WINDOW
        for ts, payload in self.slack.announcements(self.config.channel, self.now(), since, threads_of=repo):
            if payload.get("repo") == repo and payload.get("number") == number:
                return ts, payload
        return None, None

    def stack_thread(self, repo, pr, stack):
        """The earliest thread of the stack's announced PRs that started within STACK_WINDOW, or None.
        The earliest, so runs posting at once all settle on the same thread."""
        if not stack:
            return None
        # A failed lookup only loses the grouping, never the post.
        try:
            threads = [payload.get("thread") or ts
                       for ts, payload in self.slack.announcements(self.config.channel, self.now(),
                                                                   self.now() - STACK_WINDOW, threads_of=repo)
                       if payload.get("repo") == repo and payload.get("number") in stack]
        except APIError as err:
            log(f"stack thread of {pr['html_url']}: {err}")
            return None
        return min((t for t in threads if self.now() - float(t) < STACK_WINDOW), key=float, default=None)

    def join(self, thread, pr, payload):
        """Mark the thread as holding a stack and point the payload at it. None when it can't be marked."""
        if thread is None:
            return None
        # Marked before the reply, so a reply always sits in a thread that lookups read.
        try:
            self.slack.react(self.config.channel, thread, STACK_REACTION, add=True)
        except APIError as err:
            log(f"stack mark for {pr['html_url']}: {err}")
            return None
        payload["thread"] = thread
        return thread

    def regroup(self, repo, pr, payload, ts, stack, sender):
        """Move a PR just posted to the channel into the thread of a stack PR that posted alongside it.
        Of PRs made ready at once each posts to the channel, and all but the earliest move."""
        time.sleep(SETTLE_SECONDS)
        thread = self.stack_thread(repo, pr, stack)
        if thread is None or float(thread) >= float(ts) or self.join(thread, pr, payload) is None:
            return

        # Deleted first: a failed repost leaves the PR unannounced, and its next event posts it.
        self.slack.delete(self.config.channel, ts, pr["number"])
        self.remember_teams(payload, payload.get("teams"))
        text, blocks = self.render(pr, payload)
        self.slack.post(self.config.channel, text, blocks, thread_ts=thread, sender=sender)
        log(f"moved {pr['html_url']} into its stack's thread")

    def stack(self, repo, pr):
        """Numbers of the other open PRs in the PR's stack: PRs by the same author whose branches chain into
        or out of this one's. A PR into a shared branch, like a release one, isn't part of that branch's PR."""
        author = pr["user"]["login"].lower()
        seen, queue = {pr["number"]}, [pr]
        while queue and len(seen) < STACK_LIMIT:
            current = queue.pop(0)
            head, base = current["head"], current["base"]
            default = (base.get("repo") or {}).get("default_branch")
            neighbours = []
            # A fork's branch name means nothing in this repo, and a deleted fork leaves no repo at all.
            if (head.get("repo") or {}).get("full_name") == repo and head["ref"] != default:
                neighbours += self.github.open_pulls_into(repo, head["ref"])
            if base["ref"] != default:
                neighbours += self.github.open_pulls_from(repo, base["ref"])
            for other in neighbours:
                if len(seen) >= STACK_LIMIT:
                    break
                if other["number"] not in seen and other["user"]["login"].lower() == author:
                    seen.add(other["number"])
                    queue.append(other)
        return seen - {pr["number"]}

    def settle(self, ts, payload, pr):
        """Delete the message of a withdrawn PR, or save the PR's current state to it."""
        if withdrawn(pr):
            self.slack.delete(self.config.channel, ts, pr["number"], payload.get("thread"))
            log(f"withdrew {pr['html_url']}")
            return

        self.save(ts, payload, pr)

    def save(self, ts, payload, pr):
        # A merged PR gets its reaction once, whichever run sees the merge first.
        if pr.get("merged") and not payload.get("merged"):
            # A failed reaction must not stop the message from updating.
            try:
                self.slack.react(self.config.channel, ts, MERGED_REACTION, add=True)
                payload["merged"] = True
            except APIError as err:
                log(f"merged reaction for {pr['html_url']}: {err}")
                # A missing emoji won't appear on a retry. Other failures are retried by the next run.
                if err.code == "invalid_name":
                    payload["merged"] = True

        text, blocks = self.render(pr, payload)
        self.slack.update(self.config.channel, ts, text, blocks)

    def announced(self, pr):
        if is_bot(pr):
            return False
        login = pr["user"]["login"].lower()
        if login in self.config.excluded_authors:
            return False
        if self.config.authors and login not in self.config.authors:
            return False
        return self.config.opened_after is None or parse_time(pr["created_at"]) >= self.config.opened_after

    # Entry points

    def on_pull_request(self, repo, number, action, teams=None):
        """`teams` are the requested team slugs from the event, or None when the event didn't say."""
        if action not in PR_ACTIONS:
            return
        # Runs can be skipped or reordered, so act on the PR as it is now, not as the event saw it.
        pr = self.github.pull_request(repo, number)
        if not self.announced(pr):
            return

        ts, payload = self.find(repo, number, pr)
        state = pr_state(pr)

        # Any event may post: a newer run can replace the queued ready_for_review one.
        if ts is None:
            if state != STATE_OPEN:
                return
            # A duplicate post is the costliest mistake, so a miss is confirmed before posting.
            time.sleep(RECHECK_SECONDS)
            ts, payload = self.find(repo, number, pr)
        if ts is None:
            payload = {"repo": repo, "number": number, "state": state, "ready_at": int(self.now()),
                       "reminded": False, "approved": False}
            try:
                stack = self.stack(repo, pr)
            except APIError as err:
                log(f"stack of {pr['html_url']}: {err}")
                stack = set()
            thread = self.join(self.stack_thread(repo, pr, stack), pr, payload)
            self.remember_teams(payload, teams)
            text, blocks = self.render(pr, payload)
            # Posted as the author when their name and avatar are known, otherwise as the app.
            sender = self.sender(pr["user"])
            _, ts = self.slack.post(self.config.channel, text, blocks, thread_ts=thread, sender=sender)
            log(f"announced {pr['html_url']}")
            if stack and thread is None and not self.slack.dry_run:
                self.regroup(repo, pr, payload, ts, stack, sender)
            return

        # Back from draft, or reopened, restarts the reminder. A repeated event doesn't.
        if state == STATE_OPEN and payload.get("state") != STATE_OPEN:
            payload.update(ready_at=int(self.now()), reminded=False)
        payload["state"] = state
        self.remember_teams(payload, teams)
        self.settle(ts, payload, pr)

    def on_review(self, repo, number, action, teams=None):
        if action not in REVIEW_ACTIONS:
            return
        # The review run may have replaced the queued run that would have announced the PR.
        self.on_pull_request(repo, number, "edited", teams)
        pr = self.github.pull_request(repo, number)
        ts, payload = self.find(repo, number, pr)
        if ts is None:
            return
        self.sync_approval(ts, payload, self.github.approved(repo, number))
        self.remember_teams(payload, teams)
        # A submitted review drops its author from the requested reviewers without a pull_request event.
        self.save(ts, payload, pr)

    def remember_teams(self, payload, teams):
        """Keep the event's team requests in the state, since GITHUB_TOKEN can't see teams on the PR."""
        if teams is None:
            return

        # The state must fit Slack's block ID limit, so listed teams go first and the rest while they fit.
        # The fit is checked at the longest state, so a later state change can't push it over.
        kept = []
        for slug in sorted(teams, key=lambda slug: slug.lower() not in self.config.teams):
            if len(encode_state({**payload, "state": STATE_CLOSED, "merged": True, "reminded": REMINDED_PLACEHOLDER,
                                         "teams": kept + [slug]})) <= BLOCK_ID_LIMIT:
                kept.append(slug)

        if kept:
            payload["teams"] = kept
        else:
            payload.pop("teams", None)

    def sync_approval(self, ts, payload, approved):
        """Add or remove the reaction to match the approval. The caller saves the payload."""
        if bool(payload.get("approved")) == approved:
            return
        self.slack.react(self.config.channel, ts, APPROVED_REACTION, add=approved)
        payload["approved"] = approved

    def local_now(self):
        return datetime.fromtimestamp(self.now(), self.config.timezone)

    def weekday(self):
        return self.local_now().weekday() < 5

    def reminders_paused(self):
        paused_until = self.config.reminders_paused_until
        return paused_until is not None and self.local_now().date() < paused_until

    def today_start(self):
        return self.local_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def due(self, payload):
        """Whether the PR is due a reminder: ready long enough, then again each day after its last one.
        `reminded` holds the last reminder's Unix time; an older True reads as reminded long ago."""
        if reminded := payload.get("reminded"):
            return reminded < self.today_start()
        waited = self.now() - payload.get("ready_at", self.now())
        return waited >= self.config.remind_after.total_seconds() - REMIND_GRACE

    def sweep(self, repo):
        """Remind about the repo's due PRs. Each repo sweeps only its own, since its token can't read others."""
        # The schedule is weekday mornings; this keeps a manual weekend run quiet too.
        if not self.weekday():
            log("weekend, no reminders")
            return

        if self.reminders_paused():
            log(f"reminders paused until {self.config.reminders_paused_until}, only settling PRs")

        # A stack's PRs share a thread, and get one reminder in it between them.
        sizes, due = {}, {}
        for ts, payload in list(self.slack.announcements(self.config.channel, self.now(), threads_of=repo)):
            if payload.get("repo") != repo:
                continue
            thread = payload.get("thread") or ts
            sizes[thread] = sizes.get(thread, 0) + 1
            if payload.get("state") != STATE_OPEN or payload.get("approved") or not self.due(payload):
                continue
            due.setdefault(thread, []).append((ts, payload))

        for thread, entries in due.items():
            try:
                self.remind(thread, entries, shared=sizes[thread] > 1)
            except APIError as err:
                log(f"reminder in thread {thread}: {err}")

    def remind(self, thread, entries, shared=False):
        """Reply in the thread about its PRs still waiting. `shared` is a thread holding several PRs."""
        waiting = []
        for ts, payload in entries:
            # One PR failing must not stop the others' reminder.
            try:
                if found := self.waiting(ts, payload):
                    waiting.append(found)
            except APIError as err:
                log(f"reminder for {payload.get('repo')}#{payload.get('number')}: {err}")
        if not waiting:
            return

        if shared:
            text = self.stack_reminder(waiting)
        else:
            pr, payload = waiting[0]
            text = self.reminder(pr, payload, timedelta(seconds=self.now() - payload["ready_at"]))
        numbers = [payload["number"] for _, payload in waiting]
        blocks = [{"type": "section", "block_id": REMINDER_PREFIX + json.dumps(numbers, separators=(",", ":")),
                   "text": {"type": "mrkdwn", "text": text}}]
        self.slack.post(self.config.channel, text, blocks, thread_ts=thread)
        log(f"reminded about {', '.join(pr['html_url'] for pr, _ in waiting)}")

    def waiting(self, ts, payload):
        """(pr, payload) when the PR is still waiting for review and due a reminder, recording the reminder."""
        repo, number = payload["repo"], payload["number"]
        pr = self.github.pull_request(repo, number)
        if not self.announced(pr):
            return None

        # An event run may have saved since the sweep listed the channel; act on its state.
        ts, payload = self.find(repo, number, pr)
        if ts is None or payload.get("state") != STATE_OPEN or not self.due(payload):
            return None

        # A close, draft or approval the event run missed settles the PR here instead of a reminder.
        if pr_state(pr) != STATE_OPEN:
            # Read again just before acting, since an event run may have made it ready meanwhile.
            pr = self.github.pull_request(repo, number)
        if pr_state(pr) != STATE_OPEN:
            payload["state"] = pr_state(pr)
            self.settle(ts, payload, pr)
            return None

        approved = self.github.approved(repo, number)
        self.sync_approval(ts, payload, approved)
        if approved:
            self.save(ts, payload, pr)
            return None

        # Requested changes leave the PR with its author, so nobody is nudged until they are addressed.
        if self.github.changes_requested(repo, number):
            return None

        if self.reminders_paused():
            return None

        # Recorded first: a reminder lost to a Slack error beats one repeated every hour. The thread
        # check covers an event run that saved a stale `reminded` over it.
        payload["reminded"] = int(self.now())
        # States saved with `reminded: false` may only fit their teams without the longer timestamp.
        self.remember_teams(payload, payload.get("teams"))
        self.save(ts, payload, pr)
        thread = payload.get("thread")
        since = max(payload["ready_at"], self.today_start())
        if self.slack.has_reminder(self.config.channel, ts, since, number, thread):
            return None
        return pr, payload


def load_overrides(path):
    overrides = {}
    if not path or not os.path.exists(path):
        return overrides
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            login, sep, slack_id = line.partition("=")
            if not sep or not login.strip() or not slack_id.strip():
                raise ValueError(f"slack user override {line!r} is not in login=USERID or login=email form")
            overrides[login.strip().lower()] = slack_id.strip()
    return overrides


def load_teams(path):
    teams = {}
    if not path or not os.path.exists(path):
        return teams
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            team, sep, members = line.partition("=")
            logins = [m.strip() for m in members.split(",") if m.strip()]
            if not sep or not team.strip() or not logins:
                raise ValueError(f"slack team line {line!r} is not in team=login1,login2 form")
            teams[team.strip().lower()] = logins
    return teams


def load_excluded_authors(path):
    if not path or not os.path.exists(path):
        return set()
    excluded = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not re.fullmatch(r"[A-Za-z0-9-]+", line):
                raise ValueError(f"excluded author {line!r} is not a bare GitHub login")
            excluded.add(line.lower())
    return excluded


def parse_paused_until(value):
    """The pause date, or None. A bad value is logged and ignored so announcements keep running."""
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        log(f"::warning::ignoring reminders pause {value!r}, it is not a YYYY-MM-DD date")
        return None


def load_config(env):
    return Config(
        channel=env["SLACK_CHANNEL"],
        authors={a.strip().lower() for a in env.get("PR_AUTHORS", "").split(",") if a.strip()},
        excluded_authors=load_excluded_authors(env.get("EXCLUDED_AUTHORS_FILE")),
        opened_after=parse_time(env["OPENED_AFTER"]) if env.get("OPENED_AFTER") else None,
        overrides=load_overrides(env.get("SLACK_USERS_FILE")),
        teams=load_teams(env.get("SLACK_TEAMS_FILE")),
        remind_after=timedelta(hours=float(env.get("REMIND_AFTER_HOURS") or 24)),
        timezone=ZoneInfo(env.get("REMINDER_TZ") or "Asia/Ho_Chi_Minh"),
        reminders_paused_until=parse_paused_until(env.get("REMINDERS_PAUSED_UNTIL", "")),
    )


def main(env=os.environ):
    announcer = Announcer(
        load_config(env),
        Slack(env["SLACK_TOKEN"], dry_run=env.get("DRY_RUN", "").strip().lower() == "true"),
        GitHub(env["GITHUB_TOKEN"]),
    )

    event_name = env["GITHUB_EVENT_NAME"]
    if event_name in ("schedule", "workflow_dispatch"):
        announcer.sweep(env["GITHUB_REPOSITORY"])
        return

    with open(env["GITHUB_EVENT_PATH"]) as f:
        event = json.load(f)
    repo, number, action = event["repository"]["full_name"], event["pull_request"]["number"], event["action"]
    # Webhook payloads list team requests that the API hides from GITHUB_TOKEN.
    teams = event_teams(event)

    if event_name == "pull_request":
        announcer.on_pull_request(repo, number, action, teams)
    elif event_name == "pull_request_review":
        announcer.on_review(repo, number, action, teams)


if __name__ == "__main__":
    main()
