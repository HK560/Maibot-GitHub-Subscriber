"""后台轮询引擎：拉取 GitHub 事件、过滤并投递通知。"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .client import GitHubAuthError, GitHubClient, GitHubError, GitHubNotFoundError, GitHubRateLimitError
from .models import Event
from .notify import Notifier
from .state import StateStore

logger = logging.getLogger(__name__)


def _iso_ago(seconds: float) -> str:
    dt = datetime.fromtimestamp(time.time() - seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_ts(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_line(text: str) -> str:
    text = str(text or "").strip()
    return text.splitlines()[0].strip() if text else ""


class SubscriptionEngine:
    """按配置定时轮询所有订阅仓库并推送变化。"""

    def __init__(self, ctx: Any, get_config: Any, state: StateStore, notifier: Notifier) -> None:
        self.ctx = ctx
        self.get_config = get_config
        self.state = state
        self.notifier = notifier

        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[GitHubClient] = None
        self._client_sig: Optional[Tuple[Any, ...]] = None
        self._poll_lock = asyncio.Lock()
        self._rate_limit_until = 0.0
        self._last_run = 0.0
        self._last_stats: Dict[str, Any] = {}
        self._consecutive_errors = 0
        self._paused = False

    # ------------------------------------------------------------------ 客户端
    def _client_signature(self, cfg: Any) -> Tuple[Any, ...]:
        g = cfg.github
        return (g.token, g.api_base_url, g.proxy, g.verify_ssl, g.request_timeout_seconds)

    async def _get_client(self, cfg: Any) -> GitHubClient:
        sig = self._client_signature(cfg)
        if self._client is None or self._client_sig != sig:
            if self._client is not None:
                await self._client.close()
            self._client = GitHubClient(
                token=cfg.github.token,
                api_base_url=cfg.github.api_base_url,
                proxy=cfg.github.proxy,
                timeout=float(cfg.github.request_timeout_seconds),
                verify_ssl=bool(cfg.github.verify_ssl),
            )
            self._client_sig = sig
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._client_sig = None

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="github-subscriber-engine")
        logger.info("GitHub 订阅引擎已启动")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self.close()
        logger.info("GitHub 订阅引擎已停止")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    async def _run_loop(self) -> None:
        cfg = self.get_config()
        delay = float(getattr(cfg.notify, "startup_delay_seconds", 20) or 0)
        if delay > 0:
            await self._sleep(delay)
        while not self._stop_event.is_set():
            try:
                cfg = self.get_config()
                if not cfg.plugin.enabled or self._paused:
                    await self._sleep(30)
                    continue
                if time.time() < self._rate_limit_until:
                    await self._sleep(max(5.0, self._rate_limit_until - time.time()))
                    continue
                await self.poll_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._consecutive_errors += 1
                logger.error("轮询循环异常: %s", exc, exc_info=True)
            # 计算下一轮等待
            cfg = self.get_config()
            interval = max(60, int(getattr(cfg.polling, "interval_seconds", 300) or 300))
            jitter = max(0, int(getattr(cfg.polling, "jitter_seconds", 0) or 0))
            wait = interval + (random.uniform(0, jitter) if jitter else 0)
            await self._sleep(wait)

    # ------------------------------------------------------------------ 轮询
    async def poll_all(self, *, force: bool = False) -> Dict[str, Any]:
        """轮询所有启用仓库；返回本轮统计。"""

        async with self._poll_lock:
            cfg = self.get_config()
            client = await self._get_client(cfg)
            repos = [r for r in cfg.subscriptions.repos if r.enabled and r.repo]
            semaphore = asyncio.Semaphore(max(1, int(cfg.polling.max_concurrent_repos)))

            stats: Dict[str, Any] = {
                "repos": len(repos),
                "events": 0,
                "sent": 0,
                "failed": 0,
                "skipped": 0,
                "unknown": 0,
                "errors": [],
                "ran_at": time.time(),
            }

            if not cfg.plugin.enabled:
                return stats
            if time.time() < self._rate_limit_until:
                stats["errors"].append("触发 GitHub 速率限制，跳过轮询")
                return stats

            async def worker(repo_cfg: Any) -> None:
                async with semaphore:
                    try:
                        events = await self._poll_repo(client, cfg, repo_cfg, force=force)
                        stats["events"] += len(events)
                    except GitHubRateLimitError as exc:
                        self._rate_limit_until = max(
                            self._rate_limit_until,
                            max(time.time() + 60, client.rate_limit.reset_timestamp + 5),
                        )
                        stats["errors"].append(f"{repo_cfg.repo}: {exc}")
                        logger.warning("触发 GitHub 速率限制，暂停到 %s", client.rate_limit.reset_time_str)
                        return
                    except GitHubAuthError as exc:
                        stats["errors"].append(f"{repo_cfg.repo}: {exc}")
                        logger.error("GitHub 认证失败: %s", exc)
                        return
                    except (GitHubNotFoundError, GitHubError) as exc:
                        backoff = int(getattr(cfg.polling, "error_backoff_seconds", 120) or 120)
                        state = self.state.repo_state(repo_cfg.repo)
                        state["error_until"] = time.time() + backoff
                        self.state.mark_poll(repo_cfg.repo, success=False, error=str(exc))
                        self.state.save()
                        stats["errors"].append(f"{repo_cfg.repo}: {exc}")
                        logger.warning("仓库 %s 轮询失败: %s", repo_cfg.repo, exc)
                        return
                    except Exception as exc:  # noqa: BLE001
                        stats["errors"].append(f"{repo_cfg.repo}: {exc}")
                        logger.error("仓库 %s 轮询异常: %s", repo_cfg.repo, exc, exc_info=True)
                        return

                    # 无论本轮是否有新事件，都尝试投递/重试该仓库出件箱中的事件
                    await self._deliver_outbox(repo_cfg.repo, cfg, stats)

            tasks = [asyncio.create_task(worker(repo_cfg)) for repo_cfg in repos]
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        stats["errors"].append(f"Worker exception: {r}")
                        logger.error("Worker 发生未捕获异常: %s", r, exc_info=r)
            self.state.cleanup(int(getattr(cfg.polling, "state_retention_days", 30) or 30))
            self.state.save()
            self._last_run = time.time()
            self._last_stats = stats
            if stats["events"] or stats["sent"] or stats["failed"]:
                logger.info(
                    "本轮轮询完成：仓库 %d，新事件 %d，发送目标成功 %d 失败 %d 未知 %d 跳过 %d",
                    stats["repos"], stats["events"], stats["sent"], stats["failed"], stats["unknown"], stats["skipped"],
                )
            return stats

    @property
    def last_run(self) -> float:
        return self._last_run

    @property
    def last_stats(self) -> Dict[str, Any]:
        return dict(self._last_stats)

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, value: bool) -> None:
        self._paused = bool(value)

    @property
    def rate_limit_until(self) -> float:
        return self._rate_limit_until

    # ------------------------------------------------------------------ 出件箱投递
    async def _deliver_outbox(self, repo: str, cfg: Any, stats: Dict[str, Any]) -> None:
        """投递指定仓库出件箱中的所有未完成事件，按单目标独立隔离投递。"""
        if self.notifier is None:
            return

        state = self.state.repo_state(repo)
        outbox = state.get("outbox", [])
        if not outbox:
            return

        current_cfg = self.get_config()
        if not current_cfg.plugin.enabled:
            return
        repo_cfg = next((r for r in current_cfg.subscriptions.repos if r.repo == repo and r.enabled), None)
        if repo_cfg is None:
            return

        current_dests = set(self.notifier.destinations(current_cfg, repo_cfg))
        current_platform = str(getattr(current_cfg.notify, "platform", "qq") or "qq")
        terminal_statuses = {"delivered", "terminal", "unknown", "skipped"}

        for item in list(outbox):
            item_platform = str(item.get("platform") or "qq")
            if item_platform != current_platform:
                logger.info(
                    "Outbox 条目 %s 属于平台 %s，与当前平台 %s 不一致，跳过投递",
                    item.get("id"),
                    item_platform,
                    current_platform,
                )
                continue

            events = [Event(**e_data) for e_data in item.get("events", [])]
            receipts = item.get("receipts", {})

            for target_key, receipt in list(receipts.items()):
                kind = receipt.get("kind", "")
                target_id = str(receipt.get("target_id", ""))
                dest_pair = (kind, target_id)
                status = receipt.get("status", "pending")

                if status in terminal_statuses:
                    continue

                if dest_pair not in current_dests:
                    receipt["status"] = "skipped"
                    receipt["error"] = "目标已从当前通知配置中移除"
                    stats["skipped"] = stats.get("skipped", 0) + 1
                    self.state.save()
                    continue

                # 如果先前状态为 sending，说明上次运行在发送中途崩溃/中断；标记 unknown 终止，避免重复重发
                if status == "sending":
                    receipt["status"] = "unknown"
                    receipt["error"] = "上次发送过程中断"
                    stats["unknown"] = stats.get("unknown", 0) + 1
                    self.state.save()
                    continue

                retries = int(receipt.get("retries", 0))
                if retries >= 3:
                    receipt["status"] = "terminal"
                    receipt["error"] = "超过最大重试次数 (3)"
                    self.state.save()
                    continue

                # 发送前先落盘 sending 状态
                receipt["status"] = "sending"
                receipt["last_attempt"] = time.time()
                self.state.save()

                try:
                    res = await self.notifier.send_events(
                        events,
                        current_cfg,
                        repo=repo_cfg,
                        targets=[dest_pair],
                    )
                    sent = int(res.get("sent", 0))
                    unknown = int(res.get("unknown", 0))
                    skipped = int(res.get("skipped", 0))
                    failed = int(res.get("failed", 0))

                    if sent > 0:
                        receipt["status"] = "delivered"
                        receipt["error"] = ""
                        stats["sent"] = stats.get("sent", 0) + 1
                    elif unknown > 0:
                        receipt["status"] = "unknown"
                        receipt["retries"] = retries + 1
                        stats["unknown"] = stats.get("unknown", 0) + 1
                    elif skipped > 0:
                        receipt["status"] = "skipped"
                        stats["skipped"] = stats.get("skipped", 0) + 1
                    elif failed > 0:
                        receipt["status"] = "failed"
                        receipt["retries"] = retries + 1
                        stats["failed"] = stats.get("failed", 0) + 1
                        if receipt["retries"] >= 3:
                            receipt["status"] = "terminal"
                    else:
                        receipt["status"] = "failed"
                        receipt["retries"] = retries + 1
                        stats["failed"] = stats.get("failed", 0) + 1
                        if receipt["retries"] >= 3:
                            receipt["status"] = "terminal"
                except Exception as exc:
                    logger.warning("投递事件至 %s %s 异常: %s", kind, target_id, exc)
                    receipt["status"] = "unknown"
                    receipt["error"] = str(exc)
                    receipt["retries"] = retries + 1
                    stats["unknown"] = stats.get("unknown", 0) + 1

                self.state.save()

        self.state.prune_outbox(repo)
        self.state.save()

    # ------------------------------------------------------------------ 单仓库
    async def _poll_repo(
        self, client: GitHubClient, cfg: Any, repo_cfg: Any, *, force: bool = False
    ) -> List[Event]:
        state = self.state.repo_state(repo_cfg.repo)
        if not repo_cfg.enabled:
            return []
        if not force and state.get("error_until", 0) > time.time():
            return []

        repo = repo_cfg.repo
        per_page = int(getattr(cfg.github, "per_page", 15) or 15)
        now = time.time()

        # 确定默认分支（支持 UI 选项带 '<repo> :: <branch>' 前缀）
        raw_branches = [
            b.split(" :: ", 1)[-1].strip()
            for b in (repo_cfg.branches or [])
            if b and str(b).strip()
        ]
        branches = [b for b in raw_branches if b]
        if not branches:
            default_branch = state.get("default_branch") or ""
            if not default_branch:
                info = await client.get_repo_info(repo)
                default_branch = str(info.get("default_branch") or "main")
                state["default_branch"] = default_branch
            branches = [default_branch]

        baseline = not bool(state.get("initialized"))
        notify_first = bool(getattr(cfg.notify, "notify_on_first_run", False))
        max_events = int(getattr(cfg.polling, "max_events_per_repo_per_run", 5) or 5)
        last_success = float(state.get("last_success") or 0)

        lookback_cutoff_iso = ""
        if baseline:
            if notify_first:
                lookback_hours = int(getattr(cfg.polling, "first_run_lookback_hours", 72) or 72)
                since = _iso_ago(lookback_hours * 3600)
                lookback_cutoff_iso = since
            else:
                since = None
        else:
            since = _iso_ago(300) if last_success <= 0 else _iso_from_ts(last_success - 300)

        # 暂存区：若任一接口异常，不会向 state 写入部分状态（事务性回滚）
        staged_branch_commits: Dict[str, List[str]] = {}
        staged_releases: List[str] = []
        staged_tags: List[str] = []
        staged_issues: Dict[str, Dict[str, str]] = {}
        staged_prs: Dict[str, Dict[str, Any]] = {}
        staged_issue_numbers: List[str] = []
        staged_pr_numbers: List[str] = []
        staged_issue_watermark = ""
        staged_pr_watermark = ""

        collected: List[Event] = []
        seen_shas_in_run: Set[str] = set()
        warnings: List[str] = []

        # --- Commits
        if repo_cfg.watch_commits:
            for branch in branches:
                try:
                    commits, _etag, _nm = await client.get_commits(
                        repo,
                        sha=branch,
                        since=since,
                        per_page=per_page,
                    )
                except GitHubNotFoundError as exc:
                    msg = f"分支 {branch} 不存在或不可访问 (404)"
                    logger.warning("仓库 %s %s: %s", repo, msg, exc)
                    warnings.append(msg)
                    continue
                known = set(self.state.known_commits(state, branch))
                branch_events: List[Event] = []
                for item in commits:
                    sha = str(item.get("sha") or "")
                    if not sha or sha in known or sha in seen_shas_in_run:
                        continue
                    event = self._commit_event(repo, repo_cfg, cfg, item, branch=branch)
                    if event is not None:
                        if lookback_cutoff_iso and event.event_time and event.event_time < lookback_cutoff_iso:
                            continue
                        seen_shas_in_run.add(sha)
                        branch_events.append(event)
                branch_events.sort(key=lambda e: e.event_time)
                if not baseline or notify_first:
                    collected.extend(branch_events)
                staged_branch_commits[branch] = [str(item.get("sha") or "") for item in commits if item.get("sha")]

        # --- Releases
        if repo_cfg.watch_releases:
            releases, _etag, _nm = await client.get_releases(repo, per_page=per_page)
            known_releases = set(state.get("releases", []))
            release_events: List[Event] = []
            for item in releases:
                rid = str(item.get("id") or "")
                if not rid or rid in known_releases:
                    continue
                if item.get("draft"):
                    continue
                event = self._release_event(repo, repo_cfg, cfg, item)
                if event is not None:
                    if lookback_cutoff_iso and event.event_time and event.event_time < lookback_cutoff_iso:
                        continue
                    release_events.append(event)
            release_events.sort(key=lambda e: e.event_time)
            if not baseline or notify_first:
                collected.extend(release_events)
            staged_releases = [str(item.get("id")) for item in releases if item.get("id") and not item.get("draft")]

        # --- Tags
        if repo_cfg.watch_tags:
            tags, _etag, _nm = await client.get_tags(repo, per_page=per_page)
            known_tags = set(state.get("tags", []))
            tag_events: List[Event] = []
            for item in tags:
                name = str(item.get("name") or "")
                if not name or name in known_tags:
                    continue
                event = self._tag_event(repo, repo_cfg, cfg, item)
                if event is not None:
                    tag_events.append(event)
            if not baseline or notify_first:
                collected.extend(tag_events)
            staged_tags = [str(item.get("name")) for item in tags if item.get("name")]

        # --- Issues
        if repo_cfg.watch_issues:
            issues_since = None
            if baseline and notify_first and lookback_cutoff_iso:
                issues_since = lookback_cutoff_iso
            elif last_success > 0:
                issues_since = _iso_from_ts(last_success - 120)
            issues, _etag, _nm = await client.get_issues(
                repo, since=issues_since, state="all", per_page=per_page
            )
            prior = dict(state.get("issues", {}))
            issue_seen = set(state.get("seen_issues", []))
            issue_watermark = self._watermark(state, prior, "issue_watermark")
            issue_events: List[Event] = []
            for item in issues:
                number = str(item.get("number") or "")
                if not number:
                    continue
                current = str(item.get("state") or "")
                updated_at = str(item.get("updated_at") or "")
                prev = prior.get(number)
                event: Optional[Event] = None
                if prev is None:
                    if not baseline or notify_first:
                        fresh = self._is_fresh(number, updated_at, issue_seen, issue_watermark)
                        if current == "open":
                            if fresh:
                                event = self._issue_event(repo, repo_cfg, cfg, item, action="opened")
                        elif current == "closed" and repo_cfg.issue_state == "all" and repo_cfg.notify_issue_close:
                            # 关闭动作会刷新 updated_at：即使编号墓碑里已有此条目，
                            # 只要关闭发生在上一轮成功轮询之后，仍应补发一次关闭通知。
                            if self._is_recent(updated_at, last_success):
                                event = self._issue_event(repo, repo_cfg, cfg, item, action="closed")
                else:
                    prev_state = str(prev.get("state") or "")
                    if current == "closed" and prev_state == "open":
                        if repo_cfg.issue_state == "all" and repo_cfg.notify_issue_close:
                            event = self._issue_event(repo, repo_cfg, cfg, item, action="closed")
                    elif current == "open" and prev_state == "closed":
                        event = self._issue_event(repo, repo_cfg, cfg, item, action="reopened")
                if event is not None:
                    if lookback_cutoff_iso and event.event_time and event.event_time < lookback_cutoff_iso:
                        pass
                    else:
                        issue_events.append(event)
                prior[number] = {"state": current, "updated_at": updated_at}
            issue_events.sort(key=lambda e: e.event_time)
            collected.extend(issue_events)
            staged_issues = prior
            staged_issue_numbers = list(prior.keys())
            staged_issue_watermark = _iso_from_ts(issue_watermark) if issue_watermark > 0 else ""

        # --- Pull Requests
        if repo_cfg.watch_prs:
            prs, _etag, _nm = await client.get_pull_requests(repo, state="all", per_page=per_page)
            prior_prs = dict(state.get("prs", {}))
            pr_seen = set(state.get("seen_prs", []))
            pr_watermark = self._watermark(state, prior_prs, "pr_watermark")
            pr_events: List[Event] = []
            for item in prs:
                number = str(item.get("number") or "")
                if not number:
                    continue
                if bool(getattr(cfg.filters, "ignore_drafts", True)) and item.get("draft"):
                    continue
                current = str(item.get("state") or "")
                merged = item.get("merged_at") is not None
                updated_at = str(item.get("updated_at") or "")
                prev = prior_prs.get(number)
                event: Optional[Event] = None
                if prev is None:
                    if not baseline or notify_first:
                        fresh = self._is_fresh(number, updated_at, pr_seen, pr_watermark)
                        if current == "open" and not merged:
                            # 新开 PR 必须通过墓碑/水位校验，避免清理后旧 PR 反复通知
                            if fresh:
                                event = self._pr_event(repo, repo_cfg, cfg, item, action="opened")
                        elif repo_cfg.issue_state == "all" and self._is_recent(updated_at, last_success):
                            # 合并/关闭会刷新 updated_at：上一轮成功轮询之后发生的仍要补发
                            if merged and repo_cfg.notify_pr_merge:
                                event = self._pr_event(repo, repo_cfg, cfg, item, action="merged")
                            elif current == "closed" and not merged:
                                event = self._pr_event(repo, repo_cfg, cfg, item, action="closed")
                else:
                    prev_merged = bool(prev.get("merged"))
                    prev_state = str(prev.get("state") or "")
                    if merged and not prev_merged:
                        if repo_cfg.issue_state == "all" and repo_cfg.notify_pr_merge:
                            event = self._pr_event(repo, repo_cfg, cfg, item, action="merged")
                    elif current == "closed" and prev_state == "open" and not prev_merged:
                        if repo_cfg.issue_state == "all":
                            event = self._pr_event(repo, repo_cfg, cfg, item, action="closed")
                    elif current == "open" and prev_state == "closed":
                        event = self._pr_event(repo, repo_cfg, cfg, item, action="reopened")
                if event is not None:
                    if lookback_cutoff_iso and event.event_time and event.event_time < lookback_cutoff_iso:
                        pass
                    else:
                        pr_events.append(event)
                prior_prs[number] = {"state": current, "merged": merged, "updated_at": updated_at}
            pr_events.sort(key=lambda e: e.event_time)
            collected.extend(pr_events)
            staged_prs = prior_prs
            staged_pr_numbers = list(prior_prs.keys())
            staged_pr_watermark = _iso_from_ts(pr_watermark) if pr_watermark > 0 else ""

        # 全局按事件时间升序排序（保证跨类型事件的时间顺序）
        collected.sort(key=lambda e: e.event_time or "")

        # 限制单轮数量：保留最新 max_events 条
        if len(collected) > max_events:
            collected = collected[-max_events:]

        # 写入 Outbox（如果有待发送事件且配置了目标，无目标时直接丢弃避免积压）
        if (not baseline or notify_first) and collected:
            dests = self.notifier.destinations(cfg, repo_cfg) if self.notifier is not None else []
            if dests:
                platform = str(getattr(cfg.notify, "platform", "qq") or "qq")
                self.state.add_to_outbox(repo, collected, dests, platform=platform)
            else:
                logger.debug("仓库 %s 本轮产生 %d 条事件，但无有效通知目标，丢弃出件", repo, len(collected))

        # 事务性提交本轮进度至持久化状态
        for branch, shas in staged_branch_commits.items():
            self.state.remember_commits(state, branch, shas)
        if repo_cfg.watch_releases and staged_releases:
            self.state.remember_releases(state, staged_releases)
        if repo_cfg.watch_tags and staged_tags:
            self.state.remember_tags(state, staged_tags)
        if repo_cfg.watch_issues:
            state["issues"] = staged_issues
            # 编号墓碑 + 水位在清理后仍需保留，避免旧条目被重新当作新开
            self.state.remember_numbers(state, "seen_issues", staged_issue_numbers)
            if staged_issue_watermark:
                state["issue_watermark"] = staged_issue_watermark
        if repo_cfg.watch_prs:
            state["prs"] = staged_prs
            self.state.remember_numbers(state, "seen_prs", staged_pr_numbers)
            if staged_pr_watermark:
                state["pr_watermark"] = staged_pr_watermark

        state["initialized"] = True
        state["last_warning"] = "；".join(warnings) if warnings else ""
        self.state.mark_poll(repo, success=True)
        self.state.save()
        return collected

    # ------------------------------------------------------------------ 事件构造
    def _commit_event(
        self, repo: str, repo_cfg: Any, cfg: Any, item: Dict[str, Any], *, branch: str = ""
    ) -> Optional[Event]:
        commit = item.get("commit") or {}
        author_info = item.get("author") or {}
        login = str(author_info.get("login") or "")
        email = str((commit.get("author") or {}).get("email") or "")
        name = str((commit.get("author") or {}).get("name") or "")
        if not self._author_allowed(repo_cfg, cfg, [login, email, name]):
            return None
        message = str(commit.get("message") or "")
        title = _first_line(message) or str(item.get("sha", "")[:8])
        body = message if "\n" in message else ""
        if self._keyword_hit(cfg, repo_cfg, title + "\n" + message):
            return None
        extra: Dict[str, Any] = {"branch": branch} if branch else {}
        return Event(
            kind="commit",
            repo=repo,
            title=title,
            body=body,
            author=login or name or email,
            event_time=str((commit.get("author") or {}).get("date") or ""),
            url=str(item.get("html_url") or f"https://github.com/{repo}/commit/{item.get('sha', '')}"),
            key=str(item.get("sha") or ""),
            extra=extra,
        )

    def _release_event(self, repo: str, repo_cfg: Any, cfg: Any, item: Dict[str, Any]) -> Optional[Event]:
        author = str((item.get("author") or {}).get("login") or "")
        if not self._author_allowed(repo_cfg, cfg, [author]):
            return None
        title = str(item.get("name") or item.get("tag_name") or "").strip()
        body = str(item.get("body") or "")
        if self._keyword_hit(cfg, repo_cfg, title + "\n" + body):
            return None
        action = "预发布" if item.get("prerelease") else "正式发布"
        return Event(
            kind="release",
            repo=repo,
            title=title or str(item.get("tag_name") or ""),
            body=body,
            author=author,
            action=action,
            event_time=str(item.get("published_at") or item.get("created_at") or ""),
            url=str(item.get("html_url") or f"https://github.com/{repo}/releases"),
            key=str(item.get("id") or ""),
            extra={"tag": str(item.get("tag_name") or "")},
        )

    def _tag_event(self, repo: str, repo_cfg: Any, cfg: Any, item: Dict[str, Any]) -> Optional[Event]:
        name = str(item.get("name") or "")
        if not name:
            return None
        if self._keyword_hit(cfg, repo_cfg, name):
            return None
        return Event(
            kind="tag",
            repo=repo,
            title=name,
            url=f"https://github.com/{repo}/releases/tag/{name}",
            key=name,
        )

    def _issue_event(
        self, repo: str, repo_cfg: Any, cfg: Any, item: Dict[str, Any], *, action: str
    ) -> Optional[Event]:
        user = str((item.get("user") or {}).get("login") or "")
        if not self._author_allowed(repo_cfg, cfg, [user]):
            return None
        title = str(item.get("title") or "")
        body = str(item.get("body") or "")
        if self._keyword_hit(cfg, repo_cfg, title + "\n" + body):
            return None
        action_label = {"opened": "新开", "closed": "已关闭", "reopened": "重新打开"}.get(action, action)
        return Event(
            kind="issue",
            repo=repo,
            title=f"#{item.get('number')} {title}".strip(),
            body=body,
            author=user,
            action=action_label,
            event_time=str(item.get("updated_at") or item.get("created_at") or ""),
            url=str(item.get("html_url") or ""),
            key=f"issue-{item.get('number')}-{action}",
            extra={"number": str(item.get("number") or "")},
        )

    def _pr_event(
        self, repo: str, repo_cfg: Any, cfg: Any, item: Dict[str, Any], *, action: str
    ) -> Optional[Event]:
        user = str((item.get("user") or {}).get("login") or "")
        if not self._author_allowed(repo_cfg, cfg, [user]):
            return None
        title = str(item.get("title") or "")
        body = str(item.get("body") or "")
        if self._keyword_hit(cfg, repo_cfg, title + "\n" + body):
            return None
        action_label = {"opened": "新开", "closed": "已关闭", "merged": "已合并", "reopened": "重新打开"}.get(action, action)
        return Event(
            kind="pr",
            repo=repo,
            title=f"#{item.get('number')} {title}".strip(),
            body=body,
            author=user,
            action=action_label,
            event_time=str(item.get("updated_at") or item.get("created_at") or ""),
            url=str(item.get("html_url") or ""),
            key=f"pr-{item.get('number')}-{action}",
            extra={"number": str(item.get("number") or "")},
        )

    # ------------------------------------------------------------------ 过滤
    @staticmethod
    def _watermark(state: Dict[str, Any], prior: Dict[str, Any], key: str) -> float:
        """上一轮已知 Issue/PR 的最新 updated_at（秒）。

        只取本轮开始前就已记录的数据，因此真正的新条目一定 ``>=`` 该水位；
        翻页带进来的旧条目则通常低于水位，不会被误当作新开。
        """

        values = [StateStore.parse_iso_ts(str(state.get(key) or ""))]
        for meta in prior.values():
            if isinstance(meta, dict):
                values.append(StateStore.parse_iso_ts(str(meta.get("updated_at") or "")))
        return max(values)

    @staticmethod
    def _is_fresh(number: str, updated_at: str, seen: Set[str], watermark: float) -> bool:
        """判断首次出现（prev 为 None）的 Issue/PR 是否应视为新事件。

        两种旧条目必须被抑制：
        1. 保留期清理删掉了状态，但条目仍在第一页 —— 由 ``seen`` 编号墓碑识别；
        2. 条目首次翻进第一页 —— 其 ``updated_at`` 低于已知水位。
        真正的新条目既不在墓碑中，时间也不会旧于水位。
        """

        if number in seen:
            return False
        if watermark <= 0:
            return True
        ts = StateStore.parse_iso_ts(updated_at)
        if ts <= 0:
            # 缺少可解析时间时放行，交由编号墓碑兜底
            return True
        return ts >= watermark

    @staticmethod
    def _is_recent(updated_at: str, last_success: float) -> bool:
        """条目是否在上一轮成功轮询之后（含 120 秒重叠）发生变化。"""

        if last_success <= 0:
            return True
        ts = StateStore.parse_iso_ts(updated_at)
        if ts <= 0:
            return False
        return ts >= last_success - 120

    @staticmethod
    def _author_allowed(repo_cfg: Any, cfg: Any, candidates: List[str]) -> bool:
        norm = [c.strip().lower() for c in candidates if c and c.strip()]
        if not norm:
            # 没有作者信息时，仓库若限定了作者则不推送
            return not bool(repo_cfg.authors)

        include_bot = bool(getattr(cfg.filters, "include_bot_authors", False))
        global_ignore = {a.strip().lower() for a in getattr(cfg.filters, "ignore_authors", []) if a.strip()}
        for candidate in norm:
            if candidate in global_ignore:
                return False
            if not include_bot and candidate.endswith("[bot]"):
                return False
            if not include_bot and candidate.endswith("-bot"):
                return False
        if repo_cfg.authors:
            wanted = {a.strip().lower() for a in repo_cfg.authors if a.strip()}
            if not (set(norm) & wanted):
                return False
        return True

    @staticmethod
    def _keyword_hit(cfg: Any, repo_cfg: Any, text: str) -> bool:
        haystack = str(text or "").lower()
        if not haystack:
            return False
        keywords = list(getattr(cfg.filters, "ignore_keywords", []) or []) + list(
            getattr(repo_cfg, "ignore_keywords", []) or []
        )
        return any(k and k.strip().lower() in haystack for k in keywords)
