# MaiBot GitHub Subscriber

在 MaiBot 中订阅多个 GitHub 仓库，把提交、Release、Issue、Pull Request 和 Tag 变化推送到指定的群聊或私聊。每个仓库可以独立选择事件类型、提交分支、作者和通知目标；没有单独配置通知目标时自动继承全局目标。

- **插件 ID**：`github.hk560.github-subscriber`
- **仓库**：<https://github.com/HK560/Maibot-GitHub-Subscriber>
- **许可证**：MIT
- **兼容**：MaiBot 1.3.17–1.3.x、插件 SDK 2.19.0–2.98.x、Python 3.10+
- **当前版本**：1.2.0

## 功能

- 多仓库订阅，支持提交、Release、Issue、Pull Request、Tag 五类事件。
- 每个仓库独立选择分支、作者、事件类型、Issue/PR 状态范围和忽略关键词。
- 支持全局通知目标，也支持每个仓库单独指定群聊/私聊；未单独配置时继承全局。
- 使用 GitHub REST 轮询，不需要开放 Webhook 端口。
- 通知先写待发队列，再按目标记录回执；明确失败有限重试，已成功目标不会重复发送。
- 提供 WebUI 可视化配置、运行状态卡片和测试/订阅摘要按钮。
- 提供 `/githubsub` 管理命令和只读 LLM 工具。

## 安装与开始使用

