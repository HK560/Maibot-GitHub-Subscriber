"""发布清单与仓库元数据测试。"""

from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))

    def test_required_manifest_fields(self) -> None:
        for field in (
            "manifest_version",
            "id",
            "version",
            "name",
            "description",
            "author",
            "license",
            "urls",
            "host_application",
            "sdk",
            "capabilities",
            "i18n",
        ):
            self.assertIn(field, self.manifest)
        self.assertEqual(self.manifest["manifest_version"], 2)
        self.assertEqual(self.manifest["license"], "MIT")
        self.assertEqual(self.manifest["id"], "github.hk560.github-subscriber")
        self.assertEqual(self.manifest["name"], "MaiBot GitHub Subscriber")

    def test_repository_urls_and_author(self) -> None:
        self.assertEqual(self.manifest["author"]["name"], "HK560")
        self.assertEqual(self.manifest["author"]["url"], "https://github.com/HK560")
        for key in ("repository", "homepage", "documentation", "issues"):
            self.assertEqual(
                self.manifest["urls"][key].split("/issues")[0].split("/blob/")[0],
                "https://github.com/HK560/Maibot-GitHub-Subscriber",
            )
        self.assertTrue(self.manifest["urls"]["documentation"].endswith("/README.md"))

    def test_version_is_three_part_and_matches_pyproject(self) -> None:
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+$")
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), self.manifest["version"])

    def test_release_files_exist_and_manifest_is_valid_json(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "build_release_under_test", ROOT / "scripts" / "build_release.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in module.FILES:
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file(), name)
        self.assertNotIn("config.toml", module.FILES)
        self.assertNotIn("state.json", module.FILES)


if __name__ == "__main__":
    unittest.main()
