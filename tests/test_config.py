"""配置模型与 WebUI Schema 测试。"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List

import _helpers

config_mod = _helpers.load("config")
sdk_config = None
try:
    from maibot_sdk.config import build_plugin_default_config, generate_plugin_config_schema
except Exception:  # pragma: no cover
    build_plugin_default_config = None
    generate_plugin_config_schema = None


class ConfigModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.Cfg = config_mod.GithubSubConfig

    def test_defaults_build(self) -> None:
        cfg = self.Cfg()
        self.assertEqual(cfg.plugin.timezone, "Asia/Shanghai")
        self.assertEqual(cfg.polling.interval_seconds, 300)
        self.assertEqual(cfg.notify.platform, "qq")
        # 默认托管一个占位仓库条目，便于新装用户填写
        self.assertEqual(len(cfg.subscriptions.repos), 1)
        self.assertEqual(cfg.subscriptions.repos[0].repo, "")

    def test_repo_normalization(self) -> None:
        Repo = config_mod.RepoSubscription
        self.assertEqual(Repo(repo="https://github.com/Mai-with-u/MaiBot").repo, "Mai-with-u/MaiBot")
        self.assertEqual(Repo(repo="git@github.com:foo/bar.git").repo, "foo/bar")
        self.assertEqual(Repo(repo="a/b/").repo, "a/b")
        with self.assertRaises(ValueError):
            Repo(repo="not-a-repo")
        with self.assertRaises(ValueError):
            Repo(repo="a/b/c")

    def test_repo_lists_deduped(self) -> None:
        Repo = config_mod.RepoSubscription
        repo = Repo(repo="a/b", branches=["main", "main ", "dev"], authors=["x", "x"])
        self.assertEqual(repo.branches, ["main", "dev"])
        self.assertEqual(repo.authors, ["x"])

    def test_branch_options_cannot_cross_repositories(self) -> None:
        Repo = config_mod.RepoSubscription
        repo = Repo(repo="a/b", branches=["a/b :: dev", "main"])
        self.assertEqual(repo.branches, ["a/b :: dev", "main"])
        with self.assertRaises(ValueError):
            Repo(repo="a/b", branches=["c/d :: dev"])

    def test_admin_normalization(self) -> None:
        Section = config_mod.PluginSection
        section = Section(admin_users=["123456", "qq:123456", "telegram:42"])
        self.assertEqual(section.admin_users, ["qq:123456", "telegram:42"])
        with self.assertRaises(ValueError):
            Section(admin_users=["badformat"])

    def test_group_and_user_validation(self) -> None:
        Notify = config_mod.NotifySection
        n = Notify(group_targets=["123456789", "987654321"])
        self.assertEqual(n.group_targets, ["123456789", "987654321"])
        with self.assertRaises(ValueError):
            Notify(group_targets=["123"])
        users = Notify(private_targets=["12345678", "87654321"])
        self.assertEqual(users.private_targets, ["12345678", "87654321"])
        with self.assertRaises(ValueError):
            Notify(private_targets=["abc"])

    def test_whitelist_and_blacklist_fields_removed(self) -> None:
        Notify = config_mod.NotifySection
        self.assertNotIn("private_whitelist", Notify.model_fields)
        self.assertNotIn("private_blacklist", Notify.model_fields)
        self.assertNotIn("group_blacklist", Notify.model_fields)
        # 旧配置里的遗留字段被忽略，不影响正常加载
        n = Notify(group_targets=["123456789"], private_whitelist=["11111111"], group_blacklist=["987654321"])
        self.assertEqual(n.group_targets, ["123456789"])
        self.assertFalse(hasattr(n, "private_whitelist"))

    def test_timezone_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.Cfg(plugin={"timezone": "Mars/Olympus"})

    def test_subscriptions_drop_empty_and_duplicate(self) -> None:
        Repo = config_mod.RepoSubscription
        Section = config_mod.SubscriptionsSection
        section = Section(repos=[
            Repo(repo=""),
            Repo(repo="a/b"),
            Repo(repo="A/B"),
            Repo(repo="c/d"),
        ])
        self.assertEqual([r.repo for r in section.repos], ["a/b", "c/d"])

    @unittest.skipIf(build_plugin_default_config is None, "maibot_sdk 不可用")
    def test_default_config_and_schema(self) -> None:
        defaults = build_plugin_default_config(self.Cfg)
        self.assertEqual(defaults["plugin"]["config_version"], "1.1.0")
        schema = generate_plugin_config_schema(self.Cfg)
        sections = schema["sections"]
        for name in ["plugin", "github", "subscriptions", "notify", "polling", "filters", "template", "advanced"]:
            self.assertIn(name, sections)
        repos_field = sections["subscriptions"]["fields"]["repos"]
        self.assertEqual(repos_field["type"], "array")
        self.assertEqual(repos_field["item_type"], "object")
        for field in ["repo", "branches", "authors", "watch_commits", "watch_releases",
                      "watch_issues", "watch_prs", "watch_tags"]:
            self.assertIn(field, repos_field["item_fields"])
        self.assertEqual(repos_field["item_fields"]["issue_state"]["choices"], ["all", "open"])

    def test_multiple_notification_targets_use_supported_list_widget(self):
        schema = generate_plugin_config_schema(self.Cfg)
        fields = schema["sections"]["notify"]["fields"]
        for key in ("group_targets", "private_targets"):
            with self.subTest(key=key):
                self.assertEqual(fields[key]["ui_type"], "list")
                self.assertEqual(fields[key]["type"], "array")
                self.assertEqual(fields[key]["item_type"], "string")
        cfg = self.Cfg(notify={"group_targets": ["123456789", "987654321"], "private_targets": ["12345678", "87654321"]})
        restored = self.Cfg.model_validate(cfg.model_dump())
        self.assertEqual(len(restored.notify.group_targets), 2)
        self.assertEqual(len(restored.notify.private_targets), 2)


if __name__ == "__main__":
    unittest.main()
