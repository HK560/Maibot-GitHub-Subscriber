"""出件箱持久化、单目标隔离投递、事务性回滚与重试测试。"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import _helpers

config_mod = _helpers.load("config")
client_mod = _helpers.load("client")
engine_mod = _helpers.load("engine")
models_mod = _helpers.load("models")
state_mod = _helpers.load("state")


def commit(sha: str, login: str = "alice", message: str = "fix bug", date: str = "2026-11-03T00:00:00Z") -> Dict[str, Any]:
    return {
        "sha": sha,
        "commit": {
            "message": message,
            "author": {"name": login, "email": f"{login}@example.com", "date": date},
        },
        "author": {"login": login},
        "html_url": f"https://github.com/a/b/commit/{sha}",
    }


def release(rid: int, name: str, tag: str = "v1", date: str = "2026-11-03T01:00:00Z") -> Dict[str, Any]:
    return {
        "id": rid,
        "name": name,
        "tag_name": tag,
        "body": "release notes",
        "html_url": f"https://github.com/a/b/releases/tag/{tag}",
        "published_at": date,
        "author": {"login": "alice"},
        "draft": False,
        "prerelease": False,
    }


def pr(number: int, state: str, date: str = "2026-01-01T00:00:00Z") -> Dict[str, Any]:
    return {
        "number": number,
        "title": f"pr {number}",
        "body": "pr body",
        "state": state,
        "merged_at": None,
        "draft": False,
        "html_url": f"https://github.com/a/b/pull/{number}",
        "user": {"login": "alice"},
        "created_at": date,
        "updated_at": date,
    }


class FakeDeliveryClient:
    def __init__(self) -> None:
        self.commits: Dict[str, List[Dict[str, Any]]] = {"main": []}
        self.releases: List[Dict[str, Any]] = []
        self.tags: List[Dict[str, Any]] = []
        self.issues: List[Dict[str, Any]] = []
        self.prs: List[Dict[str, Any]] = []
        self.fail_releases = False

    async def get_repo_info(self, repo: str) -> Dict[str, Any]:
        return {"default_branch": "main", "full_name": repo}

    async def get_commits(self, repo: str, sha=None, author=None, since=None, etag=None, per_page=10):
        return list(self.commits.get(sha or "main", [])), None, False

    async def get_releases(self, repo: str, etag=None, per_page=5):
        if self.fail_releases:
            raise client_mod.GitHubError("GitHub releases API error", 500)
        return list(self.releases), None, False

    async def get_tags(self, repo: str, etag=None, per_page=10):
        return list(self.tags), None, False

    async def get_issues(self, repo: str, since=None, etag=None, state="all", per_page=15):
        return list(self.issues), None, False

    async def get_pull_requests(self, repo: str, etag=None, state="all", per_page=15):
        return list(self.prs), None, False


class RecordingNotifier:
    def __init__(self, target_behaviors: Optional[Dict[Tuple[str, str], List[Dict[str, int]]]] = None) -> None:
        self.behaviors = target_behaviors or {}
        self.calls: List[Dict[str, Any]] = []

    def destinations(self, config: Any, repo: Any = None) -> List[Tuple[str, str]]:
        if repo is None:
            groups, privates = config.notify.group_targets, config.notify.private_targets
        else:
            groups, privates = config.notify_targets_for(repo)
        return [("group", g) for g in groups] + [
            ("private", p) for p in privates
        ]

    async def send_events(
        self,
        events: List[Any],
        config: Any,
        *,
        repo: Any = None,
        targets: Optional[List[Tuple[str, str]]] = None,
    ) -> Dict[str, int]:
        dests = list(targets) if targets is not None else self.destinations(config, repo)
        self.calls.append({"dests": dests, "event_keys": [e.key for e in events]})
        if not dests:
            return {"sent": 0, "failed": 0, "unknown": 0, "skipped": 1, "targets": 0}
        target = dests[0]
        seq = self.behaviors.get(target)
        if seq:
            res = seq.pop(0)
            if "exception" in res:
                raise RuntimeError(str(res["exception"]))
            return dict(res)
        return {"sent": 1, "failed": 0, "unknown": 0, "skipped": 0, "targets": 1}


class DeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "state.json"
        self.state = state_mod.StateStore(self.state_file)
        self.client = FakeDeliveryClient()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make(self, notifier: Any, **repo_kwargs: Any) -> Tuple[Any, Any, Any]:
        defaults = dict(repo="a/b", branches=["main"], authors=[])
        defaults.update(repo_kwargs)
        repo = config_mod.RepoSubscription(**defaults)
        cfg = config_mod.GithubSubConfig(
            subscriptions={"repos": [repo]},
            notify={"group_targets": ["123456789"], "private_targets": ["11111111"]},
        )
        eng = engine_mod.SubscriptionEngine(None, lambda: cfg, self.state, notifier)
        return eng, cfg, repo

    def test_endpoint_failure_transactional_rollback(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)
        # Establish baseline
        self.client.commits["main"] = [commit("c1")]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        # Endpoint failure during releases
        self.client.commits["main"] = [commit("c2"), commit("c1")]
        self.client.fail_releases = True

        with self.assertRaises(client_mod.GitHubError):
            asyncio.run(eng._poll_repo(self.client, cfg, repo))

        # Check rollback: c2 was NOT recorded into known commits, outbox is empty
        state = self.state.repo_state("a/b")
        self.assertEqual(self.state.known_commits(state, "main"), ["c1"])
        self.assertEqual(state.get("outbox", []), [])

        # Recover endpoint and poll again: c2 is properly discovered and added
        self.client.fail_releases = False
        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))
        self.assertEqual([e.key for e in events], ["c2"])
        self.assertIn("c2", self.state.known_commits(state, "main"))
        self.assertEqual(len(state.get("outbox", [])), 1)

    def test_outbox_retry_on_definite_failure_bounded_and_dedup(self) -> None:
        # Group 123456789 always fails; Private 11111111 succeeds on first attempt
        behavior = {
            ("group", "123456789"): [
                {"sent": 0, "failed": 1, "unknown": 0, "skipped": 0, "targets": 1},
                {"sent": 0, "failed": 1, "unknown": 0, "skipped": 0, "targets": 1},
                {"sent": 0, "failed": 1, "unknown": 0, "skipped": 0, "targets": 1},
                {"sent": 0, "failed": 1, "unknown": 0, "skipped": 0, "targets": 1},
            ],
            ("private", "11111111"): [
                {"sent": 1, "failed": 0, "unknown": 0, "skipped": 0, "targets": 1},
            ],
        }
        notifier = RecordingNotifier(behavior)
        eng, cfg, repo = self._make(notifier)

        # Baseline
        self.client.commits["main"] = [commit("c1")]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        # New event
        self.client.commits["main"] = [commit("c2"), commit("c1")]

        # Round 1: poll_all
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        stats1 = asyncio.run(eng.poll_all())
        self.assertEqual(stats1["events"], 1)
        self.assertEqual(stats1["sent"], 1)
        self.assertEqual(stats1["failed"], 1)

        # Receipts in outbox: private delivered, group failed (retries=1)
        state = self.state.repo_state("a/b")
        self.assertEqual(len(state["outbox"]), 1)
        receipts = state["outbox"][0]["receipts"]
        self.assertEqual(receipts["private:11111111"]["status"], "delivered")
        self.assertEqual(receipts["group:123456789"]["status"], "failed")
        self.assertEqual(receipts["group:123456789"]["retries"], 1)

        # Round 2: private is NOT called again! Only group retries
        calls_before = len(notifier.calls)
        stats2 = asyncio.run(eng.poll_all())
        group_calls = [c for c in notifier.calls[calls_before:] if c["dests"] == [("group", "123456789")]]
        private_calls = [c for c in notifier.calls[calls_before:] if c["dests"] == [("private", "11111111")]]
        self.assertEqual(len(group_calls), 1)
        self.assertEqual(len(private_calls), 0)  # No duplicate send to private!
        self.assertEqual(receipts["group:123456789"]["retries"], 2)

        # Round 3: group retries third time and reaches max 3 -> terminal
        asyncio.run(eng.poll_all())
        self.assertEqual(receipts["group:123456789"]["retries"], 3)
        self.assertEqual(receipts["group:123456789"]["status"], "terminal")

        # Now all receipts are terminal/delivered -> outbox item is pruned!
        self.assertEqual(state.get("outbox", []), [])

    def test_unknown_status_on_send_exception_no_auto_retry(self) -> None:
        behavior = {
            ("group", "123456789"): [
                {"exception": "Network timeout"},
            ],
            ("private", "11111111"): [
                {"sent": 1, "failed": 0, "unknown": 0, "skipped": 0, "targets": 1},
            ],
        }
        notifier = RecordingNotifier(behavior)
        eng, cfg, repo = self._make(notifier)
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        # Establish baseline then add new commit
        self.client.commits["main"] = [commit("c1")]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))
        self.client.commits["main"] = [commit("c2"), commit("c1")]

        stats = asyncio.run(eng.poll_all())
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["unknown"], 1)

        state = self.state.repo_state("a/b")
        # Because both receipts are now terminal (private=delivered, group=unknown),
        # outbox is cleanly pruned and not retried on next poll!
        self.assertEqual(state.get("outbox", []), [])

        calls_len = len(notifier.calls)
        asyncio.run(eng.poll_all())
        self.assertEqual(len(notifier.calls), calls_len)  # No more calls!

    def test_interrupted_sending_status_marked_unknown(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        # Simulate state saved with 'sending' left from an uncompleted run
        state = self.state.repo_state("a/b")
        state["initialized"] = True
        self.state.add_to_outbox(
            "a/b",
            [models_mod.Event(kind="commit", repo="a/b", title="t", url="u", key="c0")],
            [("group", "123456789")],
        )
        state["outbox"][0]["receipts"]["group:123456789"]["status"] = "sending"
        self.state.save()

        stats = asyncio.run(eng.poll_all())
        self.assertEqual(stats["unknown"], 1)
        # Pruned because status transitioned to unknown (terminal)
        self.assertEqual(state.get("outbox", []), [])

    def test_removed_destination_marked_skipped(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        # Queue outbox with target 99999999 which is not in cfg targets
        state = self.state.repo_state("a/b")
        state["initialized"] = True
        self.state.add_to_outbox(
            "a/b",
            [models_mod.Event(kind="commit", repo="a/b", title="t", url="u", key="c0")],
            [("group", "99999999")],
        )
        self.state.save()

        stats = asyncio.run(eng.poll_all())
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(state.get("outbox", []), [])

    def test_no_destinations_discards_events(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)
        # Clear all destinations
        cfg.notify.group_targets = []
        cfg.notify.private_targets = []

        self.client.commits["main"] = [commit("c1")]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        self.client.commits["main"] = [commit("c2"), commit("c1")]
        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))
        self.assertEqual([e.key for e in events], ["c2"])

        state = self.state.repo_state("a/b")
        # No destinations -> outbox is empty (discarded)
        self.assertEqual(state.get("outbox", []), [])
        # But progress advances
        self.assertIn("c2", self.state.known_commits(state, "main"))

    def test_repo_custom_targets_used_for_outbox(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(
            notifier,
            use_custom_targets=True,
            group_targets=["222222222"],
            private_targets=[],
        )
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        self.client.commits["main"] = [commit("c1")]
        asyncio.run(eng.poll_all())
        self.client.commits["main"] = [commit("c2"), commit("c1")]
        asyncio.run(eng.poll_all())

        self.assertEqual(notifier.calls, [{
            "dests": [("group", "222222222")],
            "event_keys": ["c2"],
        }])
        state = self.state.repo_state("a/b")
        self.assertEqual(state.get("outbox", []), [])
        self.assertEqual(
            list(state["history"][0]["receipts"]),
            ["group:222222222"],
        )

    def test_repo_inherits_global_targets_for_outbox(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        self.client.commits["main"] = [commit("c1")]
        asyncio.run(eng.poll_all())
        self.client.commits["main"] = [commit("c2"), commit("c1")]
        asyncio.run(eng.poll_all())

        state = self.state.repo_state("a/b")
        delivered = {dest for call in notifier.calls for dest in call["dests"]}
        self.assertEqual(delivered, {
            ("group", "123456789"),
            ("private", "11111111"),
        })
        self.assertEqual(
            set(state["history"][0]["receipts"]),
            {"group:123456789", "private:11111111"},
        )

    def test_multiple_repos_use_their_own_targets(self) -> None:
        notifier = RecordingNotifier()
        repo_a = config_mod.RepoSubscription(
            repo="a/b", branches=["main"], use_custom_targets=True,
            group_targets=["222222222"], private_targets=[],
        )
        repo_c = config_mod.RepoSubscription(repo="c/d", branches=["main"])
        cfg = config_mod.GithubSubConfig(
            subscriptions={"repos": [repo_a, repo_c]},
            notify={"group_targets": ["123456789"], "private_targets": ["11111111"]},
        )
        eng = engine_mod.SubscriptionEngine(None, lambda: cfg, self.state, notifier)
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        self.client.commits["main"] = [commit("c1")]
        asyncio.run(eng.poll_all())
        self.client.commits["main"] = [commit("c2"), commit("c1")]
        asyncio.run(eng.poll_all())

        state_a = self.state.repo_state("a/b")
        state_c = self.state.repo_state("c/d")
        self.assertEqual(list(state_a["history"][0]["receipts"]), ["group:222222222"])
        self.assertEqual(
            set(state_c["history"][0]["receipts"]),
            {"group:123456789", "private:11111111"},
        )

    def test_custom_repo_does_not_fall_back_to_global_for_old_outbox(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(
            notifier,
            use_custom_targets=True,
            group_targets=["222222222"],
            private_targets=[],
        )
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        state = self.state.repo_state("a/b")
        state["initialized"] = True
        # 这条回执来自切换配置前的全局目标，现在不属于该仓库的单独目标
        self.state.add_to_outbox(
            "a/b",
            [models_mod.Event(kind="commit", repo="a/b", title="t", url="u", key="c0")],
            [("group", "123456789")],
        )
        self.state.save()

        stats = asyncio.run(eng.poll_all())
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(state.get("outbox", []), [])

    def test_custom_repo_target_survives_global_target_change(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(
            notifier,
            use_custom_targets=True,
            group_targets=["222222222"],
            private_targets=[],
        )
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        state = self.state.repo_state("a/b")
        state["initialized"] = True
        self.state.add_to_outbox(
            "a/b",
            [models_mod.Event(kind="commit", repo="a/b", title="t", url="u", key="c0")],
            [("group", "222222222")],
        )
        self.state.save()

        # 全局目标清空不影响仓库级目标
        cfg.notify.group_targets = []
        cfg.notify.private_targets = []
        stats = asyncio.run(eng.poll_all())
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(notifier.calls[0]["dests"], [("group", "222222222")])
        self.assertEqual(state.get("outbox", []), [])

    def test_global_event_time_sort_and_cap(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)
        cfg.polling.max_events_per_repo_per_run = 1

        self.client.commits["main"] = [commit("c1")]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        # Add an old PR from Jan and a brand new commit from Nov
        self.client.commits["main"] = [commit("c2_new", date="2026-11-03T00:00:00Z"), commit("c1")]
        self.client.prs = [pr(10, "open", date="2026-01-01T00:00:00Z")]

        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))
        # With global time sort, the newer commit must be chosen over the old PR
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].key, "c2_new")

    def test_branch_commit_dedup_in_same_run(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier, branches=["main", "dev"])

        self.client.commits["main"] = [commit("shared_sha")]
        self.client.commits["dev"] = [commit("shared_sha")]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        # New shared commit on both branches
        self.client.commits["main"] = [commit("new_shared"), commit("shared_sha")]
        self.client.commits["dev"] = [commit("new_shared"), commit("shared_sha")]

        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))
        # Should only yield 1 event, not 2
        self.assertEqual([e.key for e in events], ["new_shared"])

    def test_first_run_lookback_filtering(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)
        cfg.notify.notify_on_first_run = True
        cfg.polling.first_run_lookback_hours = 24

        # c_old is 48 hours ago, c_recent is 2 hours ago
        now = time.time()
        c_old_date = datetime.fromtimestamp(now - 48 * 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        c_recent_date = datetime.fromtimestamp(now - 2 * 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        self.client.commits["main"] = [
            commit("c_recent", date=c_recent_date),
            commit("c_old", date=c_old_date),
        ]

        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))
        self.assertEqual([e.key for e in events], ["c_recent"])

    def test_save_disk_failure_propagates(self) -> None:
        # Point store to an invalid/non-writable directory to verify OSError is propagated
        invalid_path = Path("/proc/invalid_path/state.json")
        store = state_mod.StateStore(invalid_path)
        with self.assertRaises(OSError):
            store.save()

    def test_platform_route_change_skips_outbox(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        # Queue outbox item with platform 'telegram'
        state = self.state.repo_state("a/b")
        state["initialized"] = True
        self.state.add_to_outbox(
            "a/b",
            [models_mod.Event(kind="commit", repo="a/b", title="t", url="u", key="c0")],
            [("group", "123456789")],
            platform="telegram",
        )
        self.state.save()

        # Current cfg notify platform is 'qq'
        cfg.notify.platform = "qq"
        stats = asyncio.run(eng.poll_all())
        # Should NOT send on qq!
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(len(notifier.calls), 0)
        # Outbox item remains pending for telegram
        self.assertEqual(len(state.get("outbox", [])), 1)

    def test_branch_404_records_warning_in_state(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier, branches=["missing-branch"])

        async def not_found(*a, **k):
            raise client_mod.GitHubNotFoundError("Branch not found", 404)
        self.client.get_commits = not_found

        asyncio.run(eng._poll_repo(self.client, cfg, repo))
        state = self.state.repo_state("a/b")
        self.assertIn("404", state.get("last_warning", ""))

    def test_branch_prefix_stripped_and_extra_branch(self) -> None:
        notifier = RecordingNotifier()
        # Branches list in UI choices is formatted as '<repo> :: <branch>'
        eng, cfg, repo = self._make(notifier, branches=["a/b :: dev_feat"])

        self.client.commits["dev_feat"] = [commit("c_feat")]
        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))
        # Baseline established on dev_feat branch
        self.client.commits["dev_feat"] = [commit("c_feat2"), commit("c_feat")]
        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].key, "c_feat2")
        self.assertEqual(events[0].extra.get("branch"), "dev_feat")

    def test_author_display_name_local_filter(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier, authors=["Alice Smith"])

        self.client.commits["main"] = [commit("c_base")]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        # Commit where git author display name matches "Alice Smith"
        c_match = {
            "sha": "c_alice",
            "commit": {
                "message": "fix",
                "author": {"name": "Alice Smith", "email": "other@example.com", "date": "2026-11-03T00:00:00Z"},
            },
            "author": {"login": "asmith_github"},  # Login does not match "Alice Smith"
            "html_url": "https://github.com/a/b/commit/c_alice",
        }
        self.client.commits["main"] = [c_match, commit("c_base")]
        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))
        self.assertEqual([e.key for e in events], ["c_alice"])

    def test_force_poll_bypasses_repo_error_backoff(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        state = self.state.repo_state("a/b")
        state["initialized"] = True
        state["error_until"] = time.time() + 9999
        self.client.commits["main"] = [commit("c_force")]

        # Non-force poll skips repository due to error backoff
        stats_normal = asyncio.run(eng.poll_all(force=False))
        self.assertEqual(stats_normal["events"], 0)

        # Force poll bypasses error backoff!
        stats_force = asyncio.run(eng.poll_all(force=True))
        self.assertEqual(stats_force["events"], 1)

    def test_history_diagnostics_and_retention_cleanup(self) -> None:
        notifier = RecordingNotifier({
            ("group", "123456789"): [{"sent": 1, "failed": 0, "unknown": 0, "skipped": 0, "targets": 1}],
            ("private", "11111111"): [{"sent": 1, "failed": 0, "unknown": 0, "skipped": 0, "targets": 1}],
        })
        eng, cfg, repo = self._make(notifier)
        async def mock_get_client(c):
            return self.client
        eng._get_client = mock_get_client

        # Establish baseline
        self.client.commits["main"] = [commit("c_base")]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        # Send new event
        self.client.commits["main"] = [commit("c_hist"), commit("c_base")]
        asyncio.run(eng.poll_all())

        state = self.state.repo_state("a/b")
        # Outbox item pruned and preserved in history
        self.assertEqual(state.get("outbox", []), [])
        self.assertEqual(len(state.get("history", [])), 1)
        self.assertIn("c_hist", state["history"][0]["event_keys"])

        # Test retention cleanup on issues
        state["issues"] = {
            "1": {"state": "closed", "updated_at": "2020-01-01T00:00:00Z"},  # old
            "2": {"state": "open", "updated_at": "2026-11-03T00:00:00Z"},    # fresh
        }
        self.state.cleanup(retention_days=30)
        self.assertNotIn("1", state["issues"])
        self.assertIn("2", state["issues"])

    # ------------------------------------------------------------ 陈旧条目回归
    def test_stale_pr_not_renotified_after_retention_cleanup(self) -> None:
        """保留期清理掉旧 PR 记录后，/pulls 仍返回该旧 PR 时不得再当作新开通知。"""

        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)

        # 旧 PR：updated_at 远早于上次成功轮询
        self.client.prs = [pr(10, "open", date="2026-01-01T00:00:00Z")]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        state = self.state.repo_state("a/b")
        state["last_success"] = time.time()
        self.state.cleanup(retention_days=30)
        # 清理确实移除了旧记录
        self.assertEqual(state["prs"], {})

        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))
        self.assertEqual(events, [])  # 不得重复通知
        # 状态被重新记录，后续轮询同样安静
        self.assertIn("10", state["prs"])
        self.assertEqual(asyncio.run(eng._poll_repo(self.client, cfg, repo)), [])

    def test_stale_issue_not_renotified_after_retention_cleanup(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)

        self.client.issues = [{
            "number": 7,
            "title": "old issue",
            "body": "",
            "state": "open",
            "html_url": "https://github.com/a/b/issues/7",
            "user": {"login": "alice"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        state = self.state.repo_state("a/b")
        state["last_success"] = time.time()
        self.state.cleanup(retention_days=30)
        self.assertEqual(state["issues"], {})

        self.assertEqual(asyncio.run(eng._poll_repo(self.client, cfg, repo)), [])

    def test_new_pr_still_notified_after_cleanup(self) -> None:
        """新鲜度门槛不得吞掉真正的新 PR。"""

        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        state = self.state.repo_state("a/b")
        state["last_success"] = time.time()
        fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.client.prs = [pr(11, "open", date=fresh)]

        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))
        self.assertEqual([e.key for e in events], ["pr-11-opened"])

    def test_page_shift_old_pr_not_notified_as_new(self) -> None:
        """旧 PR 首次翻进第一页时，不得被当作新开通知。"""

        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)

        fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.client.prs = [pr(5, "open", date=fresh)]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        # 新 PR 出现，把一条从未见过的陈旧 PR 顶进第一页
        self.client.prs = [
            pr(6, "open", date=fresh),
            pr(999, "open", date="2020-01-01T00:00:00Z"),
        ]
        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))
        self.assertEqual([e.key for e in events], ["pr-6-opened"])

    def test_issue_close_after_prune_still_notified(self) -> None:
        """清理掉 open 记录后真正发生的关闭仍需补发一次通知。"""

        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)

        old_issue = {
            "number": 3,
            "title": "old",
            "body": "",
            "state": "open",
            "html_url": "https://github.com/a/b/issues/3",
            "user": {"login": "alice"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        self.client.issues = [old_issue]
        asyncio.run(eng._poll_repo(self.client, cfg, repo))

        state = self.state.repo_state("a/b")
        state["last_success"] = time.time()
        self.state.cleanup(retention_days=30)
        self.assertEqual(state["issues"], {})

        closed = dict(old_issue, state="closed",
                      updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.client.issues = [closed]

        events = asyncio.run(eng._poll_repo(self.client, cfg, repo))
        self.assertEqual([(e.kind, e.action) for e in events], [("issue", "已关闭")])

    def test_release_page_of_100_does_not_renotify(self) -> None:
        """per_page 可达 100，已见 Release 上限必须 >= 100，否则 51..100 会反复通知。"""

        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier)

        self.client.releases = [release(i, f"v{i}", f"v{i}") for i in range(1, 101)]
        self.assertEqual(asyncio.run(eng._poll_repo(self.client, cfg, repo)), [])  # 基线

        for _ in range(3):
            self.assertEqual(asyncio.run(eng._poll_repo(self.client, cfg, repo)), [])

        state = self.state.repo_state("a/b")
        self.assertEqual(len(state["releases"]), 100)

    def test_tag_page_of_100_does_not_renotify(self) -> None:
        notifier = RecordingNotifier()
        eng, cfg, repo = self._make(notifier, watch_tags=True)

        self.client.tags = [{"name": f"t{i}"} for i in range(100)]
        self.assertEqual(asyncio.run(eng._poll_repo(self.client, cfg, repo)), [])

        for _ in range(3):
            self.assertEqual(asyncio.run(eng._poll_repo(self.client, cfg, repo)), [])

        self.assertEqual(len(self.state.repo_state("a/b")["tags"]), 100)


if __name__ == "__main__":
    unittest.main()
