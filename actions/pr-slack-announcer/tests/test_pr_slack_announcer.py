import copy
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pr_slack_announcer as psa  # noqa: E402

psa.RECHECK_SECONDS = 0
psa.SETTLE_SECONDS = 0

REPO = "iomete/infra"
# 12:30 on a Monday in Asia/Ho_Chi_Minh.
MONDAY_MORNING = datetime(2026, 10, 5, 5, 30, tzinfo=timezone.utc).timestamp()


class FakeSlack:
    def __init__(self):
        self.messages = []  # dicts: ts, text, blocks, thread_ts, bot_id
        self.posted = 0
        self.dry_run = False
        # Added to the counter timestamps, for tests that compare them with the clock.
        self.ts_base = 0
        # The test's clock, recorded on each post so reminder lookups can compare times.
        self.clock = lambda: 0
        self.reactions = []
        self.by_email = {"rita@iomete.com": "URITA", "alex@iomete.com": "UALEX", "ann@iomete.com": "UANN"}
        self.profiles = {"UANN": {"name": "Ann Smith", "image": "https://avatars.slack-edge.com/ann_72.png"}}

    def post(self, channel, text, blocks=None, thread_ts=None, sender=None):
        self.posted += 1
        ts = f"{self.ts_base + self.posted}.000"
        self.messages.append({"ts": ts, "text": text, "blocks": copy.deepcopy(blocks),
                              "thread_ts": thread_ts, "bot_id": "BBOT", "sender": sender, "at": self.clock()})
        return "C123", ts

    def update(self, channel, ts, text, blocks):
        self.by_ts(ts).update(text=text, blocks=copy.deepcopy(blocks))

    def delete(self, channel, ts, number, thread=None):
        self.messages = [m for m in self.messages if m["ts"] != ts and not (
            m.get("thread_ts") == (thread or ts) and m.get("bot_id") == "BBOT" and psa.reminds_about(m, number, thread, only=True))]

    def react(self, channel, ts, name, add):
        self.reactions.append((ts, name, add))

    def profile(self, user_id):
        return self.profiles.get(user_id, {})

    def user_by_email(self, email):
        return self.by_email.get(email, "")

    def users(self):
        return [("UKANAN", {"Kanan Rahimov", "kenan"}), ("UUJJ", {"Ujjawal Khare"}),
                ("USAM1", {"Sam Lee"}), ("USAM2", {"Sam  Lee"})]

    def has_reminder(self, channel, ts, since, number, thread=None):
        return any(m["thread_ts"] == (thread or ts) and psa.reminds_about(m, number, thread) and m["at"] >= since
                   for m in self.messages)

    def announcements(self, channel, now, since=None, threads_of=None):
        for message in reversed(self.messages):
            if message.get("bot_id") != "BBOT":
                continue
            payload = psa.parse_announcement(message)
            if payload is not None:
                yield message["ts"], payload

    def by_ts(self, ts):
        return next(m for m in self.messages if m["ts"] == ts)

    def body(self, ts="1.000"):
        return self.by_ts(ts)["blocks"][0]["text"]["text"]

    def reviewers(self, ts="1.000"):
        blocks = self.by_ts(ts)["blocks"]
        return blocks[1]["elements"][0]["text"] if len(blocks) > 1 else ""

    def payload(self, ts="1.000"):
        return psa.parse_announcement(self.by_ts(ts))


class FakeGitHub:
    def __init__(self):
        self.prs = {}
        self.approvals = {}
        self.states = {}
        self.changes = {}
        self.profiles = {
            "rita": {"name": "Rita", "email": "rita@iomete.com"},
            "alex": {"name": "", "email": "alex@iomete.com"},
            "ann": {"name": "Ann", "email": "ann@iomete.com"},
            "KenanBek": {"name": "Kanan Rahimov", "email": "kenan@example.com"},
            "ujjawal": {"name": "ujjawal KHARE", "email": ""},
            "sam": {"name": "Sam Lee", "email": ""},
        }

    def pull_request(self, repo, number):
        return copy.deepcopy(self.prs[number])

    def open_pulls_from(self, repo, branch):
        return [copy.deepcopy(pr) for pr in self.prs.values() if pr["head"]["ref"] == branch and pr["state"] == "open"]

    def open_pulls_into(self, repo, branch):
        return [copy.deepcopy(pr) for pr in self.prs.values() if pr["base"]["ref"] == branch and pr["state"] == "open"]

    def approved(self, repo, number):
        return self.approvals.get(number, False)

    def reviewer_states(self, repo, number):
        return self.states.get(number, {})

    def changes_requested(self, repo, number):
        return self.changes.get(number, False)

    def profile(self, login):
        return self.profiles.get(login, {})


def new_pr(number=1, author="ann", **changes):
    pr = {
        "number": number,
        "title": "Add <cool> feature & more",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "state": "open",
        "draft": False,
        "merged": False,
        "user": {"login": author, "type": "User"},
        "requested_reviewers": [{"login": "rita"}],
        "requested_teams": [],
        "assignees": [{"login": "alex"}],
        "base": {"ref": "main", "repo": {"full_name": REPO, "default_branch": "main"}},
        "head": {"ref": f"branch-{number}", "repo": {"full_name": REPO}},
        "created_at": "2026-10-01T00:00:00Z",
    }
    pr.update(changes)
    return pr


