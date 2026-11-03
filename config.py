"""插件配置模型与 WebUI Schema 声明。

所有字段都放在命名配置节内：SDK 会把根级标量字段归入 WebUI 的 general
虚拟节，而前端按 ``config[节名][字段名]`` 取值，根级字段在表单里取不到值。
"""

from __future__ import annotations

import re
from typing import List, Literal, Tuple

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GROUP_PATTERN = re.compile(r"^\d{5,12}$")
_USER_PATTERN = re.compile(r"^\d{4,15}$")


def _normalize_repo(value: str) -> str:
    """把各种 GitHub 仓库写法归一化为 ``owner/repo``。"""

    text = str(value or "").strip()
    if not text:
        raise ValueError("仓库不能为空，请填写 owner/repo，例如 Mai-with-u/MaiBot")
    text = re.sub(r"^https?://github\.com/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^git@github\.com:", "", text, flags=re.IGNORECASE)
    text = text.strip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if text.count("/") != 1:
        raise ValueError(f"仓库格式应为 owner/repo，当前为：{value}")
    owner, name = text.split("/", 1)
    if not owner or not name or not _REPO_PATTERN.match(text):
        raise ValueError(f"仓库格式不合法：{value}，示例：Mai-with-u/MaiBot")
    return f"{owner}/{name}"


def _dedupe(items: List[str]) -> List[str]:
    seen: List[str] = []
    for item in items:
        item = str(item).strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def _clean_group_targets(values: List[str]) -> List[str]:
    result: List[str] = []
    for raw in values:
        group = str(raw).strip()
        if not group:
            continue
        if not _GROUP_PATTERN.match(group):
            raise ValueError(f"QQ 群号应为 5～12 位数字：{raw}")
        if group not in result:
            result.append(group)
    return result


def _clean_private_targets(values: List[str]) -> List[str]:
    result: List[str] = []
    for raw in values:
        user = str(raw).strip()
        if not user:
            continue
        if not _USER_PATTERN.match(user):
            raise ValueError(f"QQ 号应为 4～15 位数字：{raw}")
        if user not in result:
            result.append(user)
    return result


class PluginSection(PluginConfigBase):
    """插件总开关、管理员与配置版本。"""

    __ui_label__ = "基本设置"
    __ui_icon__ = "settings"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="启用 GitHub 订阅通知后台轮询",
        json_schema_extra={
            "label": "启用插件",
            "hint": "关闭后停止后台轮询，但不会删除已保存的订阅和状态；重新开启后从上次进度继续。",
        },
    )
    config_version: str = Field(
        default="1.2.0",
        description="插件自动维护的配置格式版本",
        json_schema_extra={
            "label": "配置格式版本（无需修改）",
            "hint": "由插件自动维护，用于识别配置格式，保持原值即可。",
            "disabled": True,
        },
    )
    admin_users: List[str] = Field(
        default_factory=list,
        description="可使用管理命令的账号，QQ 号或 平台:用户号",
        json_schema_extra={
            "label": "管理员账号",
            "hint": "QQ 管理员直接填 QQ 号（保存时自动补全 qq:），其他平台填写 平台:用户号。每项一个账号。留空时仅本机操作员可用管理命令。",
            "placeholder": "例如：12345678",
            "x-widget": "list",
        },
    )
    timezone: str = Field(
        default="Asia/Shanghai",
        description="消息中展示时间所用时区",
        json_schema_extra={
            "label": "展示时区",
            "hint": "IANA 时区名，例如 Asia/Shanghai、UTC。影响通知落款时间与状态页时间显示。",
            "placeholder": "Asia/Shanghai",
        },
    )

    @field_validator("admin_users")
    @classmethod
    def _normalize_admins(cls, values: List[str]) -> List[str]:
        normalized: List[str] = []
        for value in values:
            value = str(value).strip()
            if not value:
                continue
            if value.isascii() and value.isdecimal():
                value = f"qq:{value}"
            if ":" not in value or any(not part.strip() for part in value.split(":", 1)):
                raise ValueError("管理员请填写 QQ 号或 平台:用户号，例如 123456 或 qq:123456")
            platform, user_id = value.split(":", 1)
            normalized.append(f"{platform.strip().lower()}:{user_id.strip()}")
        return list(dict.fromkeys(normalized))


