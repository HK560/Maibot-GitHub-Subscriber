"""订阅进度的本地持久化。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_VERSION = 1
_MAX_SEEN_COMMITS = 100
_MAX_SEEN_RELEASES = 500
_MAX_SEEN_TAGS = 500
_MAX_SEEN_NUMBERS = 500


def _parse_iso_ts(iso_str: str) -> float:
    if not iso_str:
        return 0.0
    text = str(iso_str).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _empty_repo_state() -> Dict[str, Any]:
    return {
        "initialized": False,
        "default_branch": "",
        "branches": {},  # branch -> [sha, ...] 最新在前
        "releases": [],  # release id 字符串，最新在前
        "tags": [],  # tag 名称，最新在前
        "issues": {},  # number(str) -> {"state": str, "updated_at": str}
        "prs": {},  # number(str) -> {"state": str, "merged": bool, "updated_at": str}
        "last_poll": 0.0,
        "last_success": 0.0,
        "last_error": "",
        "last_warning": "",
        "error_until": 0.0,
        "outbox": [],  # 待投递事件与投递回执
        "history": [],  # 终态回执历史（保留最近 100 条供诊断）
        # 已见过但可能被保留期清理掉的编号，用于避免旧条目被重新当作新开
        "seen_issues": [],
        "seen_prs": [],
        # 上一轮已知条目的最新 updated_at（ISO 字符串），用于识别翻页带入的旧条目
        "issue_watermark": "",
        "pr_watermark": "",
    }


class StateStore:
    """维护并持久化每个仓库的订阅进度。"""

    #: 供引擎复用的 ISO 时间解析入口
    parse_iso_ts = staticmethod(_parse_iso_ts)

    def __init__(self, path: Optional[Path], enabled: bool = True) -> None:
        self.path = Path(path) if path else None
        self.enabled = enabled
        self._data: Dict[str, Any] = {"version": STATE_VERSION, "meta": {}, "repos": {}}
        self.load()

    # ------------------------------------------------------------------ 读写
    def load(self) -> None:
        """从磁盘加载状态；文件损坏时回退为空状态并记录告警。"""

        if not self.enabled or self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("读取状态文件失败，将使用空状态: %s", exc)
            return
        if isinstance(raw, dict) and isinstance(raw.get("repos"), dict):
            self._data = {
                "version": raw.get("version", STATE_VERSION),
                "meta": raw.get("meta", {}) if isinstance(raw.get("meta"), dict) else {},
                "repos": raw["repos"],
            }

    def save(self) -> None:
        """原子写入状态文件；磁盘 I/O 异常时向外传播，防止状态未落盘就发生发送。"""

        if not self.enabled or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    # ------------------------------------------------------------------ 访问
    def repo_state(self, repo: str) -> Dict[str, Any]:
        """获取（并在需要时创建）某仓库的状态字典。"""

        repos = self._data.setdefault("repos", {})
        state = repos.get(repo)
        if not isinstance(state, dict):
            state = _empty_repo_state()
            repos[repo] = state
            return state
        # 补齐可能缺失的键（版本升级场景）
        base = _empty_repo_state()
        for key, value in base.items():
            if key not in state:
                state[key] = list(value) if isinstance(value, list) else (dict(value) if isinstance(value, dict) else value)
        return state

    def mark_poll(self, repo: str, *, success: bool, error: str = "") -> None:
        state = self.repo_state(repo)
        now = time.time()
        state["last_poll"] = now
        if success:
            state["last_success"] = now
            state["last_error"] = ""
            state["error_until"] = 0.0
        else:
            state["last_error"] = error[:500]

    # ------------------------------------------------------------------ 出件箱
    def add_to_outbox(
        self,
        repo: str,
        events: List[Any],
        destinations: List[Any],
        platform: str = "qq",
    ) -> Dict[str, Any]:
        """将一组待发送事件及目标回执记录到仓库出件箱中。"""
        from dataclasses import asdict, is_dataclass

        state = self.repo_state(repo)
        event_payloads = [
            asdict(e) if is_dataclass(e) else dict(e) for e in events
        ]
        receipts: Dict[str, Dict[str, Any]] = {}
        for kind, target_id in destinations:
            target_key = f"{kind}:{target_id}"
            receipts[target_key] = {
                "kind": str(kind),
                "target_id": str(target_id),
                "status": "pending",
                "retries": 0,
                "last_attempt": 0.0,
                "error": "",
            }

        item = {
            "id": f"{repo}:{int(time.time() * 1000)}:{len(state.get('outbox', []))}",
            "created_at": time.time(),
            "platform": str(platform or "qq"),
            "events": event_payloads,
            "receipts": receipts,
        }
        state.setdefault("outbox", []).append(item)
        return item

    def prune_outbox(self, repo: str) -> None:
        """移除出件箱中所有目标均已处理完毕（终态）的条目，并归档至最近 100 条历史。"""
        state = self.repo_state(repo)
        outbox = state.get("outbox", [])
        if not outbox:
            return
        remaining = []
        terminal_statuses = {"delivered", "terminal", "unknown", "skipped"}
        for item in outbox:
            receipts = item.get("receipts", {})
            if not receipts:
                continue
            if all(r.get("status") in terminal_statuses for r in receipts.values()):
                history_entry = {
                    "id": item.get("id"),
                    "created_at": item.get("created_at"),
                    "resolved_at": time.time(),
                    "platform": item.get("platform", "qq"),
                    "event_keys": [
                        e.get("key") or e.get("title")
                        for e in item.get("events", [])
                        if isinstance(e, dict)
                    ],
                    "receipts": receipts,
                }
                state.setdefault("history", []).append(history_entry)
            else:
                remaining.append(item)
        state["outbox"] = remaining
        if "history" in state and len(state["history"]) > 100:
            state["history"] = state["history"][-100:]

    # ------------------------------------------------------------------ 记录
    @staticmethod
    def remember_numbers(state: Dict[str, Any], key: str, numbers: List[str]) -> None:
        """记录已见过的 Issue/PR 编号（有界），清理后仍可识别旧条目。"""

        merged = list(dict.fromkeys(list(numbers) + list(state.get(key, []))))[:_MAX_SEEN_NUMBERS]
        state[key] = merged

    def remember_commits(self, state: Dict[str, Any], branch: str, shas: List[str]) -> None:
        existing = state.setdefault("branches", {}).get(branch, [])
        merged = list(dict.fromkeys(list(shas) + list(existing)))[:_MAX_SEEN_COMMITS]
        state["branches"][branch] = merged

    def known_commits(self, state: Dict[str, Any], branch: str) -> List[str]:
        return list(state.setdefault("branches", {}).get(branch, []))

    def remember_releases(self, state: Dict[str, Any], ids: List[str]) -> None:
        merged = list(dict.fromkeys(list(ids) + list(state.get("releases", []))))[:_MAX_SEEN_RELEASES]
        state["releases"] = merged

    def remember_tags(self, state: Dict[str, Any], names: List[str]) -> None:
        merged = list(dict.fromkeys(list(names) + list(state.get("tags", []))))[:_MAX_SEEN_TAGS]
        state["tags"] = merged

    # ------------------------------------------------------------------ 清理
    def cleanup(self, retention_days: int) -> None:
        """删除超过保留期的仓库状态条目字段（保留订阅本身）。"""

        cutoff = time.time() - max(1, retention_days) * 86400
        for repo, state in list(self._data.get("repos", {}).items()):
            if not isinstance(state, dict):
                continue
            last = float(state.get("last_poll") or 0)
            if last and last < cutoff:
                self._data["repos"][repo] = _empty_repo_state()
            else:
                outbox = state.get("outbox", [])
                if outbox:
                    state["outbox"] = [
                        item for item in outbox
                        if float(item.get("created_at") or 0) >= cutoff
                    ]
                history = state.get("history", [])
                if history:
                    state["history"] = [
                        h for h in history
                        if float(h.get("resolved_at") or h.get("created_at") or 0) >= cutoff
                    ][-100:]
                for col_key in ("issues", "prs"):
                    col = state.get(col_key)
                    if isinstance(col, dict):
                        pruned = {}
                        for num, meta in col.items():
                            up_str = str(meta.get("updated_at") or "") if isinstance(meta, dict) else ""
                            up_ts = _parse_iso_ts(up_str)
                            if not up_ts or up_ts >= cutoff:
                                pruned[num] = meta
                        state[col_key] = pruned

    def snapshot(self) -> Dict[str, Any]:
        """返回状态副本，供状态页/命令展示。"""

        return json.loads(json.dumps(self._data, ensure_ascii=False))
