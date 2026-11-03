"""轮询引擎事件采集与过滤测试。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

import _helpers

config_mod = _helpers.load("config")
engine_mod = _helpers.load("engine")
state_mod = _helpers.load("state")


def commit(sha: str, login: str = "alice", message: str = "fix bug", date: str = "2026-01-01T00:00:00Z") -> Dict[str, Any]:
    return {
        "sha": sha,
        "commit": {
            "message": message,
            "author": {"name": login, "email": f"{login}@example.com", "date": date},
        },
        "author": {"login": login},
        "html_url": f"https://github.com/a/b/commit/{sha}",
    }


def release(rid: int, name: str, tag: str = "v1") -> Dict[str, Any]:
    return {
        "id": rid,
        "name": name,
        "tag_name": tag,
        "body": "release notes",
        "html_url": f"https://github.com/a/b/releases/tag/{tag}",
        "published_at": "2026-02-01T00:00:00Z",
        "author": {"login": "alice"},
        "draft": False,
        "prerelease": False,
    }


def issue(number: int, state: str, title: str = "bug") -> Dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": "issue body",
        "state": state,
        "html_url": f"https://github.com/a/b/issues/{number}",
        "user": {"login": "alice"},
        "created_at": "2026-03-01T00:00:00Z",
        "updated_at": "2026-03-02T00:00:00Z",
    }


def pr(number: int, state: str, merged: bool = False, draft: bool = False, title: str = "feat") -> Dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": "pr body",
        "state": state,
        "merged_at": "2026-03-05T00:00:00Z" if merged else None,
        "draft": draft,
        "html_url": f"https://github.com/a/b/pull/{number}",
        "user": {"login": "alice"},
        "created_at": "2026-03-01T00:00:00Z",
        "updated_at": "2026-03-02T00:00:00Z",
    }


class FakeClient:
    def __init__(self) -> None:
        self.commits: Dict[str, List[Dict[str, Any]]] = {"main": []}
        self.releases: List[Dict[str, Any]] = []
        self.tags: List[Dict[str, Any]] = []
        self.issues: List[Dict[str, Any]] = []
        self.prs: List[Dict[str, Any]] = []

    async def get_repo_info(self, repo: str) -> Dict[str, Any]:
        return {"default_branch": "main", "full_name": repo}

    async def get_commits(self, repo: str, sha=None, author=None, since=None, etag=None, per_page=10):
        return list(self.commits.get(sha or "main", [])), None, False

    async def get_releases(self, repo: str, etag=None, per_page=5):
        return list(self.releases), None, False

    async def get_tags(self, repo: str, etag=None, per_page=10):
        return list(self.tags), None, False

    async def get_issues(self, repo: str, since=None, etag=None, state="all", per_page=15):
        return list(self.issues), None, False

    async def get_pull_requests(self, repo: str, etag=None, state="all", per_page=15):
        return list(self.prs), None, False


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = state_mod.StateStore(Path(self.tmp.name) / "state.json")
        self.client = FakeClient()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make(self, **repo_kwargs: Any):
        defaults = dict(repo="a/b", branches=["main"], authors=[])
        defaults.update(repo_kwargs)
        repo = config_mod.RepoSubscription(**defaults)
        cfg = config_mod.GithubSubConfig(subscriptions={"repos": [repo]})
        eng = engine_mod.SubscriptionEngine(None, lambda: cfg, self.state, None)
        return eng, cfg, repo

    def _poll(self, eng, cfg, repo):
        return asyncio.run(eng._poll_repo(self.client, cfg, repo))

    def test_baseline_then_new_commit(self) -> None:
        eng, cfg, repo = self._make()
        self.client.commits["main"] = [commit("c1")]
        self.assertEqual(self._poll(eng, cfg, repo), [])  # 基线不推送
        self.client.commits["main"] = [commit("c2"), commit("c1")]
        events = self._poll(eng, cfg, repo)
        self.assertEqual([e.key for e in events], ["c2"])
        self.assertEqual(events[0].kind, "commit")

    def test_author_filter(self) -> None:
        eng, cfg, repo = self._make(authors=["alice"])
        self.client.commits["main"] = [commit("c1", login="alice")]
        self._poll(eng, cfg, repo)
        self.client.commits["main"] = [
            commit("c2", login="bob"),
            commit("c3", login="alice"),
        ]
        events = self._poll(eng, cfg, repo)
        self.assertEqual([e.key for e in events], ["c3"])

    def test_keyword_filter(self) -> None:
        eng, cfg, repo = self._make(ignore_keywords=["wip"])
        self.client.commits["main"] = [commit("c1")]
        self._poll(eng, cfg, repo)
        self.client.commits["main"] = [
            commit("c2", message="wip something"),
            commit("c3", message="real fix"),
        ]
        events = self._poll(eng, cfg, repo)
        self.assertEqual([e.key for e in events], ["c3"])

    def test_release_detection(self) -> None:
        eng, cfg, repo = self._make()
        self.client.releases = [release(1, "v1.0")]
        self.assertEqual(self._poll(eng, cfg, repo), [])
        self.client.releases = [release(2, "v1.1", "v1.1"), release(1, "v1.0")]
        events = self._poll(eng, cfg, repo)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "release")
        self.assertIn("v1.1", events[0].title)

    def test_issue_open_and_close(self) -> None:
        eng, cfg, repo = self._make()
        self.client.issues = [issue(1, "open")]
        self.assertEqual(self._poll(eng, cfg, repo), [])
        # 关闭通知
        self.client.issues = [issue(1, "closed")]
        events = self._poll(eng, cfg, repo)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "issue")
        self.assertEqual(events[0].action, "已关闭")
        # 新开 Issue
        self.client.issues = [issue(2, "open", "new bug"), issue(1, "closed")]
        events = self._poll(eng, cfg, repo)
        self.assertEqual(len(events), 1)
        self.assertIn("#2", events[0].title)

    def test_pr_merge_detection(self) -> None:
        eng, cfg, repo = self._make()
        self.client.prs = [pr(10, "open")]
        self.assertEqual(self._poll(eng, cfg, repo), [])
        self.client.prs = [pr(10, "closed", merged=True)]
        events = self._poll(eng, cfg, repo)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "已合并")

    def test_ignore_draft_pr(self) -> None:
        eng, cfg, repo = self._make()
        self.client.prs = [pr(11, "open", draft=True)]
        self.assertEqual(self._poll(eng, cfg, repo), [])
        # 草稿转为就绪后应通知
        self.client.prs = [pr(11, "open", draft=False)]
        events = self._poll(eng, cfg, repo)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "新开")

    def test_max_events_limit(self) -> None:
        eng, cfg, repo = self._make()
        cfg.polling.max_events_per_repo_per_run = 3
        self.client.commits["main"] = [commit("base")]
        self._poll(eng, cfg, repo)
        self.client.commits["main"] = [commit(f"n{i}") for i in range(10)] + [commit("base")]
        events = self._poll(eng, cfg, repo)
        self.assertEqual(len(events), 3)

    def test_default_branch_from_repo_info(self) -> None:
        eng, cfg, repo = self._make(branches=[])
        self.client.commits["main"] = [commit("c1")]
        self._poll(eng, cfg, repo)
        self.assertEqual(self.state.repo_state("a/b")["default_branch"], "main")
        self.client.commits["main"] = [commit("c2"), commit("c1")]
        events = self._poll(eng, cfg, repo)
        self.assertEqual([e.key for e in events], ["c2"])

    def test_bot_author_ignored_by_default(self) -> None:
        eng, cfg, repo = self._make()
        self.client.commits["main"] = [commit("c0")]
        self._poll(eng, cfg, repo)
        self.client.commits["main"] = [commit("c1", login="dependabot[bot]"), commit("c2")]
        events = self._poll(eng, cfg, repo)
        self.assertEqual([e.key for e in events], ["c2"])


if __name__ == "__main__":
    unittest.main()