class GitHubSection(PluginConfigBase):
    """GitHub API 连接与认证设置。"""

    __ui_label__ = "GitHub 连接"
    __ui_icon__ = "github"
    __ui_order__ = 1

    token: str = Field(
        default="",
        description="GitHub Personal Access Token，用于提高速率限制并访问私有仓库",
        json_schema_extra={
            "label": "GitHub Token",
            "hint": "公开仓库可留空（通常每小时 60 次请求）。推荐 fine-grained Token：选择目标仓库，仅授予 Contents、Issues、Pull requests 的读取权限。classic 的 repo 权限包含写权限，请按需谨慎使用。",
            "input_type": "password",
            "placeholder": "ghp_xxxxxxxx 或 github_pat_xxxxxxxx",
            "x-widget": "password",
        },
    )
    api_base_url: str = Field(
        default="https://api.github.com",
        description="GitHub API 基础地址，兼容 GitHub Enterprise",
        json_schema_extra={
            "label": "API 地址",
            "hint": "默认 https://api.github.com。使用 GitHub Enterprise 时改为 https://<域名>/api/v3。",
        },
    )
    proxy: str = Field(
        default="",
        description="访问 GitHub 时使用的代理地址，可选",
        json_schema_extra={
            "label": "代理地址",
            "hint": "可选，例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:7891。留空直连。",
            "placeholder": "http://127.0.0.1:7890",
        },
    )
    verify_ssl: bool = Field(
        default=True,
        description="是否校验 HTTPS 证书",
        json_schema_extra={
            "label": "校验 SSL 证书",
            "hint": "保持开启；仅在明确知道企业自签名证书风险时才关闭。",
        },
    )
    per_page: int = Field(
        default=15,
        ge=1,
        le=100,
        description="单次拉取事件的最大条数",
        json_schema_extra={
            "label": "单次拉取条数",
            "hint": "每页拉取条数，默认 15。GitHub 每次请求消耗一次额度，页大小主要影响响应体积；高活跃仓库可调至 100。",
            "x-widget": "number",
        },
    )
    request_timeout_seconds: int = Field(
        default=15,
        ge=5,
        le=120,
        description="单次 GitHub 请求超时（秒）",
        json_schema_extra={
            "label": "请求超时（秒）",
            "hint": "默认 15 秒。网络较差可适当调高。",
            "x-widget": "number",
        },
    )


    @field_validator("api_base_url")
    @classmethod
    def _api_url(cls, value: str) -> str:
        from urllib.parse import urlsplit
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("API 地址必须是无凭据、无查询参数的 HTTPS 地址")
        return value.rstrip("/")