class AnnouncerTest(unittest.TestCase):
    def setUp(self):
        self.slack = FakeSlack()
        self.github = FakeGitHub()
        self.clock = MONDAY_MORNING
        self.slack.clock = lambda: self.clock
        self.announcer = psa.Announcer(psa.Config(channel="C123"), self.slack, self.github, now=lambda: self.clock)

    def event(self, action, number=1, **changes):
        self.github.prs.setdefault(number, new_pr(number))
        self.github.prs[number].update(changes)
        self.announcer.on_pull_request(REPO, number, action)

    def test_announces_ready_pr(self):
        self.event("opened")

        self.assertEqual(len(self.slack.messages), 1)
        self.assertEqual(self.slack.by_ts("1.000")["sender"],
                         {"name": "Ann Smith", "image": "https://avatars.slack-edge.com/ann_72.png"})
        self.assertEqual(self.slack.body(),
                         "*<https://github.com/iomete/infra/pull/1|infra#1>* (<@UANN>) Add &lt;cool&gt; feature &amp; more")
        self.assertEqual(self.slack.reviewers(), "<@URITA>")
        self.assertEqual(self.slack.payload()["ready_at"], int(MONDAY_MORNING))

    def test_author_without_slack_profile_is_mentioned(self):
        self.slack.profiles = {}
        self.event("opened")

        self.assertFalse(self.slack.by_ts("1.000")["sender"])
        self.assertEqual(self.slack.body(),
                         "*<https://github.com/iomete/infra/pull/1|infra#1>* (<@UANN>) Add &lt;cool&gt; feature &amp; more")
        self.assertEqual(self.slack.reviewers(), "<@URITA>")

    def test_message_posted_as_author_before_mentions_gains_one_on_update(self):
        payload = {"repo": REPO, "number": 1, "state": psa.STATE_OPEN, "ready_at": int(MONDAY_MORNING),
                   "reminded": False, "approved": False, "as_author": True}
        self.slack.messages.append({"ts": "1.000", "bot_id": "BBOT", "thread_ts": None, "text": "", "blocks": [
            {"type": "section", "block_id": psa.encode_state(payload),
             "text": {"type": "mrkdwn", "text": "*<https://github.com/iomete/infra/pull/1|infra#1>* Add cool feature"}}]})

        self.event("edited")

        self.assertEqual(len(self.slack.messages), 1)
        self.assertIn("* (<@UANN>) ", self.slack.body())

    def test_author_without_slack_match_is_posted_as_github_user(self):
        avatar = "https://avatars.githubusercontent.com/u/1"
        self.github.profiles["stranger"] = {"name": "Stan Ranger", "email": ""}
        self.github.prs[1] = new_pr(1, author="stranger", user={"login": "stranger", "type": "User", "avatar_url": avatar})
        self.github.prs[2] = new_pr(2, author="nameless", user={"login": "nameless", "type": "User", "avatar_url": avatar})

        self.announcer.on_pull_request(REPO, 1, "opened")
        self.announcer.on_pull_request(REPO, 2, "opened")

        self.assertEqual(self.slack.by_ts("1.000")["sender"], {"name": "Stan Ranger", "image": avatar})
        self.assertEqual(self.slack.by_ts("2.000")["sender"], {"name": "nameless", "image": avatar})
        self.assertIn("* (`nameless`) ", self.slack.body("2.000"))

    def test_unmatched_author_without_avatar_is_shown_by_login(self):
        self.github.prs[1] = new_pr(1, author="stranger", user={"login": "stranger", "type": "User"})

        self.announcer.on_pull_request(REPO, 1, "opened")

        self.assertIn("* (`stranger`) ", self.slack.body())

    def test_skips_drafts_bots_and_unlisted_authors(self):
        self.github.prs[1] = new_pr(1, draft=True)
        self.github.prs[2] = new_pr(2, author="iomete-renovate-bot[bot]", user={"login": "iomete-renovate-bot[bot]", "type": "Bot"})
        self.github.prs[3] = new_pr(3, author="rita")
        self.announcer.config.authors = {"ann"}

        for number in (1, 2, 3):
            self.announcer.on_pull_request(REPO, number, "opened")

        self.assertEqual(self.slack.messages, [])

    def test_announces_listed_author_case_insensitively(self):
        self.announcer.config.authors = {"ann"}
        self.github.prs[1] = new_pr(1, author="Ann", user={"login": "Ann", "type": "User"})

        self.announcer.on_pull_request(REPO, 1, "opened")

        self.assertEqual(len(self.slack.messages), 1)

    def test_draft_deletes_message_and_ready_again_posts_anew(self):
        self.event("opened")
        self.slack.post("C123", f"{psa.REMINDER_MARK} reminder", thread_ts="1.000")
        self.slack.messages.append({"ts": "9.000", "text": "on it", "blocks": None, "thread_ts": "1.000", "bot_id": None})
        self.event("converted_to_draft", draft=True)

        self.assertEqual([m["ts"] for m in self.slack.messages], ["9.000"], "only the human reply is left")

        self.clock += 3 * 3600
        self.event("ready_for_review", draft=False)

        self.assertEqual(len(self.slack.messages), 2)
        self.assertEqual(self.slack.payload("3.000")["ready_at"], int(self.clock))

    def test_closed_without_merge_deletes_message(self):
        self.event("opened")
        self.event("closed", state="closed")

        self.assertEqual(self.slack.messages, [])

    def test_repeated_event_keeps_reminder_clock(self):
        self.event("opened")
        self.clock += 5 * 3600
        self.event("ready_for_review")

        self.assertEqual(self.slack.payload()["ready_at"], int(MONDAY_MORNING))

    def test_reopened_as_draft_stays_out_of_slack(self):
        self.event("opened")
        self.event("closed", state="closed")
        self.event("reopened", state="open", draft=True)

        self.assertEqual(self.slack.messages, [])

    def test_merged_pr_gets_reaction_once(self):
        self.event("opened")
        self.event("closed", state="closed", merged=True)
        self.event("edited", state="closed", merged=True)

        self.assertNotIn("(merged)", self.slack.body(), "the reaction marks the merge")
        self.assertNotIn("~", self.slack.body())
        self.assertEqual(self.slack.reactions, [("1.000", psa.MERGED_REACTION, True)])
        self.assertTrue(self.slack.payload()["merged"])

    def test_any_event_announces_open_pr_without_message(self):
        # A newer run replaced the queued ready_for_review one.
        self.event("review_requested")

        self.assertEqual(len(self.slack.messages), 1)

    def test_approval_reaction_follows_review_state(self):
        self.event("opened")

        self.github.approvals[1] = True
        self.announcer.on_review(REPO, 1, "submitted")
        self.announcer.on_review(REPO, 1, "submitted")
        self.github.approvals[1] = False
        self.announcer.on_review(REPO, 1, "dismissed")

        self.assertEqual(self.slack.reactions, [("1.000", psa.APPROVED_REACTION, True),
                                                ("1.000", psa.APPROVED_REACTION, False)])
        self.assertFalse(self.slack.payload()["approved"])

    def test_review_refreshes_reviewers(self):
        self.event("opened")
        self.github.prs[1]["requested_reviewers"] = []
        self.github.states[1] = {"rita": "APPROVED"}

        self.announcer.on_review(REPO, 1, "submitted")

        self.assertEqual(self.slack.reviewers(), "<@URITA> :white_check_mark:")

    def test_comment_review_marks_reviewer_without_approving(self):
        self.event("opened")
        self.github.prs[1]["requested_reviewers"] = []
        self.github.states[1] = {"rita": "COMMENTED"}

        self.announcer.on_review(REPO, 1, "submitted")

        self.assertEqual(self.slack.reviewers(), "<@URITA> :speech_balloon:")
        self.assertEqual(self.slack.reactions, [])

    def test_changes_requested_after_approval_swaps_mark_and_reaction(self):
        self.event("opened")
        self.github.prs[1]["requested_reviewers"] = []
        self.github.approvals[1], self.github.states[1] = True, {"rita": "APPROVED"}
        self.announcer.on_review(REPO, 1, "submitted")
        self.github.approvals[1], self.github.states[1] = False, {"rita": "CHANGES_REQUESTED"}

        self.announcer.on_review(REPO, 1, "submitted")

        self.assertEqual(len(self.slack.messages), 1)
        self.assertEqual(self.slack.reviewers(), "<@URITA> :no_entry:")
        self.assertEqual(self.slack.reactions, [("1.000", psa.APPROVED_REACTION, True),
                                                ("1.000", psa.APPROVED_REACTION, False)])

    def test_mentions_others_once_without_author(self):
        self.github.prs[1] = new_pr(1, requested_reviewers=[{"login": "rita"}, {"login": "ann"}],
                                    assignees=[{"login": "Rita"}, {"login": "ann"}],
                                    requested_teams=[{"slug": "platform"}])

        self.announcer.on_pull_request(REPO, 1, "opened")

        self.assertEqual(self.slack.reviewers(), "<@URITA>")

    def test_approvers_are_marked_and_rerequested_ones_are_not(self):
        self.github.prs[1] = new_pr(1, requested_reviewers=[{"login": "rita"}], assignees=[{"login": "alex"}])
        self.github.states[1] = {login: "APPROVED" for login in ["Rita", "KenanBek", "alex", "ann", "auto-approve[bot]"]}

        self.announcer.on_pull_request(REPO, 1, "opened")

        self.assertEqual(self.slack.reviewers(), "<@URITA> <@UKANAN> :white_check_mark: <@UALEX> :white_check_mark:",
                         "rita was asked again, and the author and bots are left out")

    def test_reviewers_are_marked_by_their_review(self):
        self.github.prs[1] = new_pr(1, requested_reviewers=[], assignees=[])
        self.github.states[1] = {"rita": "COMMENTED", "alex": "CHANGES_REQUESTED", "KenanBek": "DISMISSED",
                                 "ujjawal": "PENDING"}

        self.event("opened")

        self.assertEqual(self.slack.reviewers(), "<@URITA> :speech_balloon: <@UALEX> :no_entry:",
                         "dismissed and pending reviews leave no mark")

    def test_rerequested_reviewer_loses_comment_and_changes_marks(self):
        self.github.prs[1] = new_pr(1, requested_reviewers=[{"login": "rita"}, {"login": "alex"}], assignees=[])
        self.github.states[1] = {"Rita": "CHANGES_REQUESTED", "alex": "COMMENTED", "KenanBek": "COMMENTED"}

        self.event("opened")

        self.assertEqual(self.slack.reviewers(), "<@URITA> <@UALEX> <@UKANAN> :speech_balloon:")

    def test_author_and_bot_reviews_are_not_marked(self):
        self.github.states[1] = {"ann": "COMMENTED", "Ann": "CHANGES_REQUESTED", "linter[bot]": "CHANGES_REQUESTED"}

        self.event("opened")

        self.assertEqual(self.slack.reviewers(), "<@URITA>")

    def test_failed_approval_read_still_posts(self):
        def fail(repo, number):
            raise psa.APIError("GET reviews: 502")
        self.github.reviewer_states = fail

        self.event("opened")

        self.assertEqual(self.slack.reviewers(), "<@URITA>")

    def test_matches_by_name_when_email_misses(self):
        self.github.prs[1] = new_pr(1, requested_reviewers=[{"login": "KenanBek"}, {"login": "ujjawal"}, {"login": "sam"}],
                                    assignees=[])

        self.announcer.on_pull_request(REPO, 1, "opened")

        self.assertEqual(self.slack.reviewers(), "<@UKANAN> <@UUJJ> `sam`",
                        "an ambiguous name is not guessed")

    def test_rechecks_before_posting(self):
        # The first lookup misses, as a bad Slack read would; the recheck finds the message.
        self.event("opened")
        real = self.slack.announcements
        calls = []

        def flaky(channel, now, since=None, threads_of=None):
            calls.append(1)
            return iter(()) if len(calls) == 1 else real(channel, now, since, threads_of)

        self.slack.announcements = flaky
        self.event("edited")

        self.assertEqual(len(self.slack.messages), 1)
        self.assertEqual(len(calls), 2)

    def test_adopts_message_posted_without_state(self):
        legacy = {"ts": "1791000000.000100", "bot_id": "BBOT", "text": "",
                  "blocks": [{"type": "section", "block_id": "abc",
                              "text": {"type": "mrkdwn", "text": "*PR:* https://github.com/iomete/infra/pull/1\n*Details:* x"}}]}
        human = {"ts": "1791000001.000100", "text": "PR: https://github.com/iomete/infra/pull/1", "blocks": []}
        self.slack.messages += [legacy, human]

        self.event("edited")

        self.assertEqual(len(self.slack.messages), 2, "the bot's old message is updated, nothing is posted")
        self.assertEqual(self.slack.payload("1791000000.000100")["state"], psa.STATE_OPEN)
        self.assertEqual(self.slack.payload("1791000000.000100")["ready_at"], 1791000000)

        self.event("converted_to_draft", draft=True)

        self.assertEqual(self.slack.messages, [human], "the bot's old message is deleted")

    def test_recognises_messages_already_posted_for_infra_5940(self):
        # The shapes Slack can return for the two messages posted before state was stored.
        url = "https://github.com/iomete/infra/pull/5940"
        shapes = [
            {"blocks": [{"block_id": "x1", "text": {"text": f"*PR:* {url}\n*Details:* feat"}}], "text": ""},
            {"blocks": [{"block_id": "x2", "text": {"text": f"*PR:* <{url}>\n*Details:* feat"}}], "text": ""},
            {"blocks": [{"block_id": "x3", "text": {"text": "rewritten"}}], "text": f"PR: {url} feat"},
        ]
        for message in shapes:
            with self.subTest(message=message):
                payload = psa.parse_announcement({"ts": "1791000000.1", **message})
                self.assertEqual((payload["repo"], payload["number"]), ("iomete/infra", 5940))

    def test_state_fits_in_a_block_id(self):
        payload = {"repo": "iomete/iomete-api-testing-suite-warmup", "number": 123456, "state": "open",
                   "ready_at": 1791000000, "reminded": False, "approved": False}

        self.assertLessEqual(len(psa.encode_state(payload)), 255)

    def test_override_by_email(self):
        self.slack.by_email["maks@example.com"] = "UMAKS"
        self.announcer.config.overrides = {"maksym-iomete": "maks@example.com"}
        self.github.prs[1] = new_pr(1, author="maksym-iomete", user={"login": "maksym-iomete", "type": "User"})

        self.announcer.on_pull_request(REPO, 1, "opened")

        self.assertIn("<@UMAKS>", self.slack.body())

    def test_skips_excluded_author(self):
        self.announcer.config.excluded_authors = {"ann"}
        self.github.prs[1] = new_pr(1, author="Ann", user={"login": "Ann", "type": "User"})

        self.announcer.on_pull_request(REPO, 1, "opened")

        self.assertEqual(self.slack.messages, [])

    def test_skips_excluded_author_even_when_listed(self):
        self.announcer.config.authors = {"ann"}
        self.announcer.config.excluded_authors = {"ann"}
        self.github.prs[1] = new_pr(1, author="ann")

        self.announcer.on_pull_request(REPO, 1, "opened")

        self.assertEqual(self.slack.messages, [])

    def test_shipped_excluded_authors_parse(self):
        path = os.path.join(os.path.dirname(__file__), "..", "excluded-authors.txt")

        self.assertIn("fmusayev", psa.load_excluded_authors(path))

    def test_review_announces_pr_whose_announcing_run_was_replaced(self):
        self.github.prs[1] = new_pr(1)

        self.announcer.on_review(REPO, 1, "submitted")

        self.assertEqual(len(self.slack.messages), 1)

    def test_ignores_prs_opened_before_cutoff(self):
        self.announcer.config.opened_after = psa.parse_time("2026-10-09")
        self.github.prs[1] = new_pr(1, created_at="2026-10-08T23:59:00Z")
        self.github.prs[2] = new_pr(2, created_at="2026-10-09T00:00:00Z")

        self.announcer.on_pull_request(REPO, 1, "edited")
        self.announcer.on_review(REPO, 1, "submitted")
        self.announcer.on_pull_request(REPO, 2, "opened")

        self.assertEqual(len(self.slack.messages), 1)
        self.assertIn("pull/2", self.slack.body())

    def test_listed_team_is_expanded_to_members(self):
        self.announcer.config.teams = {"platform": ["rita", "ann", "alex"]}
        self.github.prs[1] = new_pr(1, requested_reviewers=[{"login": "rita"}], assignees=[],
                                    requested_teams=[{"slug": "Platform"}, {"slug": "developers"}])

        self.announcer.on_pull_request(REPO, 1, "opened")

        # rita once, the author (ann) left out, the unlisted team hidden behind the people.
        self.assertEqual(self.slack.reviewers(), "<@URITA> <@UALEX>")

    def test_unlisted_team_stays_hidden_after_people_review(self):
        self.github.prs[1] = new_pr(1, requested_reviewers=[], requested_teams=[{"slug": "infra"}])
        self.github.states[1] = {"rita": "APPROVED"}

        self.announcer.on_pull_request(REPO, 1, "opened")

        self.assertEqual(self.slack.reviewers(), "<@URITA> :white_check_mark:")

    def test_requested_bot_does_not_hide_unlisted_team(self):
        self.github.prs[1] = new_pr(1, requested_reviewers=[{"login": "Copilot", "type": "Bot"}],
                                    requested_teams=[{"slug": "infra"}])

        self.announcer.on_pull_request(REPO, 1, "opened")

        self.assertIn("`@infra`", self.slack.reviewers())

    def test_unlisted_team_is_named_when_no_person_is_requested(self):
        self.github.prs[1] = new_pr(1, requested_reviewers=[{"login": "ann"}], assignees=[],
                                    requested_teams=[{"slug": "infra"}])

        self.announcer.on_pull_request(REPO, 1, "opened")

        # ann is the author, so the team is the only reviewer left.
        self.assertEqual(self.slack.reviewers(), "`@infra`")

    def test_event_teams_expand_when_the_api_hides_them(self):
        self.announcer.config.teams = {"infra": ["rita", "ann"]}
        self.github.prs[1] = new_pr(1, requested_reviewers=[], assignees=[])

        self.announcer.on_pull_request(REPO, 1, "opened")
        self.announcer.on_pull_request(REPO, 1, "review_requested", teams=["infra"])
        self.announcer.on_pull_request(REPO, 1, "edited")

        self.assertEqual(self.slack.reviewers(), "<@URITA>")
        self.assertEqual(self.slack.payload()["teams"], ["infra"])

        self.announcer.on_pull_request(REPO, 1, "review_request_removed", teams=[])

        self.assertNotIn("<@URITA>", self.slack.reviewers())
        self.assertNotIn("teams", self.slack.payload())

    def test_listed_teams_survive_the_cap(self):
        self.announcer.config.teams = {"infra": ["rita"]}
        self.github.prs[1] = new_pr(1, requested_reviewers=[], assignees=[])

        long_teams = [f"long-unlisted-team-name-{i:02d}" for i in range(10)]
        self.announcer.on_pull_request(REPO, 1, "opened", teams=long_teams + ["infra"])

        teams = self.slack.payload()["teams"]
        self.assertEqual(teams[0], "infra")
        self.assertLess(len(teams), 11)
        self.assertLessEqual(len(self.slack.messages[0]["blocks"][0]["block_id"]), psa.BLOCK_ID_LIMIT)
        self.assertIn("<@URITA>", self.slack.reviewers())

    def test_missing_merged_emoji_still_updates_message(self):
        self.event("opened")

        def fail(channel, ts, name, add):
            raise psa.APIError("slack reactions.add: invalid_name", code="invalid_name")
        self.slack.react = fail
        self.event("closed", state="closed", merged=True)

        self.assertTrue(self.slack.payload()["merged"])
        self.assertEqual(self.slack.payload()["state"], psa.STATE_CLOSED)

    def test_transient_merged_reaction_failure_retries(self):
        self.event("opened")
        react = self.slack.react

        def fail(channel, ts, name, add):
            raise psa.APIError("slack reactions.add: ratelimited", code="ratelimited")
        self.slack.react = fail
        self.event("closed", state="closed", merged=True)

        self.assertEqual(self.slack.payload()["state"], psa.STATE_CLOSED, "the message still updates")
        self.assertNotIn("merged", self.slack.payload())

        self.slack.react = react
        self.event("edited", state="closed", merged=True)

        self.assertEqual(self.slack.reactions, [("1.000", psa.MERGED_REACTION, True)])
        self.assertTrue(self.slack.payload()["merged"])

    def test_team_cap_leaves_room_for_closing(self):
        self.github.prs[1] = new_pr(1, requested_reviewers=[], assignees=[])
        teams = [f"team-{i:02d}" for i in range(30)]

        self.announcer.on_pull_request(REPO, 1, "opened", teams=teams)
        self.event("closed", state="closed", merged=True)

        self.assertLessEqual(len(self.slack.messages[0]["blocks"][0]["block_id"]), psa.BLOCK_ID_LIMIT)

    def test_event_teams_reads_payload(self):
        self.assertEqual(psa.event_teams({"pull_request": {"requested_teams": [{"slug": "infra"}]}}), ["infra"])
        self.assertEqual(psa.event_teams({"pull_request": {"requested_teams": []}}), [])
        self.assertIsNone(psa.event_teams({"pull_request": {"number": 1}}))

    def test_shipped_teams_parse(self):
        path = os.path.join(os.path.dirname(__file__), "..", "slack-teams.txt")
        teams = psa.load_teams(path)

        self.assertNotIn("engine-backend", teams)

    def test_override_wins_and_unknown_user_is_not_pinged(self):
        self.announcer.config.overrides = {"rita": "UOVERRIDE"}
        self.github.prs[1] = new_pr(1, requested_reviewers=[{"login": "rita"}, {"login": "stranger"}])

        self.announcer.on_pull_request(REPO, 1, "opened")

        self.assertIn("<@UOVERRIDE>", self.slack.reviewers())
        self.assertIn("`stranger`", self.slack.reviewers())