1. 从 [Releases](https://github.com/HK560/Maibot-GitHub-Subscriber/releases) 下载 ZIP，或在 WebUI「插件扩展」中上传构建包。
2. 也可以把插件目录放入 MaiBot 实例的 `plugins/`。
3. 在 WebUI「插件扩展」打开 **MaiBot GitHub Subscriber**。首次安装由 SDK 自动生成本地配置，不需要手工创建。
4. 在「订阅仓库」中添加 `owner/repo`，选择要推送的提交、Release、Issue、PR 和 Tag。
5. 在「通知目标」中填写全局群号或私聊 QQ 号。机器人需要已在目标聊天中接收过消息，以便定位真实会话；多个机器人账号共用目标时，填写发送账号 ID。
6. 保存后配置自动热更新。点击「立即检查一次」可手动拉取；首次采集默认只建立基线，避免推送历史内容。
7. 点击「立即发送测试通知」确认目标可达。通知群聊与通知私聊都是多项列表，先保存再测试。

若某个仓库需要发送到其它聊天，在该仓库内开启「单独配置通知目标」，再填写「本仓库群聊」和「本仓库私聊」；不开启则自动继承全局目标。

## 仓库级通知目标（可选）

每个仓库都可以在「订阅仓库」中单独指定通知对象：

- **单独配置通知目标**：默认关闭，表示该仓库继承全局目标。开启后，只使用该仓库行内填写的「本仓库群聊」与「本仓库私聊」。
- 开启后若某一类留空，表示该仓库**不向这一类发送**，不会回退到全局目标；例如只填本仓库群聊，就不会再向全局私聊发送该仓库的事件。
- 关闭开关后，该仓库立即恢复使用全局目标；已排队但属于旧目标的回执会被跳过，不会误发。
- 「立即发送测试通知」与「立即发送当前仓库订阅信息」会发送到全局目标与所有已启用仓库单独目标的去重合集。
- 运行状态、`/githubsub status`、`/githubsub list` 与订阅信息会标注每个仓库使用「继承全局」还是「单独配置」。

## 勾选指定分支

- 未读取分支前，可以直接在每个仓库的分支列表填写 `main`、`dev` 等名称；空列表表示仓库默认分支。
- 先保存仓库，再点击 **读取仓库分支选项**。后台完成后，返回插件列表并重新打开配置页，分支字段会变为可勾选的多选框。
- 选项显示为 `owner/repo :: branch`，请选择当前行所属仓库的选项。跨仓库选择会被配置校验拒绝。已填写的普通分支名继续有效。
- 添加新仓库后，先保存再重新读取分支。分支选项是运行时缓存，重启后可再次读取；已保存的订阅不受影响。
- 分支名称按 GitHub 精确匹配，支持包含 `/` 的分支名，不支持通配符。同一轮多个分支包含同一个提交时只推送一次。

## 可视化设置

配置页包含 8 个中文分区。宿主默认折叠「基本设置」，点击标题可展开管理员与时区设置。

| 分区 | 设置内容 |
| --- | --- |
| 基本设置 | 总开关、管理员账号、时区、配置版本 |
| GitHub 连接 | Token、Enterprise API 地址、代理、SSL、超时、每页条数 |
| 订阅仓库 | 多仓库、分支、作者、提交/Release/Issue/PR/Tag 开关、状态范围、关键词、仓库级群聊/私聊 |
| 通知目标 | 全局群号与私聊 QQ 号（仓库未单独配置时使用）、多账号路由、历史推送、合并转发 |
| 轮询设置 | 间隔、抖动、并发数、单轮上限、失败退避、首次回溯、状态保留 |
| 过滤规则 | 忽略作者、机器人账号、关键词、草稿 PR、摘要长度 |
| 消息样式 | 作者、仓库名、提交与 Issue/PR 摘要、emoji、落款 |
| 高级设置 | 日志级别、进度持久化、演练模式、测试消息开关 |

作者填写 GitHub 用户名、提交作者名或邮箱；提交匹配多个身份字段，Release 匹配发布者，Issue/PR 匹配创建者。Tag 列表不提供作者，因此 Tag 通知不应用作者筛选。默认忽略常见机器人；如需接收其内容，同时调整「忽略作者」和「包含机器人作者」。

插件只向「通知群聊」「通知私聊」以及仓库级目标中显式填写的号码发送，保存后立即生效，不需要维护白名单或黑名单。发送前会重新读取最新目标列表；删除某个号码后，排队中的旧消息不会再发往该目标。私聊目标必须是机器人能够定位到的真实会话：同一平台存在多个机器人账号时，填写「发送账号 ID」指定发送方，否则只有唯一会话时才会发送。

## 命令

| 命令 | 用途 |
| --- | --- |
| `/githubsub status` | 运行状态与上轮统计 |
| `/githubsub list` | 仓库、分支、事件与通知目标 |
| `/githubsub rate` | GitHub 额度 |
| `/githubsub check` | 手动检查 |
| `/githubsub test` | 测试通知目标 |
| `/githubsub pause`、`/githubsub resume` | 暂停、恢复后台轮询 |
| `/githubsub help` | 使用帮助 |

除 `help` 外，命令仅管理员或本机操作员可用。管理员填写 QQ 号或 `平台:用户号`。宿主命令权限规则仍生效。插件还提供只读 `github_sub_status` 工具供麦麦查询。

暂停保留到本次插件卸载；保存配置不会解除暂停。自动轮询暂停后，管理员仍可手动检查。

## 投递与采集语义

- 使用 GitHub REST **轮询**，无需开放 Webhook 端口。默认间隔 300 秒，最短 60 秒；未认证请求通常只有 60 次/小时。多个仓库、多分支和多种事件合用额度，建议配置 Token 或增加间隔。
- 使用 ETag 条件请求与有界网络重试，认证/权限错误不会当作成功。一个仓库的已配置接口全部成功后才提交本轮进度，避免部分采集失败丢事件。
- 通知先写入待发队列，再逐个目标记录发送回执。明确失败最多尝试 3 次；已成功的目标不重复发送。超时、进程在发送中断开等**结果未知**的情况不会自动重发，避免群内刷屏。
- 首次默认记录基线；开启历史推送后，时间信息可用的事件受首次回溯时长限制。Tag 列表不提供时间，首次开启历史通知时无法按时间过滤 Tag。
- 每类接口每轮读取一页（可配置 1–100 条），每仓库默认只推送本轮最新 5 条，超限内容会标记为已读并舍弃。高活跃仓库或长时间停机期间可能遗漏内容；这不是完整 GitHub 审计日志。PR/Issue 两次轮询之间的中间状态、评论和每次正文编辑不单独通知。
- 无通知目标时仍建立采集进度，后来添加目标不会补发之前的事件。演练模式只记录日志、不发送；退出演练后不会补发演练期间的内容。
- 状态保存在 MaiBot 分配的插件数据目录（通常为 `data/plugins/<插件ID>/state.json`），不放在插件源码目录。升级时保留该数据目录和本地配置。

## 项目结构

```text
_manifest.json       # 插件清单（Manifest v2）
plugin.py            # MaiBotPlugin + create_plugin() + 生命周期
config.py            # PluginConfigBase 配置与中文表单
client.py            # GitHub API 客户端
engine.py            # 轮询、过滤与投递队列
notify.py            # 通知目标解析与发送
state.py             # 进度持久化
models.py            # 事件与通知文案
config.example.toml  # 无凭据的参考配置
requirements.txt
pyproject.toml
README.md
CHANGELOG.md
LICENSE
scripts/build_release.py
tests/
```

运行时的 `config.toml`、`state.json`、日志、构建产物和宿主生成的安装收据**不得提交到源码或打入 ZIP**。

## 开发与测试

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

单元测试使用模拟 GitHub 和消息能力，不向真实聊天发送。

构建可上传的发布包：

```bash
python scripts/build_release.py
```

构建脚本只打包 `_manifest.json`、运行时代码、示例配置、依赖声明、README、CHANGELOG、LICENSE 和 pyproject，输出到 `dist/`。

## 发布

发布新版本时：

1. 更新 `_manifest.json` 和 `pyproject.toml` 中的版本号，保持两者一致。
2. 运行完整测试并构建 ZIP。
3. 提交代码后创建与版本号一致的 Tag，例如 `v1.2.0`。
4. 发布 GitHub Release，附上构建的 ZIP，或使用 GitHub 自动生成的源码包。

插件 ID 保持不变；`_manifest.json` 的 `version` 必须与 Release Tag 一致。

官方规范：

- [插件贡献指南](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)
- [版本规范](https://github.com/Mai-with-u/plugin-repo/blob/main/VERSIONING.md)
- [插件 SDK](https://github.com/Mai-with-u/maibot-plugin-sdk)

## 安全

GitHub Token 以明文保存在本地配置，网页使用密码输入框遮挡。推荐 fine-grained Token，仅授予目标仓库的 Contents、Issues、Pull requests **读取**权限。classic 的 `repo` scope 同时包含写权限，不存在 `repo:read` 这种 classic scope。不要将本地凭据提交到仓库。

## 许可证

[MIT](LICENSE)