class RepoSubscription(PluginConfigBase):
    """单个仓库的订阅选项。"""

    repo: str = Field(
        default="",
        description="GitHub 仓库，格式 owner/repo",
        json_schema_extra={
            "label": "仓库",
            "placeholder": "Mai-with-u/MaiBot",
            "hint": "填写 owner/repo，也可直接粘贴 https://github.com/owner/repo 链接，保存时自动归一化。",
        },
    )
    enabled: bool = Field(
        default=True,
        description="是否订阅该仓库",
        json_schema_extra={"label": "启用该仓库"},
    )
    branches: List[str] = Field(
        default_factory=list,
        description="要订阅提交的分支列表，留空表示仅默认分支",
        json_schema_extra={
            "label": "订阅分支（提交）",
            "hint": "勾选/填写要监控提交的分支名，可添加多个（如 main、dev、release/* 不支持通配）。留空则仅监控仓库默认分支。",
            "placeholder": "main",
            "x-widget": "list",
        },
    )
    authors: List[str] = Field(
        default_factory=list,
        description="只订阅这些作者的提交/事件，留空表示不限制作者",
        json_schema_extra={
            "label": "指定作者",
            "hint": "填写 GitHub 用户名或提交邮箱，可添加多个。仅推送这些作者产生的提交、Issue 与 PR。留空表示不限作者。",
            "placeholder": "torvalds",
            "x-widget": "list",
        },
    )
    watch_commits: bool = Field(
        default=True,
        description="订阅提交推送",
        json_schema_extra={"label": "订阅提交（Commits）"},
    )
    watch_releases: bool = Field(
        default=True,
        description="订阅 Release 更新",
        json_schema_extra={"label": "订阅 Release"},
    )
    watch_issues: bool = Field(
        default=True,
        description="订阅 Issues",
        json_schema_extra={"label": "订阅 Issues"},
    )
    watch_prs: bool = Field(
        default=True,
        description="订阅 Pull Requests",
        json_schema_extra={"label": "订阅 Pull Requests"},
    )
    watch_tags: bool = Field(
        default=False,
        description="订阅 Tags 变化",
        json_schema_extra={
            "label": "订阅 Tags",
            "hint": "Tags 与 Release 经常重复，默认关闭；仅在仓库只打 Tag 不发 Release 时开启。",
        },
    )
    issue_state: Literal["all", "open"] = Field(
        default="all",
        description="跟踪哪些状态的 Issue / PR",
        json_schema_extra={
            "label": "Issue/PR 跟踪范围",
            "hint": "all 表示同时跟踪新开与关闭/合并；open 表示只关注新开的。",
        },
    )
    notify_issue_close: bool = Field(
        default=True,
        description="Issue 关闭时是否通知",
        json_schema_extra={"label": "通知 Issue 关闭"},
    )
    notify_pr_merge: bool = Field(
        default=True,
        description="PR 合并时是否通知",
        json_schema_extra={"label": "通知 PR 合并"},
    )
    ignore_keywords: List[str] = Field(
        default_factory=list,
        description="该仓库标题命中后忽略的关键词",
        json_schema_extra={
            "label": "仓库级忽略关键词",
            "hint": "提交信息、Issue/PR 标题包含任一关键词时不推送（不区分大小写）。每项一个词。",
            "x-widget": "list",
        },
    )
    use_custom_targets: bool = Field(
        default=False,
        description="是否为该仓库单独指定通知群聊与私聊；关闭时继承全局通知目标",
        json_schema_extra={
            "label": "单独配置通知目标（关闭时继承全局）",
        },
    )
    group_targets: List[str] = Field(
        default_factory=list,
        description="该仓库单独发送通知的群号列表，仅在开启单独配置后生效",
        json_schema_extra={
            "label": "本仓库群聊（开启“单独配置”后生效，留空不发群）",
            "placeholder": "123456789",
            "x-widget": "list",
        },
    )
    private_targets: List[str] = Field(
        default_factory=list,
        description="该仓库单独发送通知的私聊号列表，仅在开启单独配置后生效",
        json_schema_extra={
            "label": "本仓库私聊（开启“单独配置”后生效，留空不发私聊）",
            "placeholder": "12345678",
            "x-widget": "list",
        },
    )
    note: str = Field(
        default="",
        description="备注，仅用于配置页备忘",
        json_schema_extra={
            "label": "备注",
            "hint": "可填写这个仓库的用途说明，通知中不会出现。",
            "placeholder": "例如：主程序仓库",
        },
    )

    def resolve_targets(self, notify: "NotifySection") -> Tuple[List[str], List[str]]:
        """返回该仓库生效的 (群聊, 私聊) 目标；未开启单独配置时继承全局。"""

        if self.use_custom_targets:
            return list(self.group_targets), list(self.private_targets)
        return list(notify.group_targets), list(notify.private_targets)

    @model_validator(mode="after")
    def _branch_repository(self) -> "RepoSubscription":
        for branch in self.branches:
            if " :: " in branch and branch.split(" :: ", 1)[0].casefold() != self.repo.casefold():
                raise ValueError("请选择当前仓库对应的分支选项，选项前缀必须与仓库一致")
        return self

    @field_validator("repo")
    @classmethod
    def _validate_repo(cls, value: str) -> str:
        if not str(value).strip():
            # 允许默认空值（未填写的占位条目在保存时会被过滤）
            return ""
        return _normalize_repo(value)

    @field_validator("branches", "authors")
    @classmethod
    def _clean_list(cls, values: List[str]) -> List[str]:
        return _dedupe(values)

    @field_validator("ignore_keywords")
    @classmethod
    def _clean_keywords(cls, values: List[str]) -> List[str]:
        return [item for item in _dedupe(values) if item]

    @field_validator("group_targets")
    @classmethod
    def _validate_groups(cls, values: List[str]) -> List[str]:
        return _clean_group_targets(values)

    @field_validator("private_targets")
    @classmethod
    def _validate_users(cls, values: List[str]) -> List[str]:
        return _clean_private_targets(values)


