"""GitHub REST API 异步客户端。

支持认证、代理、条件请求（ETag / 304 Not Modified）与速率限制跟踪。
"""

from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


class GitHubError(Exception):
    """GitHub API 基础异常。"""

    def __init__(self, message: str, status_code: int = 0, response_data: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class GitHubRateLimitError(GitHubError):
    """超出 GitHub API 速率限制。"""


class GitHubNotFoundError(GitHubError):
    """仓库或资源不存在。"""


class GitHubAuthError(GitHubError):
    """Token 鉴权失败。"""


@dataclass
class RateLimitInfo:
    """GitHub API 速率限制信息。"""

    limit: int = 60
    remaining: int = 60
    reset_timestamp: int = 0
    used: int = 0

    @property
    def reset_datetime(self) -> datetime:
        """重置时间的 UTC datetime。"""
        return datetime.fromtimestamp(self.reset_timestamp, tz=timezone.utc)

    @property
    def reset_time_str(self) -> str:
        """格式化本地/UTC重置时间。"""
        if self.reset_timestamp <= 0:
            return "未知"
        return self.reset_datetime.astimezone().strftime("%Y-%m-%d %H:%M:%S")


class GitHubClient:
    """GitHub REST API 客户端。"""

    def __init__(
        self,
        token: str = "",
        api_base_url: str = "https://api.github.com",
        proxy: str = "",
        timeout: float = 15.0,
        verify_ssl: bool = True,
    ) -> None:
        self.token = token.strip()
        self.api_base_url = api_base_url.rstrip("/")
        self.proxy = proxy.strip() or None
        self.timeout = timeout
        self.verify_ssl = verify_ssl

        self.rate_limit = RateLimitInfo()
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: Dict[Tuple[str, str], Tuple[str, Any]] = {}

    def _get_headers(self, etag: Optional[str] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "MaiBot-GitHub-Subscriber/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag
        return headers

    async def get_client(self) -> httpx.AsyncClient:
        """获取或创建内部 httpx 异步客户端。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.api_base_url,
                proxy=self.proxy,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        return self._client

    async def close(self) -> None:
        """关闭客户端会话。"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _update_rate_limit(self, response: httpx.Response) -> None:
        headers = response.headers
        try:
            if "x-ratelimit-limit" in headers:
                self.rate_limit.limit = int(headers["x-ratelimit-limit"])
            if "x-ratelimit-remaining" in headers:
                self.rate_limit.remaining = int(headers["x-ratelimit-remaining"])
            if "x-ratelimit-reset" in headers:
                self.rate_limit.reset_timestamp = int(headers["x-ratelimit-reset"])
            if "x-ratelimit-used" in headers:
                self.rate_limit.used = int(headers["x-ratelimit-used"])
        except (ValueError, TypeError) as exc:
            logger.debug("解析速率限制响应头失败: %s", exc)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        etag: Optional[str] = None,
    ) -> Tuple[Optional[Any], Optional[str], bool]:
        """发起 HTTP 请求。

        Returns:
            Tuple[Optional[Any], Optional[str], bool]:
                (数据, 最新ETag, 是否未修改304)
        """
        client = await self.get_client()
        cache_key = (path, str(sorted((params or {}).items())))
        cached = self._cache.get(cache_key) if etag is None else None
        headers = self._get_headers(etag or (cached[0] if cached else None))
        url = f"{self.api_base_url}/{path.lstrip('/')}"

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = await client.request(method, url, headers=headers, params=params)
                self._update_rate_limit(response)

                if response.status_code == 304:
                    # 资源未发生变化
                    if cached:
                        return cached[1], cached[0], False
                    return None, etag, True

                new_etag = response.headers.get("etag")

                if response.status_code == 401:
                    raise GitHubAuthError("GitHub 认证失败，请检查 Token 是否有效", 401)
                if response.status_code in (403, 429):
                    msg = response.text
                    if response.status_code == 429 or self.rate_limit.remaining == 0 or "rate limit" in msg.lower() or response.headers.get("retry-after"):
                        raise GitHubRateLimitError(
                            f"GitHub API 请求频率已超限（剩余 {self.rate_limit.remaining}/{self.rate_limit.limit}，重置时间：{self.rate_limit.reset_time_str}）",
                            403,
                        )
                    raise GitHubError("GitHub 请求被拒绝，请检查仓库读取权限 (403)", 403)
                if response.status_code == 404:
                    raise GitHubNotFoundError(f"未找到目标仓库或资源 (404): {path}", 404)

                response.raise_for_status()
                data = response.json()
                if new_etag:
                    if len(self._cache) >= 256:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[cache_key] = (new_etag, data)
                return data, new_etag, False

            except httpx.HTTPStatusError as exc:
                raise GitHubError(f"GitHub HTTP 请求失败 ({exc.response.status_code})", exc.response.status_code) from exc
            except (ValueError, httpx.RemoteProtocolError) as exc:
                raise GitHubError("GitHub 响应格式不正确") from exc
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                raise GitHubError(f"GitHub 网络连接异常: {type(exc).__name__}") from exc

        raise GitHubError(f"GitHub 网络连接异常: {last_exc}")

    async def get_rate_limit(self) -> RateLimitInfo:
        """获取当前 API 速率限制详情。"""
        data, _, _ = await self._request("GET", "/rate_limit")
        if not isinstance(data, dict) or "resources" not in data:
            raise GitHubError("GitHub 速率限制响应格式不正确")
        core = data["resources"].get("core", {})
        self.rate_limit.limit = core.get("limit", self.rate_limit.limit)
        self.rate_limit.remaining = core.get("remaining", self.rate_limit.remaining)
        self.rate_limit.reset_timestamp = core.get("reset", self.rate_limit.reset_timestamp)
        self.rate_limit.used = core.get("used", self.rate_limit.used)
        return self.rate_limit

    async def get_repo_info(self, repo: str) -> Dict[str, Any]:
        """获取仓库基本信息（用于连通性校验与默认分支获取）。"""
        data, _, _ = await self._request("GET", f"/repos/{repo}")
        return data or {}

    async def get_releases(
        self,
        repo: str,
        etag: Optional[str] = None,
        per_page: int = 5,
    ) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
        """获取仓库 Releases 列表。"""
        data, new_etag, not_modified = await self._request(
            "GET",
            f"/repos/{repo}/releases",
            params={"per_page": per_page},
            etag=etag,
        )
        if not_modified:
            return [], etag, True
        return (data or []) if isinstance(data, list) else [], new_etag, False

    async def get_commits(
        self,
        repo: str,
        sha: Optional[str] = None,
        author: Optional[str] = None,
        since: Optional[str] = None,
        etag: Optional[str] = None,
        per_page: int = 10,
    ) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
        """获取仓库指定分支/条件的 Commits。"""
        params: Dict[str, Any] = {"per_page": per_page}
        if sha:
            params["sha"] = sha
        if author:
            params["author"] = author
        if since:
            params["since"] = since

        data, new_etag, not_modified = await self._request(
            "GET",
            f"/repos/{repo}/commits",
            params=params,
            etag=etag,
        )
        if not_modified:
            return [], etag, True
        return (data or []) if isinstance(data, list) else [], new_etag, False

    async def get_issues(
        self,
        repo: str,
        since: Optional[str] = None,
        etag: Optional[str] = None,
        state: str = "all",
        per_page: int = 15,
    ) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
        """获取仓库 Issues 列表（包含 Issue 与 PR，需过滤 pull_request 键）。"""
        params: Dict[str, Any] = {
            "state": state,
            "sort": "updated",
            "direction": "desc",
            "per_page": per_page,
        }
        if since:
            params["since"] = since

        data, new_etag, not_modified = await self._request(
            "GET",
            f"/repos/{repo}/issues",
            params=params,
            etag=etag,
        )
        if not_modified:
            return [], etag, True

        raw_list = (data or []) if isinstance(data, list) else []
        # GitHub 的 /issues 接口默认包含 PR，这里仅筛选纯 issues
        pure_issues = [item for item in raw_list if "pull_request" not in item]
        return pure_issues, new_etag, False

    async def get_pull_requests(
        self,
        repo: str,
        etag: Optional[str] = None,
        state: str = "all",
        per_page: int = 15,
    ) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
        """获取仓库 Pull Requests 列表。"""
        params: Dict[str, Any] = {
            "state": state,
            "sort": "updated",
            "direction": "desc",
            "per_page": per_page,
        }
        data, new_etag, not_modified = await self._request(
            "GET",
            f"/repos/{repo}/pulls",
            params=params,
            etag=etag,
        )
        if not_modified:
            return [], etag, True
        return (data or []) if isinstance(data, list) else [], new_etag, False

    async def get_branches(self, repo: str) -> List[str]:
        """读取分支供配置页勾选；最多 1000 个。"""
        names = []
        for page in range(1, 11):
            data, _, _ = await self._request("GET", f"/repos/{repo}/branches", params={"per_page": 100, "page": page})
            if not isinstance(data, list):
                raise GitHubError("GitHub 分支响应格式不正确")
            names.extend(str(item["name"]) for item in data if isinstance(item, dict) and item.get("name"))
            if len(data) < 100:
                break
        return names

    async def get_tags(
        self,
        repo: str,
        etag: Optional[str] = None,
        per_page: int = 10,
    ) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
        """获取仓库 Tags 列表。"""
        data, new_etag, not_modified = await self._request(
            "GET",
            f"/repos/{repo}/tags",
            params={"per_page": per_page},
            etag=etag,
        )
        if not_modified:
            return [], etag, True
        return (data or []) if isinstance(data, list) else [], new_etag, False
