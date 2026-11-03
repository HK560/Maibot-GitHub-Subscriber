"""MaiBot SDK v2 入口：GitHub 仓库订阅通知插件。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Command, ConfigWidget, ConfigWidgetState, MaiBotPlugin, Tool

from .client import GitHubClient, GitHubError
from .config import GithubSubConfig
from .engine import SubscriptionEngine
from .models import format_event
from .notify import Notifier
from .state import StateStore

logger = logging.getLogger(__name__)

COMMAND_PATTERN = (
    r"^/githubsub(?:\s+(?P<action>status|list|check|test|pause|resume|help|rate))?\s*$"
)

HELP_TEXT = (
    "GitHub 订阅命令：\n"
    "/githubsub status  查看运行状态\n"
    "/githubsub list    查看订阅仓库\n"
    "/githubsub check   立即检查一次\n"
    "/githubsub rate    查看 GitHub 速率限制\n"
    "/githubsub test    向通知目标发送测试消息\n"
    "/githubsub pause   暂停后台轮询\n"
    "/githubsub resume  恢复后台轮询"
)


class GithubSubPlugin(MaiBotPlugin):
    """GitHub 仓库订阅通知插件主类。"""

    config_model = GithubSubConfig

    config_widgets = [
        ConfigWidget(id="status", kind="status", label="运行状态"),
        ConfigWidget(id="check_now", kind="action", label="立即检查一次"),
        ConfigWidget(id="send_summary", kind="action", label="立即发送当前仓库订阅信息"),
        ConfigWidget(id="send_test", kind="action", label="立即发送测试通知"),
        ConfigWidget(id="refresh_branches", kind="action", label="读取仓库分支选项"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._state: Optional[StateStore] = None
        self._notifier: Optional[Notifier] = None
        self._engine: Optional[SubscriptionEngine] = None
        self._action_task: Optional[asyncio.Task] = None
        self._action_text = ""
        self._action_tone = "neutral"
        self._branch_options: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------ 生命周期
    async def on_load(self) -> None:
        cfg = self.config
        self._apply_log_level(cfg)
        data_dir = getattr(getattr(self.ctx, "paths", None), "data_dir", None)
        state_path = (data_dir / "state.json") if data_dir else None
        self._state = StateStore(state_path, enabled=bool(cfg.advanced.persist_state))
        self._notifier = Notifier(self.ctx, lambda: self.config)
        self._engine = SubscriptionEngine(self.ctx, lambda: self.config, self._state, self._notifier)
        await self._engine.start()
        repos = [r.repo for r in cfg.subscriptions.repos if r.enabled and r.repo]
        logger.info(
            "GitHub 订阅插件已加载：订阅仓库 %d 个，通知群 %d 个，通知私聊 %d 个",
            len(repos),
            len(cfg.notify.group_targets),
            len(cfg.notify.private_targets),
        )

    async def on_unload(self) -> None:
        if self._action_task is not None:
            self._action_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._action_task
        if self._engine is not None:
            await self._engine.stop()
        self._engine = None
        logger.info("GitHub 订阅插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        cfg = self.config
        self._apply_log_level(cfg)
        if self._state is not None:
            self._state.enabled = bool(cfg.advanced.persist_state)
        if self._engine is not None:
            await self._engine.start()
        logger.info("GitHub 订阅插件配置已更新（版本 %s）", version)

    def _apply_log_level(self, cfg: GithubSubConfig) -> None:
        level = getattr(logging, str(cfg.advanced.log_level or "INFO").upper(), logging.INFO)
        base = __package__ or __name__
        logging.getLogger(base).setLevel(level)
        for suffix in ("engine", "notify", "client", "state"):
            logging.getLogger(f"{base}.{suffix}").setLevel(level)

    def get_webui_config_schema(self, **kwargs: Any) -> Dict[str, Any]:
        schema = super().get_webui_config_schema(**kwargs)
        choices = sorted({f"{repo} :: {branch}" for repo, branches in self._branch_options.items() for branch in branches})
        if self._plugin_config_instance is not None:
            choices = sorted(set(choices) | {branch for repo in self.config.subscriptions.repos for branch in repo.branches})
        if self._branch_options and choices:
            field = schema["sections"]["subscriptions"]["fields"]["repos"]["item_fields"]["branches"]
            field.update(type="select", choices=choices, multiple=True)
        return schema

    # ------------------------------------------------------------------ 运行时控件
    async def get_config_widget_state(self) -> Dict[str, ConfigWidgetState]:
        states: Dict[str, ConfigWidgetState] = {}
        cfg = self.config
        repos = [r for r in cfg.subscriptions.repos if r.enabled and r.repo]
        if self._engine is not None and self._engine.paused:
            run_tone, run_text = "warning", "已暂停（/githubsub resume 恢复）"
        elif not cfg.plugin.enabled:
            run_tone, run_text = "neutral", "插件已关闭"
        else:
            run_tone, run_text = "success", "运行中"
        last_run = self._engine.last_run if self._engine else 0
        last_run_text = (
            datetime.fromtimestamp(last_run).strftime("%Y-%m-%d %H:%M:%S") if last_run else "尚未轮询"
        )
        states["status"] = ConfigWidgetState(
            text=(
                f"{run_text}｜订阅仓库 {len(repos)} 个｜间隔 {cfg.polling.interval_seconds}s\n"
                f"通知群 {len(cfg.notify.group_targets)} / 私聊 {len(cfg.notify.private_targets)}｜最近轮询 {last_run_text}"
            ),
            tone=run_tone,
        )
        busy = self._action_task is not None and not self._action_task.done()
        states["check_now"] = ConfigWidgetState(text=self._action_text or "立即拉取一次所有订阅仓库", tone=self._action_tone, disabled=busy)
        states["send_summary"] = ConfigWidgetState(text=self._action_text or "把当前订阅的仓库、分支、作者与事件清单发送到已填写的通知目标", tone=self._action_tone, disabled=busy)
        states["send_test"] = ConfigWidgetState(text=self._action_text or "先保存通知目标，再点击立即发送测试通知；结果显示在此处", tone=self._action_tone, disabled=busy or not cfg.advanced.test_command_enabled)
        states["refresh_branches"] = ConfigWidgetState(text=self._action_text or "保存仓库后读取分支，完成后重新打开配置页即可勾选；也可直接填写分支名", tone=self._action_tone, disabled=busy)
        return states

    async def on_config_widget_action(self, action_id: str) -> None:
        if action_id not in {"check_now", "send_summary", "send_test", "refresh_branches"}:
            raise ValueError(f"未知的配置页操作: {action_id}")
        if self._action_task is not None and not self._action_task.done():
            raise ValueError("操作进行中")
        if self._engine is None or self._notifier is None:
            raise ValueError("插件尚未启动")
        if action_id == "send_test" and not self.config.advanced.test_command_enabled:
            raise ValueError("测试消息已关闭")
        self._action_text, self._action_tone = "操作进行中，请稍后刷新状态", "neutral"
        self._action_task = asyncio.create_task(self._run_widget_action(action_id))

    async def _run_widget_action(self, action_id: str) -> None:
        try:
            if action_id == "refresh_branches":
                cfg = self.config
                client = GitHubClient(token=cfg.github.token, api_base_url=cfg.github.api_base_url, proxy=cfg.github.proxy, timeout=cfg.github.request_timeout_seconds, verify_ssl=cfg.github.verify_ssl)
                try:
                    for repo in cfg.subscriptions.repos:
                        if repo.repo and repo.enabled:
                            self._branch_options[repo.repo] = await client.get_branches(repo.repo)
                finally:
                    await client.close()
                self._action_text, self._action_tone = "分支已读取，重新打开配置页勾选当前仓库对应的 repo :: branch 选项；新增仓库请先保存再刷新", "success"
            elif action_id == "check_now":
                stats = await self._engine.poll_all(force=True)
                self._action_text = self._summarize_stats(stats)
                self._action_tone = "warning" if stats.get("errors") else "success"
            elif action_id == "send_summary":
                stats = await self._notifier.send_text(self._subscription_summary(self.config), self.config)
                self._action_text, self._action_tone = self._describe_delivery("订阅信息", stats)
            else:
                stats = await self._notifier.send_text("🔔 GitHub 订阅测试消息\n如果你收到这条消息，说明通知目标配置正确。", self.config)
                self._action_text, self._action_tone = self._describe_delivery("测试消息", stats)
        except Exception as exc:
            self._action_text, self._action_tone = f"操作失败：{type(exc).__name__}", "error"
            logger.exception("配置页操作失败")

    # ------------------------------------------------------------------ 命令
    @staticmethod
    def _identity(platform: str, user_id: str) -> str:
        platform = (platform or "qq").strip().lower()
        return f"{platform}:{user_id.strip()}"

    def _authorize(self, platform: str, user_id: str) -> Tuple[bool, str]:
        """校验命令调用者权限：仅管理员与本机操作员。"""

        cfg = self.config
        if self._identity(platform, user_id) in cfg.plugin.admin_users:
            return True, ""
        if platform == "qq" and user_id and user_id in cfg.plugin.admin_users:
            return True, ""
        return False, "没有权限执行该命令；请让管理员把你的账号加入管理员列表。"

    @Command(
        "githubsub",
        description="GitHub 仓库订阅通知管理命令",
        pattern=COMMAND_PATTERN,
    )
    async def handle_command(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        platform: str = "qq",
        user_id: str = "",
        is_local_operator: bool = False,
        matched_groups: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, int]:
        del kwargs
        action = (matched_groups or {}).get("action") or ""
        if not action:
            match = re.match(COMMAND_PATTERN, str(text or "").strip())
            if match:
                action = match.group("action") or ""
        action = action or "status"
        if not is_local_operator and action != "help":
            allowed, reason = self._authorize(platform, user_id)
            if not allowed:
                await self._reply(stream_id, reason)
                return True, reason, 1

        cfg = self.config
        try:
            if action == "help":
                reply = HELP_TEXT
            elif action == "status":
                reply = await self._status_text(cfg)
            elif action == "list":
                reply = self._list_text(cfg)
            elif action == "rate":
                reply = await self._rate_text(cfg)
            elif action == "check":
                if self._engine is None:
                    reply = "引擎尚未启动。"
                else:
                    stats = await self._engine.poll_all(force=True)
                    reply = self._summarize_stats(stats)
            elif action == "test":
                if self._notifier is None:
                    reply = "通知器尚未初始化。"
                elif not cfg.advanced.test_command_enabled:
                    reply = "管理员已在配置中关闭测试命令。"
                else:
                    stats = await self._notifier.send_text(
                        "🔔 GitHub 订阅测试消息\n如果你收到这条消息，说明通知目标配置正确。", cfg
                    )
                    reply = f"测试消息已发送：成功 {stats['sent']}，失败 {stats['failed']}，目标 {stats['targets']}。"
            elif action == "pause":
                if self._engine is not None:
                    self._engine.set_paused(True)
                reply = "已暂停后台轮询（/githubsub resume 恢复）。"
            elif action == "resume":
                if self._engine is not None:
                    self._engine.set_paused(False)
                reply = "已恢复后台轮询。"
            else:
                reply = HELP_TEXT
        except Exception as exc:  # noqa: BLE001
            logger.error("执行 githubsub 命令失败: %s", exc, exc_info=True)
            reply = f"执行失败：{type(exc).__name__}: {exc}"

        await self._reply(stream_id, reply)
        return True, reply, 1

    # ------------------------------------------------------------------ LLM 工具
    @Tool(
        "github_sub_status",
        description="查询 GitHub 仓库订阅通知插件的运行状态与订阅列表",
        brief_description="查询 GitHub 订阅插件状态",
    )
    async def tool_status(self, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        cfg = self.config
        return {"content": await self._status_text(cfg)}

    # ------------------------------------------------------------------ 文本组装
    async def _reply(self, stream_id: str, text: str) -> None:
        if not stream_id or not text:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("回复命令失败: %s", exc)

    async def _status_text(self, cfg: GithubSubConfig) -> str:
        repos = [r for r in cfg.subscriptions.repos if r.enabled and r.repo]
        engine_paused = self._engine.paused if self._engine else False
        if not cfg.plugin.enabled:
            state_label = "已关闭"
        elif engine_paused:
            state_label = "已暂停"
        else:
            state_label = "运行中"
        lines = [
            "📡 GitHub 订阅状态",
            f"状态：{state_label}",
            f"订阅仓库：{len(repos)} 个",
            f"轮询间隔：{cfg.polling.interval_seconds} 秒",
            f"通知目标：群 {len(cfg.notify.group_targets)} 个 / 私聊 {len(cfg.notify.private_targets)} 个",
        ]
        if self._engine is not None and self._engine.last_run:
            lines.append(
                "最近轮询：" + datetime.fromtimestamp(self._engine.last_run).strftime("%Y-%m-%d %H:%M:%S")
            )
            stats = self._engine.last_stats
            if stats:
                lines.append(
                    f"上轮结果：事件 {stats.get('events', 0)} 条，发送成功 {stats.get('sent', 0)}、失败 {stats.get('failed', 0)}"
                )
                if stats.get("errors"):
                    lines.append("错误：" + "；".join(stats["errors"][:3]))
        else:
            lines.append("最近轮询：尚未轮询")
        if self._engine is not None and self._engine.rate_limit_until > time.time():
            lines.append("⚠️ 已触发 GitHub 速率限制，暂停中")
        return "\n".join(lines)

    @staticmethod
    def _describe_delivery(label: str, stats: Dict[str, int]) -> Tuple[str, str]:
        if not stats["targets"]:
            return "未发送：没有填写通知群聊或通知私聊，请先填写并保存，再点击按钮。", "warning"
        text = (
            f"{label}发送结果：目标 {stats['targets']}，成功 {stats['sent']}，失败 {stats['failed']}，"
            f"跳过 {stats.get('skipped', 0)}，结果未知 {stats.get('unknown', 0)}"
        )
        tone = "success" if stats["sent"] == stats["targets"] else "warning"
        return text, tone

    def _subscription_summary(self, cfg: GithubSubConfig) -> str:
        """生成可直接发送到群聊/私聊的订阅清单。"""
        repos = [r for r in cfg.subscriptions.repos if r.enabled and r.repo]
        lines = ["📚 当前 GitHub 仓库订阅信息"]
        if not repos:
            lines.append("尚未配置订阅仓库，请先在插件配置页的“订阅仓库”中添加。")
        for idx, repo in enumerate(repos[:20], 1):
            branches = "、".join(
                branch.split(" :: ", 1)[-1] for branch in repo.branches
            ) or "默认分支"
            authors = "、".join(repo.authors) or "不限作者"
            events = []
            if repo.watch_commits:
                events.append("提交")
            if repo.watch_releases:
                events.append("Release")
            if repo.watch_issues:
                events.append("Issue")
            if repo.watch_prs:
                events.append("PR")
            if repo.watch_tags:
                events.append("Tag")
            block = [f"{idx}. {repo.repo}", f"   分支：{branches}", f"   作者：{authors}",
                     f"   事件：{'/'.join(events) or '无'}"]
            if repo.note:
                block.append(f"   备注：{repo.note}")
            lines.append("\n".join(block))
        if len(repos) > 20:
            lines.append(f"……另有 {len(repos) - 20} 个仓库未展开")
        lines.append(f"通知目标：群聊 {len(cfg.notify.group_targets)} 个 / 私聊 {len(cfg.notify.private_targets)} 个")
        lines.append(f"轮询间隔：{cfg.polling.interval_seconds} 秒")
        return "\n".join(lines)

    def _list_text(self, cfg: GithubSubConfig) -> str:
        repos = [r for r in cfg.subscriptions.repos if r.enabled and r.repo]
        if not repos:
            return "尚未配置任何订阅仓库。请在插件配置页的“订阅仓库”中添加。"
        lines = ["📚 订阅仓库列表："]
        for idx, repo in enumerate(repos, 1):
            branches = "、".join(
                branch.split(" :: ", 1)[-1] for branch in repo.branches
            ) if repo.branches else "默认分支"
            authors = "、".join(repo.authors) if repo.authors else "不限作者"
            events = []
            if repo.watch_commits:
                events.append("提交")
            if repo.watch_releases:
                events.append("Release")
            if repo.watch_issues:
                events.append("Issue")
            if repo.watch_prs:
                events.append("PR")
            if repo.watch_tags:
                events.append("Tag")
            lines.append(
                f"{idx}. {repo.repo}｜分支：{branches}｜作者：{authors}｜事件：{'/'.join(events) or '无'}"
            )
        return "\n".join(lines)

    async def _rate_text(self, cfg: GithubSubConfig) -> str:
        client = GitHubClient(
            token=cfg.github.token,
            api_base_url=cfg.github.api_base_url,
            proxy=cfg.github.proxy,
            timeout=float(cfg.github.request_timeout_seconds),
            verify_ssl=bool(cfg.github.verify_ssl),
        )
        try:
            rl = await client.get_rate_limit()
        except GitHubError as exc:
            return f"查询 GitHub 速率限制失败：{exc}"
        finally:
            await client.close()
        return (
            "📊 GitHub 速率限制\n"
            f"额度：{rl.remaining}/{rl.limit}（已用 {rl.used}）\n"
            f"重置时间：{rl.reset_time_str}"
        )

    @staticmethod
    def _summarize_stats(stats: Dict[str, Any]) -> str:
        lines = [
            "✅ 本轮检查完成",
            f"仓库：{stats.get('repos', 0)} 个",
            f"新事件：{stats.get('events', 0)} 条",
            f"发送成功：{stats.get('sent', 0)}，失败：{stats.get('failed', 0)}",
        ]
        if stats.get("errors"):
            lines.append("错误：" + "；".join(stats["errors"][:5]))
        return "\n".join(lines)


def create_plugin() -> GithubSubPlugin:
    return GithubSubPlugin()