class SubscriptionsSection(PluginConfigBase):
    """多仓库订阅列表。"""

    __ui_label__ = "订阅仓库"
    __ui_icon__ = "github"
    __ui_order__ = 2

    repos: List[RepoSubscription] = Field(
        default_factory=lambda: [
            RepoSubscription(
                repo="",
                note="在这里填写第一个要订阅的仓库，例如 Mai-with-u/MaiBot",
            )
        ],
        description="要订阅的 GitHub 仓库列表",
        json_schema_extra={
            "label": "仓库订阅列表",
            "hint": "每项一个仓库。可分别设置订阅的分支、作者以及要推送的提交/Release/Issue/PR/Tag。留空仓库的条目会被自动忽略。",
        },
    )

    @field_validator("repos")
    @classmethod
    def _validate_repos(cls, repos: List[RepoSubscription]) -> List[RepoSubscription]:
        result: List[RepoSubscription] = []
        seen: set[str] = set()
        for item in repos:
            if not item.repo:
                continue
            key = item.repo.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result


class NotifySection(PluginConfigBase):
    """手动配置接收通知的群聊与私聊。"""

    __ui_label__ = "通知目标"
    __ui_icon__ = "send"
    __ui_order__ = 3

    platform: str = Field(
        default="qq",
        description="发送通知使用的平台标识",
        json_schema_extra={
            "label": "发送平台",
            "hint": "默认 qq。使用其它适配器时填写对应平台标识，例如 telegram。",
            "placeholder": "qq",
        },
    )
    account_id: str = Field(default="", description="多账号时指定机器人账号 ID，留空要求目标仅存在一个真实会话", json_schema_extra={"label": "发送账号 ID", "hint": "同一平台有多个机器人时填写负责发送的机器人账号。"})
    scope: str = Field(default="", description="可选的适配器路由范围", json_schema_extra={"label": "路由范围", "hint": "通常留空，仅多路由适配器需要填写。"})
    group_targets: List[str] = Field(
        default_factory=list,
        description="接收通知的 QQ 群号列表",
        json_schema_extra={
            "label": "通知群聊",
            "hint": "每项填写一个麦麦已加入的 QQ 群号。所有订阅事件会推送到这里配置的群。留空表示不推送到群。",
            "placeholder": "123456789",
            "x-widget": "list",
        },
    )
    private_targets: List[str] = Field(
        default_factory=list,
        description="接收通知的私聊 QQ 号列表",
        json_schema_extra={
            "label": "通知私聊",
            "hint": "每项填写一个接收人的 QQ 号，直接推送到该私聊。留空表示不私聊推送。",
            "placeholder": "12345678",
            "x-widget": "list",
        },
    )
    notify_on_first_run: bool = Field(
        default=False,
        description="首次采集时是否推送历史事件",
        json_schema_extra={
            "label": "首次采集推送历史",
            "hint": "默认关闭：第一次采集只记录基线，不推送一堆历史事件，避免刷屏。开启后会推送首次拉取到的最新若干条。",
        },
    )
    startup_delay_seconds: int = Field(
        default=20,
        ge=0,
        le=600,
        description="插件加载后延迟多久开始首次轮询（秒）",
        json_schema_extra={
            "label": "启动延迟（秒）",
            "hint": "避免与麦麦启动时其它任务争抢资源，默认 20 秒。",
            "x-widget": "number",
        },
    )
    send_as_forward: bool = Field(
        default=False,
        description="多条事件合并为合并转发消息发送",
        json_schema_extra={
            "label": "合并转发发送",
            "hint": "开启后，同一轮的多条事件会尝试以合并转发形式发送，减少刷屏；适配器不支持时自动退回逐条发送。",
        },
    )

    @field_validator("group_targets")
    @classmethod
    def _validate_groups(cls, values: List[str]) -> List[str]:
        return _clean_group_targets(values)

    @field_validator("private_targets")
    @classmethod
    def _validate_users(cls, values: List[str]) -> List[str]:
        return _clean_private_targets(values)


