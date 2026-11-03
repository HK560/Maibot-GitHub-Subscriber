# MaiBot GitHub 订阅通知插件测试报告

## 2026-11-03 14:25 v1.1.0 通知目标简化与订阅信息按钮

- 按需求删除「私聊白名单」「私聊黑名单」「群黑名单」。通知目标只由「通知群聊」「通知私聊」两个手动填写的列表决定，不再做额外名单过滤。
- 新增顶部按钮「立即发送当前仓库订阅信息」：把仓库、分支、作者、事件、备注及目标数量发送到已保存的通知目标，便于在群里核对订阅内容。
- 命令权限同步收紧：除 `/githubsub help` 外，其余命令仅管理员或本机操作员可用。
- 插件版本升至 1.1.0，配置格式版本升至 1.1.0；旧配置中的已删除字段会被自动忽略。
- 新增订阅清单生成、空仓库提示、按钮投递、旧字段兼容等回归用例；完整测试 **85 项通过**。

## 2026-11-03 14:10 配置列表修复复验

- 原因：插件配置页不识别 `ui_type=tags`，回退为普通文本输入框，底层数组没有以列表展示。已将全部此类字段改为官方支持的 `list`。
- 浏览器确认通知群聊、通知私聊均显示「添加项目」，可增删多个号码。
- 顶部按钮明确命名为「立即发送测试通知」；无有效目标时显示未发送原因，成功/失败/跳过/结果未知均有提示。
- 本机通过宿主官方开关仅重载该插件，配置 Schema 和按钮声明已生效。真实 Chrome 页面及相关接口验证通过；Chrome 另有宿主密码输入框不在 form 内的 verbose 建议，不影响本次功能。
- 未向真实群聊或私聊发送测试消息；按钮投递使用模拟通知器验证。

- 测试时间：2026-11-03 12:35（后续补测见上方）
- 测试环境：本机 Ubuntu Linux / Python 3.13 / MaiBot 1.3.19 / SDK 2.21.0 / WebUI 1.9.13
- 被测插件：`mai.github-sub` v1.1.0

## 1. 自动化单元与回归测试

执行命令：
```bash
PYTHONPATH=tests /root/.local/share/uv/tools/maibot/bin/python -m unittest discover -s tests -q
```

结果：**85 项测试全部通过**。

覆盖的关键边界包括：
1. **配置模型与 Schema**：8 个配置节字段验证、默认值生成、多仓库列表模型、仓库名自动归一化（支持 `https://github.com/` 和 `git@github.com:`）、QQ 群号/QQ 号正则与去重、展示时区有效性校验。
2. **分支选择与仓库校验**：`owner/repo :: branch` 前缀格式校验与跨仓库选择拦截。
3. **GitHub API 客户端**：Releases/Commits/Issues/Pull Requests/Tags 接口解析、ETag 304 缓存复用、401 鉴权失败、404 未找到、403/429 速率限制处理、网络超时与异常重试。
4. **多仓库采集事务性**：单仓库内只要有一个接口异常即事务性回滚，不向持久化状态写入部分进度；失败时记录错误退避时间；支持 `force=True` 手动重测。
5. **持久化 Outbox 与投递隔离**：发现的新事件先写入磁盘 Outbox 再投递；按目标独立隔离投递；已成功目标不重复发送；明确失败最多重试 3 次；超时/网络中断（结果未知）不自动重试避免刷屏。
6. **手动通知目标**：所有填写的群聊与私聊均会收到；未填写目标时不发送；发送前热更新目标列表会阻止已删除目标继续收消息。
7. **数据清理与墓碑防重**：历史 Issue/PR 更新时间清理、500 条墓碑记录防止保留期清理后旧条目重新宣布、已知水位过滤翻页旧条目。
8. **命令与权限**：`/githubsub` 帮助、状态、列表、速率、检查、测试、暂停、恢复，本地操作员与管理员权限校验。
9. **运行时控件与异步任务**：`check_now`、`send_summary`、`send_test`、`refresh_branches` 均使用异步任务并在 WebUI 8 秒 RPC 超时内立即返回状态。

## 2. 官方 ZIP 安装器与宿主兼容性验证

执行命令：
```bash
python3 scripts/build_release.py
MAIBOT_INSTANCE_DIR=/root/maibot /root/.local/share/uv/tools/maibot/bin/python scripts/verify_host.py
```

结果：
- 生成发布包：`dist/mai-github-sub-v1.1.0.zip`。
- 发布包严格遵循官方清单白名单，排除 `config.toml`、`.maibot-release.json`、`.git`、`logs/` 与 `__pycache__/`。
- 本机真实 `zip_install.install_zip_archive` 解压校验通过。
- 本机真实 `PluginLoader` 成功识别并加载。
- 导出的 WebUI 配置包含完整的 8 个配置节与 4 个运行时控件。

## 3. 本机运行中的 MaiBot 联调验证

1. **插件热重载与生命周期**：
   - 通过 WebUI `/api/webui/plugins/config/mai.github-sub/toggle` 执行禁用与启用，状态成功在 `inactive` 与 `success` 之间切换。
   - 重启 MaiBot 服务（`systemctl restart maibot`），后台日志确认 `mai.github-sub` v1.1.0 加载成功并注入 `PluginContext`。
2. **多仓库配置保存与热更新**：
   - 通过 WebUI PUT 接口测试同时配置 `Mai-with-u/MaiBot` 与 `encode/httpx` 两个仓库，保存返回 200 且配置即时生效。
   - 验证后恢复默认安全状态（通知群与私聊目标保持为空）。
3. **真实无头 Chrome 页面渲染验证**：
   - 登录凭据通过 CDP 写入临时 profile，打开 `http://127.0.0.1:8001/plugin-config`。
   - 截图 `artifacts/config-page.png` 确认：
     - 顶部显示插件标题、版本号、启用开关与保存按钮。
     - 运行状态卡片显示当前订阅仓库数与 4 个快捷操作按钮（含“立即发送当前仓库订阅信息”）。
     - GitHub Token 字段已渲染为密码框（`type="password"`）。
     - 订阅仓库列表支持添加、删除、分支多选与各项事件独立勾选。
     - 通知目标支持通知群聊与通知私聊两个多项目列表，不再包含白名单/黑名单。