class SweepTest(AnnouncerTest):
    def announce_and_advance(self, **changes):
        self.event("opened", **changes)
        self.clock = MONDAY_MORNING + 25 * 3600

    def test_reminds_reviewers_and_owners_once_per_run(self):
        self.announce_and_advance()

        self.announcer.sweep(REPO)
        self.announcer.sweep(REPO)

        reminders = [m for m in self.slack.messages if m["thread_ts"]]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["thread_ts"], "1.000")
        self.assertIn("waiting 25 hours for review", reminders[0]["text"])
        self.assertIn("<@URITA>, please take a look", reminders[0]["text"])
        self.assertIn("<@UALEX>, if it isn't ready for review", reminders[0]["text"])
        self.assertTrue(self.slack.by_ts("1.000")["sender"])
        self.assertIsNone(reminders[0]["sender"], "only the announcement is posted as the author")

    def test_repeats_reminder_daily_while_waiting(self):
        self.announce_and_advance()
        first = self.clock
        swept = []
        # Once per local day: a late run one day and an early one the next are under 24h apart but still repeat.
        for hours, approved in ((0, False), (6, False), (22.5, False), (46.5, True), (70.5, False)):
            self.clock = first + hours * 3600
            if self.github.approvals.get(1, False) != approved:
                self.github.approvals[1] = approved
                self.announcer.on_review(REPO, 1, "submitted" if approved else "dismissed")
            self.announcer.sweep(REPO)
            swept.append(len([m for m in self.slack.messages if m["thread_ts"]]))

        self.assertEqual(swept, [1, 1, 2, 2, 3], "none while approved, again once approval is withdrawn")
        self.assertIn("waiting 4 days for review", self.slack.messages[-1]["text"], "counted from when it was ready")

    def test_repeat_reminder_retrims_teams_to_fit(self):
        self.announce_and_advance()
        payload = self.slack.payload()
        payload["reminded"] = int(self.clock) - 86400
        # A team that fills the state exactly with the old boolean flag, as saved before timestamps.
        old_state = {**payload, "reminded": False, "state": psa.STATE_CLOSED, "merged": True, "teams": ["t"]}
        payload["teams"] = ["t" * (psa.BLOCK_ID_LIMIT - len(psa.encode_state(old_state)) + 1)]
        self.announcer.save("1.000", payload, self.github.prs[1])

        self.announcer.sweep(REPO)

        saved = self.slack.payload()
        self.assertEqual(saved["reminded"], int(self.clock))
        self.assertLessEqual(len(psa.encode_state({**saved, "state": psa.STATE_CLOSED, "merged": True})),
                             psa.BLOCK_ID_LIMIT)

    def test_old_reminded_flag_repeats_on_next_run(self):
        self.announce_and_advance()
        payload = self.slack.payload()
        payload["reminded"] = True
        self.announcer.save("1.000", payload, self.github.prs[1])

        self.announcer.sweep(REPO)

        self.assertEqual(len([m for m in self.slack.messages if m["thread_ts"]]), 1)
        self.assertEqual(self.slack.payload()["reminded"], int(self.clock))

    def test_reminder_mentions_remembered_team(self):
        self.announcer.config.teams = {"infra": ["rita"]}
        self.github.prs[1] = new_pr(1, requested_reviewers=[])
        self.announcer.on_pull_request(REPO, 1, "opened", teams=["infra"])
        self.clock = MONDAY_MORNING + 25 * 3600

        self.announcer.sweep(REPO)

        reminders = [m for m in self.slack.messages if m["thread_ts"]]
        self.assertIn("<@URITA>, please take a look", reminders[0]["text"])

    def test_sweep_acts_on_latest_saved_state(self):
        self.announce_and_advance()
        stale = list(self.slack.announcements("C123", self.clock))
        # An event run moves the PR to draft between the sweep's listing and its reminder.
        self.event("converted_to_draft", draft=True)

        self.announcer.remind(stale[0][0], stale)

        self.assertEqual(self.slack.messages, [], "deleted, and no reminder")

    def test_sweep_keeps_pr_made_ready_while_it_ran(self):
        self.announce_and_advance()
        self.github.prs[1]["draft"] = True
        fetch = self.github.pull_request
        reads = []

        def ready_after_first_read(repo, number):
            reads.append(1)
            if len(reads) > 1:
                self.github.prs[number]["draft"] = False
            return fetch(repo, number)
        self.github.pull_request = ready_after_first_read

        self.announcer.sweep(REPO)

        self.assertEqual(self.slack.payload()["state"], psa.STATE_OPEN, "the message survives")

    def test_no_reminder_while_changes_are_requested(self):
        self.announce_and_advance()
        self.github.changes[1] = True

        self.announcer.sweep(REPO)

        self.assertEqual(len(self.slack.messages), 1)
        self.assertFalse(self.slack.payload()["reminded"], "the reminder can still go out once addressed")

    def test_no_second_reminder_when_flag_was_overwritten(self):
        self.announce_and_advance()
        self.announcer.sweep(REPO)
        # An event run saved a stale reminded=False over the sweep's write.
        payload = self.slack.payload()
        payload["reminded"] = False
        self.announcer.save("1.000", payload, self.github.prs[1])

        self.announcer.sweep(REPO)

        self.assertEqual(len([m for m in self.slack.messages if m["thread_ts"]]), 1)

    def test_daily_run_reminds_prs_ready_just_after_the_last_run(self):
        # Ready Monday 13:05 in Ho Chi Minh City, swept at Tuesday's 13:00 run: 23h55m, due within the grace.
        self.clock = datetime(2026, 10, 5, 6, 5, tzinfo=timezone.utc).timestamp()
        self.event("opened")

        self.clock = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc).timestamp()
        self.announcer.sweep(REPO)

        self.assertEqual(len([m for m in self.slack.messages if m["thread_ts"]]), 1)

    def test_friday_reminder_repeats_on_monday(self):
        self.clock = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc).timestamp()
        self.event("opened")
        swept = []
        # Friday, Saturday, Sunday and Monday runs.
        for day in (9, 10, 11, 12):
            self.clock = datetime(2026, 10, day, 6, 0, tzinfo=timezone.utc).timestamp()
            self.announcer.sweep(REPO)
            swept.append(len([m for m in self.slack.messages if m["thread_ts"]]))

        self.assertEqual(swept, [1, 1, 1, 2])

    def test_draft_after_daily_reminders_deletes_them_all(self):
        self.announce_and_advance()
        for day in range(3):
            self.clock = MONDAY_MORNING + (25 + 24 * day) * 3600
            self.announcer.sweep(REPO)
        self.assertEqual(len([m for m in self.slack.messages if m["thread_ts"]]), 3)

        self.event("converted_to_draft", draft=True)

        self.assertEqual(self.slack.messages, [])

    def test_no_reminder_for_pr_before_cutoff(self):
        self.announce_and_advance()
        self.announcer.config.opened_after = psa.parse_time("2026-10-02")

        self.announcer.sweep(REPO)

        self.assertEqual(len(self.slack.messages), 1)

    def test_sweeps_only_its_own_repo(self):
        self.announce_and_advance()

        self.announcer.sweep("iomete/other")

        self.assertEqual(len(self.slack.messages), 1)

    def test_asks_author_for_reviewer_when_none_requested(self):
        self.announce_and_advance(requested_reviewers=[], assignees=[])

        self.announcer.sweep(REPO)

        self.assertIn("no reviewer requested. <@UANN>, please request a review", self.slack.messages[1]["text"])

    def test_settles_prs_without_reminding(self):
        cases = {
            "closed": ({"state": "closed"}, False, None),
            "draft": ({"draft": True}, False, None),
            "approved": ({}, True, psa.STATE_OPEN),
        }
        for name, (changes, approved, want_state) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.announce_and_advance()
                self.github.prs[1].update(changes)
                self.github.approvals[1] = approved

                self.announcer.sweep(REPO)

                self.assertFalse([m for m in self.slack.messages if m["thread_ts"]], "no reminder posted")
                if want_state is None:
                    self.assertEqual(self.slack.messages, [], "withdrawn PRs are deleted")
                    continue
                self.assertEqual(self.slack.payload()["state"], want_state)
                self.assertEqual(self.slack.reactions, [("1.000", psa.APPROVED_REACTION, True)] if approved else [])

    def test_no_reminder_too_soon_or_on_weekends(self):
        cases = {
            "under 24h less the grace": MONDAY_MORNING + 21 * 3600,
            "saturday": datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc).timestamp(),
            "sunday": datetime(2026, 10, 11, 6, 0, tzinfo=timezone.utc).timestamp(),
        }
        for name, at in cases.items():
            with self.subTest(name):
                self.setUp()
                self.announce_and_advance()
                self.clock = at

                self.announcer.sweep(REPO)

                self.assertEqual(len(self.slack.messages), 1)

    def test_no_reminder_while_paused(self):
        # 20:00 UTC on Oct 6 is already Oct 7 in the reminder timezone.
        late = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc).timestamp()
        cases = {
            "before the day": (date(2026, 10, 7), None, 1),
            "on the day": (date(2026, 10, 6), None, 2),
            "on the day in local time": (date(2026, 10, 7), late, 2),
        }
        for name, (paused_until, at, want_messages) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.announcer.config.reminders_paused_until = paused_until
                self.announce_and_advance()
                self.clock = at or self.clock

                self.announcer.sweep(REPO)

                self.assertEqual(len(self.slack.messages), want_messages)

    def test_paused_sweep_still_settles_closed_prs(self):
        self.announcer.config.reminders_paused_until = date(2026, 10, 7)
        self.announce_and_advance()
        self.github.prs[1].update({"state": "closed"})

        self.announcer.sweep(REPO)

        self.assertEqual(self.slack.messages, [], "the withdrawn PR is deleted")

    def test_bad_pause_date_is_ignored(self):
        cases = {"empty": ("", None), "date": (" 2026-11-02 ", date(2026, 11, 2)), "typo": ("2026-11-2x", None)}
        for name, (value, want) in cases.items():
            with self.subTest(name):
                self.assertEqual(psa.parse_paused_until(value), want)


