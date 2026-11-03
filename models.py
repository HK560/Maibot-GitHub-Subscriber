"""事件模型与通知文案格式化。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


KIND_META: Dict[str, Dict[str, str]] = {
    "commit": {"emoji": "📦", "label": "新提交"},
    "release": {"emoji": "🚀", "label": "新发布"},
    "issue": {"emoji": "🐞", "label": "Issue"},
    "pr": {"emoji": "🔀", "label": "Pull Request"},
    "tag": {"emoji": "🏷️", "label": "新标签"},
}


@dataclass
class Event:
    """一条待推送的仓库事件。"""

    kind: str
    repo: str
    title: str
    url: str
    author: str = ""
    body: str = ""
    event_time: str = ""
    action: str = ""  # 例如 opened / closed / merged / published
    key: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def _truncate(text: str, limit: int) -> str:
    """按字符数截断，保留省略号。"""

    text = " ".join(str(text or "").split())
    if limit > 0 and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _format_time(raw: str, timezone_name: str) -> str:
    """将 ISO 时间字符串转换为指定时区的可读时间。"""

    if not raw:
        return ""
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        dt = dt.astimezone(ZoneInfo(timezone_name))
    except Exception:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def format_event(event: Event, *, config: Any) -> str:
    """把一条事件格式化为纯文本通知。"""

    template = getattr(config, "template", None)
    filters = getattr(config, "filters", None)
    tz = getattr(getattr(config, "plugin", None), "timezone", "Asia/Shanghai") or "Asia/Shanghai"
    max_len = int(getattr(filters, "max_text_length", 400) or 400)

    use_emoji = bool(getattr(template, "use_emoji", True))
    show_author = bool(getattr(template, "show_author", True))
    show_repo = bool(getattr(template, "show_repo_full_name", True))
    footer = str(getattr(template, "footer_text", "") or "").strip()

    meta = KIND_META.get(event.kind, {"emoji": "🔔", "label": event.kind})
    prefix = f"{meta['emoji']} " if use_emoji else ""
    repo_label = event.repo if show_repo else event.repo.split("/")[-1]

    lines: List[str] = []
    lines.append(f"{prefix}【{meta['label']}】{repo_label}")

    title = _truncate(event.title, max_len)
    if title:
        lines.append(title)

    detail_bits: List[str] = []
    if show_author and event.author:
        detail_bits.append(f"作者：{event.author}")
    if event.action:
        detail_bits.append(f"动作：{event.action}")
    if event.extra.get("branch"):
        detail_bits.append(f"分支：{event.extra['branch']}")
    when = _format_time(event.event_time, tz)
    if when:
        detail_bits.append(f"时间：{when}")
    if detail_bits:
        lines.append(" · ".join(detail_bits))

    # 正文展示按事件类型开关
    show_body = True
    if event.kind == "commit":
        show_body = bool(getattr(template, "show_commit_body", True))
    elif event.kind in {"issue", "pr"}:
        show_body = bool(getattr(template, "show_issue_body", True))
    if show_body and event.body:
        lines.append(_truncate(event.body, max_len))

    if event.url:
        lines.append(event.url)

    if footer:
        lines.append(footer)

    return "\n".join(line for line in lines if line).strip()


def format_events(events: List[Event], *, config: Any) -> str:
    """多条事件合并为一条文本消息。"""

    return "\n\n".join(format_event(event, config=config) for event in events)


def event_forward_node(event: Event, *, config: Any) -> Dict[str, Any]:
    """把事件转换为合并转发的消息节点。"""

    text = format_event(event, config=config)
    nickname = f"GitHub · {event.repo.split('/')[-1]}"
    return {
        "nickname": nickname,
        "user_nickname": nickname,
        "segments": [{"type": "text", "content": text}],
    }
