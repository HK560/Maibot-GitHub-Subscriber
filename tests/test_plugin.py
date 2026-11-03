"""插件命令鉴权与状态文本测试。"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List

import _helpers

config_mod = _helpers.load("config")
plugin_mod = _helpers.load("plugin")


class FakeSend:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        self.calls.append({"text": text, "stream_id": stream_id})
        return True


class FakeCtx:
    def __init__(self) -> None:
        self.send = FakeSend()
        self.logger = _NoopLogger()


class _NoopLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass


def build_plugin(**cfg_kwargs: Any) -> Any:
    pl = plugin_mod.GithubSubPlugin()
    cfg = config_mod.GithubSubConfig(**cfg_kwargs)
    pl.set_plugin_config(cfg.model_dump())
    pl._set_context(FakeCtx())
    return pl


class PluginCommandTest(unittest.TestCase):
    def test_help_command(self) -> None:
        pl = build_plugin()
        ok, reply, intercept = asyncio.run(
            pl.handle_command(text="/githubsub help", stream_id="s1", platform="qq", user_id="1")
        )
        self.assertTrue(ok)
        self.assertIn("githubsub", reply)
        self.assertEqual(intercept, 1)
        self.assertEqual(len(pl.ctx.send.calls), 1)

    def test_non_admin_denied_for_readonly_command(self) -> None:
        pl = build_plugin()
        ok, reply, intercept = asyncio.run(
            pl.handle_command(text="/githubsub status", stream_id="s1", platform="qq", user_id="99999")
        )
        self.assertIn("没有权限", reply)
        self.assertEqual(intercept, 1)

    def test_admin_allowed_mutating(self) -> None:
        pl = build_plugin(plugin=dict(admin_users=["qq:123"]))
        ok, reply, intercept = asyncio.run(
            pl.handle_command(text="/githubsub pause", stream_id="s1", platform="qq", user_id="123")
        )
        self.assertIn("已暂停", reply)

    def test_admin_readonly_status(self) -> None:
        pl = build_plugin(plugin=dict(admin_users=["qq:55555"]))
        ok, reply, intercept = asyncio.run(
            pl.handle_command(text="/githubsub status", stream_id="s1", platform="qq", user_id="55555")
        )
        self.assertIn("GitHub 订阅状态", reply)

    def test_list_command(self) -> None:
        pl = build_plugin(
            plugin=dict(admin_users=["qq:1"]),
            subscriptions=dict(repos=[
                dict(repo="Mai-with-u/MaiBot", branches=["master"], watch_tags=True),
            ]),
        )
        ok, reply, _ = asyncio.run(
            pl.handle_command(text="/githubsub list", stream_id="s1", platform="qq", user_id="1")
        )
        self.assertIn("Mai-with-u/MaiBot", reply)
        self.assertIn("master", reply)
        self.assertIn("Tag", reply)

    def test_status_text_reports_repo_count(self) -> None:
        pl = build_plugin(subscriptions=dict(repos=[dict(repo="a/b"), dict(repo="c/d")]))
        text = asyncio.run(pl._status_text(pl.config))
        self.assertIn("订阅仓库：2 个", text)

    def test_local_operator_bypasses_auth(self) -> None:
        pl = build_plugin()
        ok, reply, _ = asyncio.run(
            pl.handle_command(text="/githubsub resume", stream_id="", platform="local", user_id="", is_local_operator=True)
        )
        self.assertTrue(ok)


class WidgetTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_check_returns_immediately_and_tracks_task(self):
        from unittest.mock import AsyncMock
        pl = build_plugin()
        gate = asyncio.Event()
        class SlowEngine:
            async def poll_all(self, **kwargs):
                await gate.wait()
                return {"repos":1, "events":0}
        pl._engine, pl._notifier = SlowEngine(), object()
        await asyncio.wait_for(pl.on_config_widget_action("check_now"), timeout=.1)
        self.assertFalse(pl._action_task.done())
        with self.assertRaises(ValueError):
            await pl.on_config_widget_action("check_now")
        gate.set()
        await pl._action_task
        self.assertIn("检查完成", pl._action_text)

    async def test_immediate_test_button_dispatches_to_notifier(self):
        from unittest.mock import AsyncMock
        pl = build_plugin(notify={"group_targets": ["123456789", "987654321"]})
        pl._engine = object()
        pl._notifier = type("Notifier", (), {})()
        pl._notifier.send_text = AsyncMock(return_value={"targets":2, "sent":2, "failed":0})
        widget = next(w for w in pl.config_widgets if w.id == "send_test")
        self.assertEqual(widget.label, "立即发送测试通知")
        await pl.on_config_widget_action("send_test")
        await pl._action_task
        pl._notifier.send_text.assert_awaited_once()
        self.assertEqual(pl._notifier.send_text.await_args.args[1].notify.group_targets, ["123456789", "987654321"])
        self.assertIn("成功 2", pl._action_text)

    async def test_test_button_reports_empty_targets(self):
        from unittest.mock import AsyncMock
        pl = build_plugin()
        pl._engine = object()
        pl._notifier = type("Notifier", (), {})()
        pl._notifier.send_text = AsyncMock(return_value={"targets":0, "sent":0, "failed":0})
        await pl.on_config_widget_action("send_test")
        await pl._action_task
        self.assertIn("没有填写通知群聊或通知私聊", pl._action_text)
        self.assertEqual(pl._action_tone, "warning")

    async def test_summary_button_sends_subscription_info(self):
        from unittest.mock import AsyncMock
        pl = build_plugin(
            notify={"group_targets": ["123456789"], "private_targets": ["12345678"]},
            subscriptions={"repos": [dict(
                repo="a/b", branches=["a/b :: dev"], authors=["alice"],
                watch_tags=True, note="核心仓库",
            )]},
        )
        pl._engine = object()
        pl._notifier = type("Notifier", (), {})()
        pl._notifier.send_text = AsyncMock(
            return_value={"targets": 2, "sent": 2, "failed": 0, "skipped": 0, "unknown": 0}
        )
        widget = next(w for w in pl.config_widgets if w.id == "send_summary")
        self.assertEqual(widget.label, "立即发送当前仓库订阅信息")
        await pl.on_config_widget_action("send_summary")
        await pl._action_task
        text = pl._notifier.send_text.await_args.args[0]
        for expected in ("a/b", "分支：dev", "作者：alice", "Tag", "核心仓库", "群聊 1 个"):
            self.assertIn(expected, text)
        self.assertIn("成功 2", pl._action_text)

    async def test_summary_handles_no_repositories(self):
        pl = build_plugin(subscriptions={"repos": []})
        text = pl._subscription_summary(pl.config)
        self.assertIn("尚未配置订阅仓库", text)
        self.assertIn("群聊 0 个", text)

    async def test_branch_schema_contains_multiselect(self):
        pl = build_plugin()
        pl._branch_options = {"a/b": ["main", "dev"]}
        schema = pl.get_webui_config_schema()
        field = schema["sections"]["subscriptions"]["fields"]["repos"]["item_fields"]["branches"]
        self.assertTrue(field["multiple"])
        self.assertEqual(field["choices"], ["a/b :: dev", "a/b :: main"])


if __name__ == "__main__":
    unittest.main()