class PollingSection(PluginConfigBase):
    """轮询间隔与失败处理。"""

    __ui_label__ = "轮询设置"
    __ui_icon__ = "refresh"
    __ui_order__ = 4

    interval_seconds: int = Field(
        default=300,
        ge=60,
        le=86400,
        description="两次轮询之间的间隔（秒）",
        json_schema_extra={
            "label": "轮询间隔（秒）",
            "hint": "默认 300 秒＝5 分钟，最少 60 秒。间隔越短越及时，但更耗 GitHub 请求配额。",
            "x-widget": "number",
        },
    )
    jitter_seconds: int = Field(
        default=15,
        ge=0,
        le=600,
        description="每轮轮询的随机抖动上限（秒）",
        json_schema_extra={
            "label": "随机抖动（秒）",
            "hint": "在间隔上叠加 0～该值的随机延迟，避免多个实例同时请求。默认 15 秒。",
            "x-widget": "number",
        },
    )
    max_events_per_repo_per_run: int = Field(
        default=5,
        ge=1,
        le=50,
        description="单轮单仓库最多推送的事件数",
        json_schema_extra={
            "label": "单轮单仓库上限",
            "hint": "防止一次性刷屏；超出的历史事件仅记录不推送，下一轮不会补发。",
            "x-widget": "number",
        },
    )
    max_concurrent_repos: int = Field(
        default=3,
        ge=1,
        le=10,
        description="并发拉取的仓库数量上限",
        json_schema_extra={
            "label": "并发仓库数",
            "hint": "同时请求多少个仓库，默认 3。过高可能触发 GitHub 二级限流。",
            "x-widget": "number",
        },
    )
    error_backoff_seconds: int = Field(
        default=120,
        ge=30,
        le=3600,
        description="某仓库请求失败后的退避时间（秒）",
        json_schema_extra={
            "label": "失败退避（秒）",
            "hint": "某个仓库本轮失败后，至少等待该时长再重试该仓库。默认 120 秒。",
            "x-widget": "number",
        },
    )
    first_run_lookback_hours: int = Field(
        default=72,
        ge=1,
        le=720,
        description="首次采集时回溯的小时数",
        json_schema_extra={
            "label": "首次回溯（小时）",
            "hint": "首次采集且开启“首次采集推送历史”时，只处理最近这么多小时内的事件。",
            "x-widget": "number",
        },
    )
    state_retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="已记录事件状态的保留天数",
        json_schema_extra={
            "label": "状态保留天数",
            "hint": "超过该天数的已读记录会被清理，避免状态文件无限增长。默认 30 天。",
            "x-widget": "number",
        },
    )


class FiltersSection(PluginConfigBase):
    """全局过滤规则。"""

    __ui_label__ = "过滤规则"
    __ui_icon__ = "filter"
    __ui_order__ = 5

    ignore_authors: List[str] = Field(
        default_factory=lambda: ["dependabot[bot]", "github-actions[bot]", "renovate[bot]"],
        description="全局忽略的作者（多为机器人账号）",
        json_schema_extra={
            "label": "忽略作者",
            "hint": "这些账号产生的提交/Issue/PR 一律不推送。默认已忽略常见依赖更新机器人。",
            "x-widget": "list",
        },
    )
    ignore_keywords: List[str] = Field(
        default_factory=list,
        description="全局忽略关键词",
        json_schema_extra={
            "label": "全局忽略关键词",
            "hint": "提交信息、Issue/PR 标题、Release 名称包含任一关键词时不推送（不区分大小写）。",
            "x-widget": "list",
        },
    )
    ignore_drafts: bool = Field(
        default=True,
        description="忽略草稿 PR",
        json_schema_extra={"label": "忽略草稿 PR"},
    )
    include_bot_authors: bool = Field(
        default=False,
        description="是否包含机器人作者（会覆盖忽略作者中的 bot 项）",
        json_schema_extra={
            "label": "包含机器人作者",
            "hint": "默认关闭。开启后，即使作者是 [bot] 后缀的账号也会推送（“忽略作者”中显式列出的仍然忽略）。",
        },
    )
    max_text_length: int = Field(
        default=400,
        ge=80,
        le=2000,
        description="单条通知正文的最大字符数",
        json_schema_extra={
            "label": "正文最大长度",
            "hint": "提交信息、Issue/PR 正文截断到该长度，避免超长消息。默认 400。",
            "x-widget": "number",
        },
    )