class StackTest(unittest.TestCase):
    def setUp(self):
        self.slack = FakeSlack()
        self.slack.ts_base = int(MONDAY_MORNING)
        self.github = FakeGitHub()
        self.clock = MONDAY_MORNING
        self.slack.clock = lambda: self.clock
        self.announcer = psa.Announcer(psa.Config(channel="C123"), self.slack, self.github, now=lambda: self.clock)
        self.github.prs[1] = new_pr(1)
        self.github.prs[2] = new_pr(2, base={"ref": "branch-1", "repo": {"full_name": REPO, "default_branch": "main"}})
        self.github.prs[3] = new_pr(3, base={"ref": "branch-2", "repo": {"full_name": REPO, "default_branch": "main"}})

    def test_stacked_prs_join_the_base_prs_thread(self):
        for number in (1, 2, 3):
            self.announcer.on_pull_request(REPO, number, "opened")

        root, second, third = self.slack.messages
        self.assertIsNone(root["thread_ts"])
        self.assertIn((root["ts"], psa.STACK_REACTION, True), self.slack.reactions, "marked as holding a stack")
        self.assertEqual(second["thread_ts"], root["ts"])
        self.assertEqual(third["thread_ts"], root["ts"], "the whole stack shares the first PR's thread")
        self.assertEqual(self.slack.payload(third["ts"])["thread"], root["ts"])

    def test_base_pr_made_ready_later_joins_the_stacked_prs_thread(self):
        self.announcer.on_pull_request(REPO, 2, "opened")
        self.announcer.on_pull_request(REPO, 1, "ready_for_review")

        root, base = self.slack.messages
        self.assertEqual(self.slack.payload(root["ts"])["number"], 2)
        self.assertEqual(base["thread_ts"], root["ts"])

    def test_stack_is_grouped_across_a_draft_in_the_middle(self):
        self.github.prs[2]["draft"] = True
        self.announcer.on_pull_request(REPO, 3, "opened")
        self.announcer.on_pull_request(REPO, 1, "opened")

        root, base = self.slack.messages
        self.assertEqual(base["thread_ts"], root["ts"])

    def test_stacked_pr_posts_to_the_channel_when_the_mark_fails(self):
        self.announcer.on_pull_request(REPO, 1, "opened")

        def react(channel, ts, name, add):
            raise psa.APIError("slack reactions.add: message_not_found", code="message_not_found")

        self.slack.react = react
        self.announcer.on_pull_request(REPO, 2, "opened")

        self.assertIsNone(self.slack.messages[1]["thread_ts"])
        self.assertNotIn("thread", self.slack.payload(self.slack.messages[1]["ts"]))

    def test_pr_from_a_deleted_fork_is_still_announced(self):
        self.github.prs[2]["head"]["repo"] = None
        self.announcer.on_pull_request(REPO, 2, "opened")

        self.assertEqual(len(self.slack.messages), 1)

    def test_stack_made_ready_at_once_settles_on_the_earliest_thread(self):
        # Both runs miss each other and post to the channel; PR 2's run then finds PR 1's earlier message.
        real = self.announcer.stack_thread
        misses = []

        def racing(repo, pr, stack):
            if pr["number"] == 2 and not misses:
                misses.append(1)
                return None
            return real(repo, pr, stack)

        self.announcer.stack_thread = racing
        self.announcer.on_pull_request(REPO, 1, "opened")
        self.announcer.on_pull_request(REPO, 2, "opened")

        root, moved = self.slack.messages
        self.assertEqual((self.slack.payload(root["ts"])["number"], root["thread_ts"]), (1, None))
        self.assertEqual((self.slack.payload(moved["ts"])["number"], moved["thread_ts"]), (2, root["ts"]))
        self.assertEqual(self.slack.payload(moved["ts"])["thread"], root["ts"])
        self.assertIn((root["ts"], psa.STACK_REACTION, True), self.slack.reactions)

    def test_earliest_pr_stays_in_the_channel(self):
        self.announcer.on_pull_request(REPO, 1, "opened")

        self.announcer.regroup(REPO, self.github.prs[1], self.slack.payload(self.slack.messages[0]["ts"]),
                               self.slack.messages[0]["ts"], {2}, {})

        self.assertEqual(len(self.slack.messages), 1)
        self.assertIsNone(self.slack.messages[0]["thread_ts"])

    def test_stacked_pr_after_a_day_gets_its_own_message(self):
        self.announcer.on_pull_request(REPO, 1, "opened")
        self.clock = MONDAY_MORNING + 25 * 3600

        self.announcer.on_pull_request(REPO, 2, "opened")

        self.assertIsNone(self.slack.messages[1]["thread_ts"])
        self.assertNotIn("thread", self.slack.payload(self.slack.messages[1]["ts"]))

    def test_pr_into_someone_elses_branch_gets_its_own_message(self):
        self.github.prs[2]["user"] = {"login": "rita", "type": "User"}
        self.announcer.on_pull_request(REPO, 1, "opened")
        self.announcer.on_pull_request(REPO, 2, "opened")

        self.assertIsNone(self.slack.messages[1]["thread_ts"])

    def test_reminder_matches_the_prs_it_lists(self):
        reminder = reminder_message("9.0", [2, 12])
        legacy = {"text": f"{psa.REMINDER_MARK} This PR has been waiting"}

        self.assertTrue(psa.reminds_about(reminder, 12))
        self.assertFalse(psa.reminds_about(reminder, 1))
        self.assertFalse(psa.reminds_about(reminder, 12, only=True), "it is about #2 as well")
        self.assertTrue(psa.reminds_about(legacy, 1), "an older reminder is the thread's own PR's")
        self.assertFalse(psa.reminds_about(legacy, 2, thread="1.0"))

    def test_stacked_pr_without_announced_base_gets_its_own_message(self):
        self.announcer.on_pull_request(REPO, 2, "opened")

        self.assertEqual(len(self.slack.messages), 1)
        self.assertIsNone(self.slack.messages[0]["thread_ts"])

    def test_withdrawn_stacked_pr_leaves_the_rest_of_the_thread(self):
        self.announcer.on_pull_request(REPO, 1, "opened")
        self.announcer.on_pull_request(REPO, 2, "opened")
        self.announcer.on_pull_request(REPO, 3, "opened")

        self.github.prs[2]["draft"] = True
        self.announcer.on_pull_request(REPO, 2, "converted_to_draft")

        self.assertEqual([self.slack.payload(m["ts"])["number"] for m in self.slack.messages], [1, 3])

    def test_stacked_pr_is_reminded_in_the_shared_thread(self):
        self.announcer.on_pull_request(REPO, 1, "opened")
        self.announcer.on_pull_request(REPO, 2, "opened")
        root = self.slack.messages[0]["ts"]
        self.clock = MONDAY_MORNING + 25 * 3600

        self.announcer.sweep(REPO)
        self.announcer.sweep(REPO)

        reminders = [m for m in self.slack.messages if m["text"].startswith(psa.REMINDER_MARK)]
        self.assertEqual(len(reminders), 1, "one for the whole stack, and once per run")
        self.assertEqual(reminders[0]["thread_ts"], root)
        self.assertEqual(reminders[0]["text"],
                         f"{psa.REMINDER_MARK} Some PRs in this thread are still waiting for review. <@URITA>, please take a look. "
                         "<@UALEX>, if any isn't ready for review, please mark it as draft.")
        self.assertTrue(self.slack.payload(root)["reminded"] and self.slack.payload(self.slack.messages[1]["ts"])["reminded"])

    def test_stacked_pr_due_later_gets_its_own_reminder_that_day(self):
        self.announcer.on_pull_request(REPO, 1, "opened")
        self.clock = MONDAY_MORNING + 12 * 3600
        self.announcer.on_pull_request(REPO, 2, "opened")
        self.clock = MONDAY_MORNING + 25 * 3600
        self.announcer.sweep(REPO)
        self.clock = MONDAY_MORNING + 49 * 3600
        self.announcer.sweep(REPO)

        reminders = [m for m in self.slack.messages if m["text"].startswith(psa.REMINDER_MARK)]
        self.assertEqual([psa.reminds_about(m, 1, only=True) for m in reminders], [True, False])
        self.assertTrue(psa.reminds_about(reminders[1], 1) and psa.reminds_about(reminders[1], 2),
                        "the next day's reminder covers both")


