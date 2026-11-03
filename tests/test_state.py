"""状态持久化测试。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import _helpers

state_mod = _helpers.load("state")


class StateStoreTest(unittest.TestCase):
    def test_save_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = state_mod.StateStore(path)
            state = store.repo_state("a/b")
            store.remember_commits(state, "main", ["sha1", "sha2"])
            store.remember_releases(state, ["1", "2"])
            state["initialized"] = True
            store.mark_poll("a/b", success=True)
            store.save()

            reloaded = state_mod.StateStore(path)
            restored = reloaded.repo_state("a/b")
            self.assertEqual(restored["branches"]["main"], ["sha1", "sha2"])
            self.assertEqual(restored["releases"], ["1", "2"])
            self.assertTrue(restored["initialized"])

    def test_remember_commits_dedup_and_cap(self) -> None:
        store = state_mod.StateStore(None)
        state = store.repo_state("a/b")
        shas = [f"s{i}" for i in range(150)]
        store.remember_commits(state, "main", shas)
        self.assertEqual(len(state["branches"]["main"]), 100)
        self.assertEqual(state["branches"]["main"][0], "s0")
        # 再次记录已有项不应重复
        store.remember_commits(state, "main", ["s0", "s1"])
        self.assertEqual(state["branches"]["main"].count("s0"), 1)

    def test_disabled_store_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = state_mod.StateStore(path, enabled=False)
            store.repo_state("a/b")["initialized"] = True
            store.save()
            self.assertFalse(path.exists())

    def test_corrupted_file_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            store = state_mod.StateStore(path)
            self.assertEqual(store.snapshot()["repos"], {})

    def test_cleanup_resets_stale(self) -> None:
        store = state_mod.StateStore(None)
        state = store.repo_state("a/b")
        state["initialized"] = True
        state["last_poll"] = time.time() - 100 * 86400
        store.cleanup(30)
        self.assertFalse(store.repo_state("a/b")["initialized"])


if __name__ == "__main__":
    unittest.main()
