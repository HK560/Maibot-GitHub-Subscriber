"""GitHub 客户端解析与错误处理测试（使用 httpx MockTransport）。"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List

import httpx

import _helpers

client_mod = _helpers.load("client")


def make_client(handler) -> Any:
    c = client_mod.GitHubClient()
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )
    return c


class ClientTest(unittest.TestCase):
    def test_get_releases_and_etag(self) -> None:
        state = {"calls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            state["calls"] += 1
            if request.headers.get("If-None-Match"):
                return httpx.Response(304, headers={"etag": '"abc"'})
            return httpx.Response(
                200,
                json=[{"id": 1, "tag_name": "v1", "name": "v1"}],
                headers={"etag": '"abc"', "x-ratelimit-limit": "60", "x-ratelimit-remaining": "59"},
            )

        async def run() -> None:
            c = make_client(handler)
            data, etag, not_mod = await c.get_releases("a/b")
            self.assertEqual(len(data), 1)
            self.assertEqual(etag, '"abc"')
            self.assertFalse(not_mod)
            self.assertEqual(c.rate_limit.remaining, 59)
            data2, etag2, not_mod2 = await c.get_releases("a/b", etag=etag)
            self.assertEqual(data2, [])
            self.assertTrue(not_mod2)
            await c.close()

        asyncio.run(run())

    def test_issues_filters_pull_requests(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[
                {"number": 1, "title": "issue"},
                {"number": 2, "title": "pr", "pull_request": {"url": "x"}},
            ])

        async def run() -> None:
            c = make_client(handler)
            data, _etag, _nm = await c.get_issues("a/b")
            self.assertEqual([item["number"] for item in data], [1])
            await c.close()

        asyncio.run(run())

    def test_commits_params_and_payload(self) -> None:
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=[{"sha": "abc", "commit": {"message": "m"}}])

        async def run() -> None:
            c = make_client(handler)
            data, _e, _n = await c.get_commits("a/b", sha="dev", author="alice", since="2026-01-01T00:00:00Z")
            self.assertEqual(captured["params"].get("sha"), "dev")
            self.assertEqual(captured["params"].get("author"), "alice")
            self.assertEqual(data[0]["sha"], "abc")
            await c.close()

        asyncio.run(run())

    def test_not_found_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        async def run() -> None:
            c = make_client(handler)
            with self.assertRaises(client_mod.GitHubNotFoundError):
                await c.get_repo_info("a/missing")
            await c.close()

        asyncio.run(run())

    def test_rate_limit_403_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                text="API rate limit exceeded for 1.2.3.4",
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"},
            )

        async def run() -> None:
            c = make_client(handler)
            with self.assertRaises(client_mod.GitHubRateLimitError):
                await c.get_releases("a/b")
            await c.close()

        asyncio.run(run())

    def test_auth_401_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Bad credentials"})

        async def run() -> None:
            c = make_client(handler)
            with self.assertRaises(client_mod.GitHubAuthError):
                await c.get_releases("a/b")
            await c.close()

        asyncio.run(run())

    def test_auth_header_present(self) -> None:
        seen: Dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json=[])

        async def run() -> None:
            c = make_client(handler)
            c.token = "secret-token"
            await c.get_releases("a/b")
            self.assertEqual(seen["auth"], "Bearer secret-token")
            await c.close()

        asyncio.run(run())


class ConditionalRequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_implicit_etag_uses_cached_payload(self):
        calls = []
        def handler(request):
            calls.append(request.headers.get("If-None-Match"))
            if len(calls) == 1:
                return httpx.Response(200, json=[{"id":1}], headers={"etag":'"v1"'})
            return httpx.Response(304)
        c = make_client(handler)
        try:
            first, _, _ = await c.get_releases("a/b")
            second, _, _ = await c.get_releases("a/b")
            self.assertEqual(first, second)
            self.assertEqual(calls, [None, '"v1"'])
        finally:
            await c.close()

    async def test_secondary_rate_limit(self):
        c = make_client(lambda request: httpx.Response(429, headers={"Retry-After":"60"}))
        try:
            with self.assertRaises(client_mod.GitHubRateLimitError):
                await c.get_repo_info("a/b")
        finally:
            await c.close()

    async def test_branch_names(self):
        c = make_client(lambda request: httpx.Response(200, json=[{"name":"main"},{"name":"dev"}]))
        try:
            self.assertEqual(await c.get_branches("a/b"), ["main", "dev"])
        finally:
            await c.close()


if __name__ == "__main__":
    unittest.main()