class DryRunTest(unittest.TestCase):
    def test_writes_are_skipped_and_reads_go_through(self):
        calls = []
        slack = psa.Slack("xoxb-test", dry_run=True)
        psa_request = psa.request_json
        psa.request_json = lambda url, method="GET", token=None, body=None, headers=None: (
            calls.append((method, url)) or {"ok": True, "messages": []})
        self.addCleanup(setattr, psa, "request_json", psa_request)

        self.assertEqual(slack.post("C1", "hi"), ("C1", "dry-run"))
        slack.update("C1", "1.0", "hi", [])
        slack.react("C1", "1.0", psa.APPROVED_REACTION, add=True)
        slack.call("conversations.history", params={"channel": "C1"})

        self.assertEqual(calls, [("GET", "https://slack.com/api/conversations.history?channel=C1")])



def announcement(ts, repo, number, **extra):
    payload = {"repo": repo, "number": number, "state": psa.STATE_OPEN}
    return {"ts": ts, "bot_id": "BBOT", "text": "",
            "blocks": [{"type": "section", "block_id": psa.encode_state(payload)}], **extra}


class SlackAnnouncementsTest(unittest.TestCase):
    def test_reads_the_repos_threads_after_the_channel(self):
        calls = []
        responses = {
            "auth.test": {"ok": True, "bot_id": "BBOT"},
            "conversations.history": {"ok": True, "messages": [
                announcement("30.0", REPO, 1, reply_count=2, reactions=[{"name": psa.STACK_REACTION}]),
                announcement("20.0", "iomete/other", 5, reply_count=1, reactions=[{"name": psa.STACK_REACTION}]),
                announcement("10.0", REPO, 3, reply_count=1),
                {"ts": "5.0", "subtype": "tombstone", "user": "USLACKBOT", "reply_count": 1},
            ]},
            "conversations.replies": {"ok": True, "messages": [
                announcement("30.0", REPO, 1), announcement("31.0", REPO, 2, thread_ts="30.0"),
                {"ts": "32.0", "bot_id": "BBOT", "text": f"{psa.REMINDER_MARK} waiting"},
            ]},
        }

        def serve(url, method="GET", token=None, body=None, headers=None):
            name = url.rsplit("/", 1)[-1].split("?")[0]
            calls.append(name)
            return responses[name]

        psa_request = psa.request_json
        psa.request_json = serve
        self.addCleanup(setattr, psa, "request_json", psa_request)
        slack = psa.Slack("xoxb-test")

        found = [(ts, p["number"]) for ts, p in slack.announcements("C1", 40.0, since=0, threads_of=REPO)]

        self.assertEqual(found, [("30.0", 1), ("20.0", 5), ("10.0", 3), ("31.0", 2), ("31.0", 2)])
        self.assertEqual(calls.count("conversations.replies"), 2,
                         "the repo's stack threads, and a withdrawn PR's tombstone")

        calls.clear()
        next(slack.announcements("C1", 40.0, since=0, threads_of=REPO))
        self.assertNotIn("conversations.replies", calls, "a hit in the channel reads no thread")


