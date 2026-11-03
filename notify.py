"""通知目标解析与投递；支持仓库级覆盖，发送前重新读取配置。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from .models import Event, event_forward_node, format_event

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, ctx: Any, get_config: Any) -> None:
        self.ctx = ctx
        self.get_config = get_config

    def destinations(self, config: Any, repo: Any = None) -> List[Tuple[str, str]]:
        """返回某个仓库实际生效的目标；不传仓库时使用全局通知目标。"""

        if repo is None:
            groups = config.notify.group_targets
            privates = config.notify.private_targets
        else:
            groups, privates = config.notify_targets_for(repo)
        return [("group", target) for target in groups] + [
            ("private", target) for target in privates
        ]

    def all_destinations(self, config: Any) -> List[Tuple[str, str]]:
        """返回全局与所有已启用仓库目标的去重合集，供测试/摘要消息使用。"""

        groups, privates = config.all_notify_targets()
        return [("group", target) for target in groups] + [
            ("private", target) for target in privates
        ]

    async def resolve(self, platform: str, kind: str, target_id: str, *, force: bool = False) -> Optional[str]:
        """要求唯一的真实会话；多账号歧义时要求用户选择账号。"""
        current = self.get_config()
        method = self.ctx.chat.get_group_streams if kind == "group" else self.ctx.chat.get_private_streams
        field = "group_id" if kind == "group" else "user_id"
        try:
            streams = await asyncio.wait_for(method(platform=platform), timeout=15)
        except Exception:
            logger.warning("读取 %s 会话失败", kind, exc_info=True)
            return None
        if not isinstance(streams, list):
            return None
        matches = {}
        for stream in streams:
            if not isinstance(stream, dict) or str(stream.get(field)) != target_id:
                continue
            if stream.get("platform", platform) != platform or stream.get("chat_type", kind) != kind:
                continue
            if current.notify.account_id and stream.get("account_id") != current.notify.account_id:
                continue
            if current.notify.scope and stream.get("scope") != current.notify.scope:
                continue
            session_id = stream.get("session_id") or stream.get("stream_id")
            if session_id and stream.get("account_id"):
                matches[session_id] = stream
        if len(matches) == 1:
            return next(iter(matches))
        logger.warning("目标 %s %s 没有唯一真实会话，请先让机器人接收该聊天的消息，或指定发送账号", kind, target_id)
        return None

    @staticmethod
    def _sent(result: Any) -> bool:
        return result is True or (isinstance(result, dict) and result.get("sent") is True)

    async def _send_text(self, text: str, stream_id: str) -> bool:
        result = await asyncio.wait_for(self.ctx.send.text(text, stream_id, return_details=True), timeout=25)
        return self._sent(result)

    async def _send_forward(self, nodes: List[Dict[str, Any]], stream_id: str) -> bool:
        result = await asyncio.wait_for(self.ctx.send.forward(nodes, stream_id, return_details=True), timeout=25)
        return self._sent(result)

    @staticmethod
    def _find_repo(config: Any, repo: Any) -> Any:
        name = str(getattr(repo, "repo", "") or "")
        return next(
            (candidate for candidate in config.subscriptions.repos if candidate.repo == name),
            None,
        )

    def _still_allowed(
        self,
        config: Any,
        kind: str,
        target: str,
        repo: Any = None,
        *,
        any_repo: bool = False,
    ) -> bool:
        current = self.get_config()
        if not current.plugin.enabled or current.notify.platform != config.notify.platform:
            return False
        if repo is not None:
            current_repo = self._find_repo(current, repo)
            if current_repo is None or not current_repo.enabled:
                return False
            return (kind, target) in self.destinations(current, current_repo)
        if any_repo:
            return (kind, target) in self.all_destinations(current)
        return (kind, target) in self.destinations(current)

    async def _deliver(
        self,
        config: Any,
        text: str,
        nodes: Optional[List[Dict[str, Any]]] = None,
        *,
        repo: Any = None,
        targets: Optional[List[Tuple[str, str]]] = None,
    ) -> Dict[str, int]:
        stats = {"sent": 0, "failed": 0, "unknown": 0, "skipped": 0, "targets": 0}
        destinations = list(targets) if targets is not None else self.destinations(config, repo)
        # 显式传入 targets（测试/摘要广播或 Outbox 单目标投递）时，热更新校验范围不同：
        # 仓库出件按该仓库的当前目标校验，跨仓库广播按全体目标校验。
        any_repo = targets is not None and repo is None
        stats["targets"] = len(destinations)
        for kind, target in destinations:
            if not self._still_allowed(config, kind, target, repo, any_repo=any_repo):
                stats["skipped"] += 1
                continue
            current = self.get_config()
            if current.advanced.dry_run:
                logger.info("[演练模式] %s %s: %s", kind, target, text)
                stats["skipped"] += 1
                continue
            stream_id = await self.resolve(config.notify.platform, kind, target)
            if not stream_id:
                stats["failed"] += 1
                continue
            # 查会话会等待 RPC，期间目标或总开关可能热更新。
            if (
                not self._still_allowed(config, kind, target, repo, any_repo=any_repo)
                or self.get_config().advanced.dry_run
            ):
                stats["skipped"] += 1
                continue
            try:
                if nodes:
                    sent = await self._send_forward(nodes, stream_id)
                    # 只有明确返回失败才退回文本；超时/异常结果未知不重发。
                    if not sent and self._still_allowed(config, kind, target, repo, any_repo=any_repo):
                        sent = await self._send_text(text, stream_id)
                else:
                    sent = await self._send_text(text, stream_id)
                stats["sent" if sent else "failed"] += 1
            except Exception:
                stats["unknown"] += 1
                logger.warning("通知发送结果未知，避免重复发送，不自动重试：%s %s", kind, target, exc_info=True)
        return stats

    async def send_events(
        self,
        events: List[Event],
        config: Any,
        *,
        repo: Any = None,
        targets: Optional[List[Tuple[str, str]]] = None,
    ) -> Dict[str, int]:
        text = "\n\n".join(format_event(event, config=config) for event in events)
        nodes = [event_forward_node(event, config=config) for event in events] if config.notify.send_as_forward and len(events) > 1 else None
        return await self._deliver(config, text, nodes, repo=repo, targets=targets) if events else {"sent": 0, "failed": 0, "targets": 0}

    async def send_text(
        self,
        text: str,
        config: Any,
        *,
        repo: Any = None,
        targets: Optional[List[Tuple[str, str]]] = None,
    ) -> Dict[str, int]:
        return await self._deliver(config, text, repo=repo, targets=targets)
