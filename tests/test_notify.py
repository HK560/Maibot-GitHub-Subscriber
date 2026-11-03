"""通知目标解析、手动目标投递与格式化测试。"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List

import _helpers

config_mod = _helpers.load("config")
notify_mod = _helpers.load("notify")
models_mod = _helpers.load("models")


class FakeSend:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.forward_calls: List[Dict[str, Any]] = []
        self.fail_streams: set[str] = set()

    async def text(self, text: str, stream_id: str, **kwargs: Any):
        self.calls.append({"text": text, "stream_id": stream_id})
        return {"sent": stream_id not in self.fail_streams, "message_id": "m1"}

    async def forward(self, messages: List[Dict[str, Any]], stream_id: str, **kwargs: Any):
        self.forward_calls.append({"messages": messages, "stream_id": stream_id})
        return {"sent": True, "message_id": "f1"}


class FakeChat:
    async def get_group_streams(self, platform: str = "qq"):
        return [
            {"group_id": "123456789", "session_id": "g-1", "chat_type": "group", "account_id": "1"},
        ]

    async def get_private_streams(self, platform: str = "qq"):
        return [
            {"user_id": "11111111", "session_id": "p-1", "chat_type": "private", "account_id": "1"},
        ]

    async def open_session(self, **kwargs: Any):
        return {"session_id": "opened-" + str(kwargs.get("group_id") or kwargs.get("user_id"))}


class FakeCtx:
    def __init__(self) -> None:
        self.chat = FakeChat()
        self.send = FakeSend()


def make_config(**notify_kwargs: Any):
    defaults = dict(
        group_targets=["123456789"],
        private_targets=["11111111"],
    )
    defaults.update(notify_kwargs)
    return config_mod.GithubSubConfig(notify=defaults)


class NotifyTest(unittest.TestCase):
    def test_destinations_default(self) -> None:
        ctx = FakeCtx()
        cfg = make_config()
        notifier = notify_mod.Notifier(ctx, lambda: cfg)
        dests = notifier.destinations(cfg)
        self.assertIn(("group", "123456789"), dests)
        self.assertIn(("private", "11111111"), dests)

    def test_every_manually_configured_target_receives(self) -> None:
        ctx = FakeCtx()
        cfg = make_config(group_targets=["123456789", "987654321"], private_targets=["11111111"])
        notifier = notify_mod.Notifier(ctx, lambda: cfg)
        self.assertEqual(notifier.destinations(cfg), [
            ("group", "123456789"), ("group", "987654321"), ("private", "11111111"),
        ])

    def test_no_targets_means_no_destinations(self) -> None:
        ctx = FakeCtx()
        cfg = make_config(group_targets=[], private_targets=[])
        notifier = notify_mod.Notifier(ctx, lambda: cfg)
        self.assertEqual(notifier.destinations(cfg), [])

    def test_send_events_to_targets(self) -> None:
        ctx = FakeCtx()
        cfg = make_config()
        notifier = notify_mod.Notifier(ctx, lambda: cfg)
        events = [
            models_mod.Event(kind="commit", repo="a/b", title="fix", url="https://x", author="alice"),
            models_mod.Event(kind="release", repo="a/b", title="v1", url="https://y"),
        ]
        stats = asyncio.run(notifier.send_events(events, cfg))
        self.assertEqual(stats["targets"], 2)
        self.assertEqual(stats["sent"], 2)
        self.assertEqual(len(ctx.send.calls), 2)  # 群 + 私聊各一条
        self.assertIn("fix", ctx.send.calls[0]["text"])

    def test_send_skips_unresolvable_target(self) -> None:
        ctx = FakeCtx()
        ctx.send.fail_streams = {"g-1"}
        cfg = make_config()
        notifier = notify_mod.Notifier(ctx, lambda: cfg)
        events = [models_mod.Event(kind="commit", repo="a/b", title="fix", url="https://x")]
        stats = asyncio.run(notifier.send_events(events, cfg))
        self.assertEqual(stats["failed"], 1)

    def test_dry_run_does_not_send(self) -> None:
        ctx = FakeCtx()
        cfg = config_mod.GithubSubConfig(
            notify=dict(group_targets=["123456789"]),
            advanced=dict(dry_run=True),
        )
        notifier = notify_mod.Notifier(ctx, lambda: cfg)
        events = [models_mod.Event(kind="commit", repo="a/b", title="fix", url="https://x")]
        stats = asyncio.run(notifier.send_events(events, cfg))
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(ctx.send.calls, [])

    def test_forward_when_multiple_events(self) -> None:
        ctx = FakeCtx()
        cfg = config_mod.GithubSubConfig(
            notify=dict(group_targets=["123456789"], send_as_forward=True),
        )
        notifier = notify_mod.Notifier(ctx, lambda: cfg)
        events = [
            models_mod.Event(kind="commit", repo="a/b", title="c1", url="https://x"),
            models_mod.Event(kind="commit", repo="a/b", title="c2", url="https://y"),
        ]
        asyncio.run(notifier.send_events(events, cfg))
        self.assertEqual(len(ctx.send.forward_calls), 1)
        self.assertEqual(len(ctx.send.forward_calls[0]["messages"]), 2)


class DeliverySafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_hot_removal_during_resolution_blocks_send(self):
        ctx, cfg = FakeCtx(), make_config(private_targets=[])
        original = ctx.chat.get_group_streams
        async def changed(platform="qq"):
            cfg.notify.group_targets = []
            return await original(platform)
        ctx.chat.get_group_streams = changed
        notifier = notify_mod.Notifier(ctx, lambda: cfg)
        stats = await notifier.send_text("test", cfg)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(ctx.send.calls, [])

    async def test_ambiguous_accounts_fail_closed(self):
        ctx, cfg = FakeCtx(), make_config(private_targets=[])
        async def streams(platform="qq"):
            return [{"group_id":"123456789", "account_id":a, "session_id":"s"+a} for a in ["1","2"]]
        ctx.chat.get_group_streams = streams
        notifier = notify_mod.Notifier(ctx, lambda: cfg)
        self.assertIsNone(await notifier.resolve("qq", "group", "123456789"))
        cfg.notify.account_id = "2"
        self.assertEqual(await notifier.resolve("qq", "group", "123456789"), "s2")

    async def test_send_timeout_is_unknown(self):
        ctx, cfg = FakeCtx(), make_config(private_targets=[])
        async def timeout(*args, **kwargs):
            raise asyncio.TimeoutError()
        ctx.send.text = timeout
        stats = await notify_mod.Notifier(ctx, lambda: cfg).send_text("test", cfg)
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(stats["failed"], 0)

    async def test_disabled_plugin_prevents_test(self):
        ctx, cfg = FakeCtx(), make_config(private_targets=[])
        cfg.plugin.enabled = False
        stats = await notify_mod.Notifier(ctx, lambda: cfg).send_text("test", cfg)
        self.assertEqual(ctx.send.calls, [])
        self.assertEqual(stats["skipped"], 1)


class FormatTest(unittest.TestCase):
    def test_format_event_contains_fields(self) -> None:
        cfg = config_mod.GithubSubConfig()
        event = models_mod.Event(
            kind="release", repo="a/b", title="v1.2.3", author="alice",
            body="changes", url="https://github.com/a/b/releases/tag/v1.2.3",
            action="正式发布", event_time="2026-01-01T00:00:00Z",
        )
        text = models_mod.format_event(event, config=cfg)
        self.assertIn("v1.2.3", text)
        self.assertIn("alice", text)
        self.assertIn("https://github.com/a/b/releases/tag/v1.2.3", text)

    def test_truncate_body(self) -> None:
        cfg = config_mod.GithubSubConfig(filters=dict(max_text_length=80))
        event = models_mod.Event(kind="commit", repo="a/b", title="t", body="x" * 500, url="u")
        text = models_mod.format_event(event, config=cfg)
        self.assertLessEqual(len(text), 400)
        self.assertIn("…", text)


if __name__ == "__main__":
    unittest.main()