def reminder_message(ts, numbers):
    block_id = psa.REMINDER_PREFIX + psa.json.dumps(numbers, separators=(",", ":"))
    return {"ts": ts, "bot_id": "BBOT", "text": f"{psa.REMINDER_MARK} waiting", "blocks": [{"type": "section", "block_id": block_id}]}


class SlackDeleteTest(unittest.TestCase):
    def test_deletes_bot_reminders_then_the_message(self):
        calls = []
        replies = {"ok": True, "messages": [{"ts": "1.0", "bot_id": "BBOT"},
                                            {"ts": "2.0", "bot_id": "BBOT", "text": f"{psa.REMINDER_MARK} This PR has been waiting"},
                                            {"ts": "3.0", "user": "UANN"},
                                            {"ts": "4.0", "bot_id": "BBOT", "text": "PR: https://github.com/iomete/infra/pull/2 x"}]}
        psa_request = psa.request_json
        psa.request_json = lambda url, method="GET", token=None, body=None, headers=None: (
            calls.append((url.rsplit("/", 1)[-1].split("?")[0], body and body["ts"])) or replies)
        self.addCleanup(setattr, psa, "request_json", psa_request)
        slack = psa.Slack("xoxb-test")
        slack.bot_id = "BBOT"

        slack.delete("C1", "1.0", 1)

        self.assertEqual(calls, [("conversations.replies", None), ("chat.delete", "2.0"), ("chat.delete", "1.0")])

    def test_stacked_pr_deletes_only_its_own_message_and_reminders(self):
        calls = []
        url = "https://github.com/iomete/infra/pull/2"
        replies = {"ok": True, "messages": [{"ts": "1.0", "bot_id": "BBOT"},
                                            {"ts": "2.0", "bot_id": "BBOT", "text": f"PR: {url} x"},
                                            reminder_message("3.0", [2]),
                                            {"ts": "4.0", "bot_id": "BBOT", "text": f"{psa.REMINDER_MARK} This PR has been waiting"},
                                            reminder_message("5.0", [1, 2])]}
        psa_request = psa.request_json
        psa.request_json = lambda url, method="GET", token=None, body=None, headers=None: (
            calls.append((url.rsplit("/", 1)[-1].split("?")[0], body and body["ts"])) or replies)
        self.addCleanup(setattr, psa, "request_json", psa_request)
        slack = psa.Slack("xoxb-test")
        slack.bot_id = "BBOT"

        slack.delete("C1", "2.0", 2, thread="1.0")

        self.assertEqual(calls, [("conversations.replies", None), ("chat.delete", "3.0"), ("chat.delete", "2.0")])

    def test_deletes_reminders_past_the_first_page(self):
        calls = []
        pages = [{"ok": True, "messages": [{"ts": "1.0", "bot_id": "BBOT"}, reminder_message("2.0", [1])],
                  "response_metadata": {"next_cursor": "next"}},
                 {"ok": True, "messages": [{"ts": "1.0", "bot_id": "BBOT"}, reminder_message("3.0", [1])]}]

        def serve(url, method="GET", token=None, body=None, headers=None):
            calls.append((url.rsplit("/", 1)[-1].split("?")[0], body and body["ts"]))
            return pages.pop(0) if method == "GET" else {"ok": True}
        psa_request = psa.request_json
        psa.request_json = serve
        self.addCleanup(setattr, psa, "request_json", psa_request)
        slack = psa.Slack("xoxb-test")
        slack.bot_id = "BBOT"

        slack.delete("C1", "1.0", 1)

        self.assertEqual(calls, [("conversations.replies", None), ("chat.delete", "2.0"),
                                 ("conversations.replies", None), ("chat.delete", "3.0"), ("chat.delete", "1.0")])

    def test_reminder_lookup_reads_only_since_and_every_page(self):
        urls = []
        pages = [{"ok": True, "messages": [{"ts": "1.0", "bot_id": "BBOT"}], "response_metadata": {"next_cursor": "next"}},
                 {"ok": True, "messages": [reminder_message("500.0", [1])]}]

        def serve(url, method="GET", token=None, body=None, headers=None):
            urls.append(url)
            return pages.pop(0)
        psa_request = psa.request_json
        psa.request_json = serve
        self.addCleanup(setattr, psa, "request_json", psa_request)
        slack = psa.Slack("xoxb-test")
        slack.bot_id = "BBOT"

        self.assertTrue(slack.has_reminder("C1", "1.0", 400, 1))
        self.assertIn("oldest=400.000000", urls[0])
        self.assertIn("cursor=next", urls[1])

    def test_message_already_deleted_is_not_an_error(self):
        psa_request = psa.request_json
        psa.request_json = lambda url, method="GET", token=None, body=None, headers=None: (
            {"ok": False, "error": "thread_not_found" if method == "GET" else "message_not_found"})
        self.addCleanup(setattr, psa, "request_json", psa_request)

        psa.Slack("xoxb-test").delete("C1", "1.0", 1)


