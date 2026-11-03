"""事件模型、默认消息模板与通知文案格式化。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


KIND_META: Dict[str, Dict[str, str]] = {
    "commit": {"emoji": "📦", "label": "新提交"},
    "release": {"emoji": "🚀", "label": "新发布"},
    "issue": {"emoji": "🐞", "label": "Issue"},
    "pr": {"emoji": "🔀", "label": "Pull Request"},
    "tag": {"emoji": "🏷️", "label": "新标签"},
}

_WITH_BODY = (
    "{emoji}【{kind_label}】{repo}\n"
    "{title}\n\n"
    "{meta}\n\n"
    "{body}\n\n"
    "{url}\n\n"
    "{footer}"
)
_WITHOUT_BODY = (
    "{emoji}【{kind_label}】{repo}\n"
    "{title}\n\n"
    "{meta}\n\n"
    "{url}\n\n"
    "{footer}"
)

#: 各事件类型的内置默认模板；配置留空时使用。
DEFAULT_TEMPLATES: Dict[str, str] = {
    "commit": _WITH_BODY,
    "release": _WITH_BODY,
    # Issue/PR 默认只显示标题，需要正文时在模板中加入 {body}
    "issue": _WITHOUT_BODY,
    "pr": _WITHOUT_BODY,
    "tag": _WITHOUT_BODY,
    "generic": _WITH_BODY,
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


def _clean_message(text: str) -> str:
    """清理模板渲染结果：去行尾空白、合并多余空行、去掉首尾空白。"""

    lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class _TemplateValues(dict):
    """未知占位符原样保留，避免模板中的笔误被静默吞掉。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_template(template: str, values: Dict[str, str]) -> str:
    try:
        return template.format_map(_TemplateValues(values))
    except (KeyError, IndexError, ValueError):
        logger.warning("消息模板格式错误，已使用默认模板渲染: %r", template, exc_info=True)
        return ""


def _issue_number(event: Event) -> str:
    number = str(event.extra.get("number") or "").strip()
    if number:
        return number
    match = re.match(r"^#(\d+)\b", event.title or "")
    return match.group(1) if match else ""


def _event_values(event: Event, config: Any) -> Dict[str, str]:
    template = getattr(config, "template", None)
    filters = getattr(config, "filters", None)
    tz = getattr(getattr(config, "plugin", None), "timezone", "Asia/Shanghai") or "Asia/Shanghai"
    max_len = int(getattr(filters, "max_text_length", 400) or 400)

    meta = KIND_META.get(event.kind, {"emoji": "🔔", "label": event.kind})
    emoji = meta["emoji"]
    kind_label = meta["label"]
    title = _truncate(event.title, max_len)
    body = _truncate(event.body, max_len)
    when = _format_time(event.event_time, tz)
    branch = str(event.extra.get("branch") or "")
    author = str(event.author or "")
    action = str(event.action or "")
    url = str(event.url or "")
    key = str(event.key or "")
    number = _issue_number(event)
    tag = str(event.extra.get("tag") or "")
    if not tag and event.kind == "tag":
        tag = event.title
    sha = key if event.kind == "commit" else str(event.extra.get("sha") or "")
    repo = str(event.repo or "")
    repo_owner, _, repo_name = repo.partition("/")
    if not repo_name:
        repo_name = repo_owner
    footer = str(getattr(template, "footer_text", "") or "").strip()

    meta_parts = []
    if author:
        meta_parts.append(f"作者：{author}")
    if action:
        meta_parts.append(f"动作：{action}")
    if branch:
        meta_parts.append(f"分支：{branch}")
    if when:
        meta_parts.append(f"时间：{when}")

    return {
        "emoji": emoji,
        "header": f"{emoji}【{kind_label}】{repo}",
        "kind": event.kind,
        "kind_label": kind_label,
        "repo": repo,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "title": title,
        "body": body,
        "author": author,
        "action": action,
        "branch": branch,
        "time": when,
        "meta": " · ".join(meta_parts),
        "url": url,
        "key": key,
        "number": number,
        "tag": tag,
        "sha": sha,
        "footer": footer,
    }


def format_event(event: Event, *, config: Any) -> str:
    """使用事件类型对应的模板，把一条事件格式化为纯文本通知。"""

    template = getattr(config, "template", None)
    field_name = f"{event.kind}_template"
    template_text = str(getattr(template, field_name, "") or "").strip()
    if not template_text:
        template_text = DEFAULT_TEMPLATES.get(event.kind, DEFAULT_TEMPLATES["generic"])

    values = _event_values(event, config)
    text = _clean_message(_render_template(template_text, values))
    if not text:
        fallback = DEFAULT_TEMPLATES.get(event.kind, DEFAULT_TEMPLATES["generic"])
        if fallback != template_text:
            text = _clean_message(_render_template(fallback, values))
    return text or event.title or event.repo


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