class TemplateSection(PluginConfigBase):
    """通知消息外观与内容。"""

    __ui_label__ = "消息样式"
    __ui_icon__ = "message"
    __ui_order__ = 6

    show_commit_body: bool = Field(
        default=True,
        description="提交通知中是否展示提交信息",
        json_schema_extra={"label": "显示提交信息"},
    )
    show_issue_body: bool = Field(
        default=False,
        description="Issue/PR 通知中是否展示正文摘要；默认关闭，只显示标题",
        json_schema_extra={
            "label": "显示 Issue/PR 内容摘要",
            "hint": "默认关闭，通知只显示标题。开启后会附带 Issue/PR 正文摘要，并按“正文最大长度”截断。",
        },
    )
    show_author: bool = Field(
        default=True,
        description="是否显示作者",
        json_schema_extra={"label": "显示作者"},
    )
    show_repo_full_name: bool = Field(
        default=True,
        description="是否显示仓库完整名 owner/repo",
        json_schema_extra={"label": "显示仓库完整名"},
    )
    use_emoji: bool = Field(
        default=True,
        description="在通知标题前使用 emoji 图标",
        json_schema_extra={
            "label": "使用 emoji 图标",
            "hint": "为不同类型的通知加上 📦 提交、🚀 发布、🐞 Issue、🔀 PR、🏷️ Tag 图标，便于阅读。",
        },
    )
    footer_text: str = Field(
        default="— MaiBot GitHub Subscriber",
        description="通知末尾的落款，留空则不显示",
        json_schema_extra={
            "label": "落款",
            "hint": "每条通知末尾附加的文字，留空不显示。",
            "placeholder": "— MaiBot GitHub Subscriber",
        },
    )


class AdvancedSection(PluginConfigBase):
    """高级与调试选项。"""

    __ui_label__ = "高级设置"
    __ui_icon__ = "sliders"
    __ui_order__ = 7

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="插件日志级别",
        json_schema_extra={
            "label": "日志级别",
            "hint": "排查问题时调到 DEBUG，可看到每次请求和状态变化细节。",
        },
    )
    persist_state: bool = Field(
        default=True,
        description="是否持久化订阅进度到数据目录",
        json_schema_extra={
            "label": "持久化进度",
            "hint": "默认开启。关闭后每次重启都会重新建立基线，可能漏推或重复推送。",
        },
    )
    dry_run: bool = Field(
        default=False,
        description="演练模式：只记录不真正发送",
        json_schema_extra={
            "label": "演练模式",
            "hint": "开启后轮询照常进行，但不会真正发送任何消息，仅在日志中记录将要推送的内容，便于先验证配置。",
        },
    )
    test_command_enabled: bool = Field(
        default=True,
        description="是否允许 /githubsub test 发送测试消息",
        json_schema_extra={"label": "允许发送测试消息"},
    )


class GithubSubConfig(PluginConfigBase):
    """MaiBot GitHub Subscriber 插件配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    github: GitHubSection = Field(default_factory=GitHubSection)
    subscriptions: SubscriptionsSection = Field(default_factory=SubscriptionsSection)
    notify: NotifySection = Field(default_factory=NotifySection)
    polling: PollingSection = Field(default_factory=PollingSection)
    filters: FiltersSection = Field(default_factory=FiltersSection)
    template: TemplateSection = Field(default_factory=TemplateSection)
    advanced: AdvancedSection = Field(default_factory=AdvancedSection)

    def notify_targets_for(self, repo: RepoSubscription) -> Tuple[List[str], List[str]]:
        """返回指定仓库实际使用的 (群聊, 私聊) 目标。"""

        return repo.resolve_targets(self.notify)

    def custom_target_repos(self) -> List[RepoSubscription]:
        """返回启用了仓库级通知目标的已订阅仓库。"""

        return [
            repo
            for repo in self.subscriptions.repos
            if repo.enabled and repo.repo and repo.use_custom_targets
        ]

    def all_notify_targets(self) -> Tuple[List[str], List[str]]:
        """汇总全局与所有已启用仓库实际生效的目标，用于测试消息和状态展示。"""

        groups = list(self.notify.group_targets)
        privates = list(self.notify.private_targets)
        for repo in self.subscriptions.repos:
            if not repo.enabled or not repo.repo:
                continue
            repo_groups, repo_privates = self.notify_targets_for(repo)
            groups.extend(repo_groups)
            privates.extend(repo_privates)
        return list(dict.fromkeys(groups)), list(dict.fromkeys(privates))

    @model_validator(mode="after")
    def _validate_timezone(self) -> "GithubSubConfig":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.plugin.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"无效的时区：{self.plugin.timezone}") from exc
        return self