def reviews(*pairs):
    return [{"user": {"login": login}, "state": state} for login, state in pairs]


class GitHubReviewsTest(unittest.TestCase):
    def serve(self, *pages):
        """Answer the reviews endpoint with `pages` in order, recording each URL asked for."""
        self.urls = []
        pages = list(pages)
        psa_request = psa.request_json
        psa.request_json = lambda url, method="GET", token=None, body=None, headers=None: (
            self.urls.append(url) or pages.pop(0))
        self.addCleanup(setattr, psa, "request_json", psa_request)
        return psa.GitHub("ghp-test")

    def test_decision_outranks_later_comments(self):
        github = self.serve(reviews(("rita", "COMMENTED"), ("rita", "APPROVED"), ("rita", "COMMENTED"),
                                    ("alex", "CHANGES_REQUESTED"), ("alex", "COMMENTED"), ("ann", "COMMENTED")))

        self.assertEqual(github.reviewer_states(REPO, 1),
                         {"rita": "APPROVED", "alex": "CHANGES_REQUESTED", "ann": "COMMENTED"})

    def test_latest_decision_wins(self):
        github = self.serve(reviews(("rita", "APPROVED"), ("rita", "CHANGES_REQUESTED"),
                                    ("alex", "CHANGES_REQUESTED"), ("alex", "APPROVED"),
                                    ("sam", "APPROVED"), ("sam", "DISMISSED")))

        self.assertEqual(github.reviewer_states(REPO, 1),
                         {"rita": "CHANGES_REQUESTED", "alex": "APPROVED", "sam": "DISMISSED"})

    def test_reads_every_page(self):
        github = self.serve(reviews(*[("alex", "COMMENTED")] * 100), reviews(("rita", "APPROVED")))

        self.assertEqual(github.reviewer_states(REPO, 1), {"alex": "COMMENTED", "rita": "APPROVED"})
        self.assertEqual([url.rsplit("page=", 1)[1] for url in self.urls], ["1", "2"])

    def test_comments_are_neither_approval_nor_changes(self):
        github = self.serve(reviews(("rita", "COMMENTED")), reviews(("rita", "COMMENTED")),
                            reviews(("rita", "COMMENTED"), ("alex", "CHANGES_REQUESTED")),
                            reviews(("rita", "COMMENTED"), ("alex", "CHANGES_REQUESTED")))

        self.assertFalse(github.approved(REPO, 1))
        self.assertFalse(github.changes_requested(REPO, 1))
        self.assertFalse(github.approved(REPO, 1))
        self.assertTrue(github.changes_requested(REPO, 1))


class ConfigTest(unittest.TestCase):
    def test_loads_excluded_authors_and_rejects_non_logins(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# comment\nAnn\n")
        self.addCleanup(os.unlink, f.name)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as bad:
            bad.write("@octocat\n")
        self.addCleanup(os.unlink, bad.name)

        self.assertEqual(psa.load_config({"SLACK_CHANNEL": "C1", "EXCLUDED_AUTHORS_FILE": f.name}).excluded_authors, {"ann"})
        with self.assertRaises(ValueError):
            psa.load_excluded_authors(bad.name)

    def test_loads_overrides_and_authors(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# comment\nMaks = U111\n")
        self.addCleanup(os.unlink, f.name)

        config = psa.load_config({"SLACK_CHANNEL": "C1", "SLACK_USERS_FILE": f.name, "PR_AUTHORS": "Maksym-IOMETE, rita"})

        self.assertEqual(config.overrides, {"maks": "U111"})
        self.assertIsNone(config.opened_after)
        self.assertEqual(psa.load_config({"SLACK_CHANNEL": "C1", "OPENED_AFTER": "2026-10-09"}).opened_after,
                         datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp())
        self.assertEqual(config.authors, {"maksym-iomete", "rita"})
        self.assertEqual(config.remind_after, timedelta(hours=24))
        self.assertEqual(psa.load_config({"SLACK_CHANNEL": "C1", "REMIND_AFTER_HOURS": "1.5"}).remind_after,
                         timedelta(hours=1.5))

    def test_rejects_malformed_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("maks\n")
        self.addCleanup(os.unlink, f.name)

        with self.assertRaises(ValueError):
            psa.load_overrides(f.name)

    def test_human_duration(self):
        self.assertEqual(psa.human_duration(timedelta(hours=1)), "1 hour")
        self.assertEqual(psa.human_duration(timedelta(hours=30)), "30 hours")
        self.assertEqual(psa.human_duration(timedelta(hours=47)), "47 hours")
        self.assertEqual(psa.human_duration(timedelta(hours=48)), "2 days")
        self.assertEqual(psa.human_duration(timedelta(hours=59, minutes=30)), "2 days")
        self.assertEqual(psa.human_duration(timedelta(hours=60)), "3 days")
        self.assertEqual(psa.human_duration(timedelta(hours=84)), "4 days")
        self.assertEqual(psa.human_duration(timedelta(hours=73)), "3 days")


if __name__ == "__main__":
    unittest.main()
