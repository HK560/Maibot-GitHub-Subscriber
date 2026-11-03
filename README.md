# GitHub 仓库订阅通知 · MaiBot

在 MaiBot 中订阅多个 GitHub 仓库，把提交、Release、Issue、Pull Request 和 Tag 变化推送到手动填写的群聊或私聊。每个仓库独立选择事件类型、提交分支和作者；通知目标只按填写的号码发送，不额外维护白名单或黑名单。

插件 ID：`mai.github-sub`，MIT 协议。需要 MaiBot **1.3.17+（1.3.x）** 和插件 SDK **2.19.0+（2.x）**；本机验证环境为 MaiBot 1.3.19、SDK 2.21.0、Python 3.13。

## 安装与开始使用

1. 将插件目录放入 MaiBot 实例的 `plugins/`，或在 WebUI 中上传构建的 ZIP。
2. 在 WebUI「插件扩展」打开 **GitHub 仓库订阅通知**。首次安装由 SDK 自动生成本地配置，不需要手工创建。
3. 在「订阅仓库」中添加 `owner/repo`，分别打开提交、Release、Issue、PR、Tag 开关。
4. 在「通知目标」中填写群号或私聊 QQ 号。机器人需要已在目标聊天中接收过消息，以便定位真实会话；多个机器人账号共用目标时，填写发送账号 ID。
5. 保存后配置自动热更新。点击「立即检查一次」，页面显示后台检查结果；首次采集默认只建立基线，避免推送历史内容。
6. 通知群聊与通知私聊都使用多项列表：点击「添加项目」逐个添加号码，每项一个，可删除多余项。先点击页面右上角「保存」，再点击页面顶部的按钮验证：
   - **立即发送当前仓库订阅信息**：把当前仓库、分支、作者、事件清单发送到已填写的目标；
   - **立即发送测试通知**：发送一条测试消息，确认目标可以收到。
   结果显示在运行状态区域。目标默认为空，不会向陌生群或用户发送。

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
| 订阅仓库 | 多仓库、分支、作者、提交/Release/Issue/PR/Tag 开关、状态范围、关键词 |
| 通知目标 | 群号、私聊 QQ 号、多账号路由、历史推送、合并转发 |
| 轮询设置 | 间隔、抖动、并发数、单轮上限、失败退避、首次回溯、状态保留 |
| 过滤规则 | 忽略作者、机器人账号、关键词、草稿 PR、摘要长度 |
| 消息样式 | 作者、仓库名、提交与 Issue/PR 摘要、emoji、落款 |
| 高级设置 | 日志级别、进度持久化、演练模式、测试消息开关 |

作者填写 GitHub 用户名、提交作者名或邮箱；提交匹配多个身份字段，Release 匹配发布者，Issue/PR 匹配创建者。Tag 列表不提供作者，因此 Tag 通知不应用作者筛选。默认忽略常见机器人；如需接收其内容，同时调整「忽略作者」和「包含机器人作者」。

插件只向「通知群聊」和「通知私聊」中显式填写的号码发送，填写后保存立即生效，不需要维护白名单或黑名单。发送前会重新读取最新目标列表；删除某个号码后，排队中的旧消息不会再发往该目标。私聊目标必须是机器人能够定位到的真实会话：同一平台存在多个机器人账号时，填写「发送账号 ID」指定发送方，否则只有唯一会话时才会发送。

## 命令

| 命令 | 用途 |
| --- | --- |
| `/githubsub status` | 运行状态与上轮统计 |
| `/githubsub list` | 仓库、分支及事件配置 |
| `/githubsub rate` | GitHub 额度 |
| `/githubsub check` | 手动检查 |
| `/githubsub test` | 测试通知目标 |
| `/githubsub pause`、`/githubsub resume` | 暂停、恢复后台轮询 |
| `/githubsub help` | 使用帮助 |

除 `help` 外，命令仅管理员或本机操作员可用。管理员填写 QQ 号或 `平台:用户号`。宿主命令权限规则仍生效。还提供只读 `github_sub_status` 工具供麦麦查询。

暂停保留到本次插件卸载；保存配置不会解除暂停。自动轮询暂停后，管理员仍可手动检查。

## 投递与采集语义

- 使用 GitHub REST **轮询**，无需开放 Webhook 端口。默认间隔 300 秒，最短 60 秒；未认证请求通常只有 60 次/小时。多个仓库、多分支和多种事件合用额度，建议配置 Token 或增加间隔。
- 使用 ETag 条件请求与有界网络重试，认证/权限错误不会当作成功。一个仓库的已配置接口全部成功后才提交本轮进度，避免部分采集失败丢事件。
- 通知先写入待发队列，再逐个目标记录发送回执。明确失败最多尝试 3 次；已成功的目标不重复发送。超时、进程在发送中断开等**结果未知**的情况不会自动重发，避免群内刷屏。
- 首次默认记录基线；开启历史推送后，时间信息可用的事件受首次回溯时长限制。Tag 列表不提供时间，首次开启历史通知时无法按时间过滤 Tag。
- 每类接口每轮读取一页（可配置 1–100 条），每仓库默认只推送本轮最新 5 条，超限内容会标记为已读并舍弃。高活跃仓库或长时间停机期间可能遗漏内容；这不是完整 GitHub 审计日志。PR/Issue 两次轮询之间的中间状态、评论和每次正文编辑不单独通知。
- 无通知目标时仍建立采集进度，后来添加目标不会补发之前的事件。演练模式只记录日志、不发送；退出演练后不会补发演练期间的内容。
- 状态位于 MaiBot 分配的 `data/plugins/mai.github-sub/state.json`，不放在插件源码目录。升级时保留该数据目录和本地配置。

## 仓库格式与发布

目录遵循官方 Manifest v2 与 SDK 插件格式：

```text
_manifest.json       # 插件清单
plugin.py            # MaiBotPlugin + create_plugin() + 生命周期
config.py            # PluginConfigBase 配置与中文表单
client.py            # GitHub API
engine.py            # 轮询与投递队列
notify.py            # 名单与真实会话
state.py             # 持久化进度
models.py            # 事件与通知文案
config.example.toml  # 无凭据的参考配置
requirements.txt
pyproject.toml
README.md
LICENSE
tests/
scripts/
```

运行时的 `config.toml` 和宿主生成的 `.maibot-release.json` **不得提交到源码或打入 ZIP**。前者由配置模型生成，后者是宿主安装收据。构建脚本只打包白名单文件：

```bash
python scripts/build_release.py
```

当前未关联远程仓库，也未发布 GitHub Release。清单中的 `https://github.com/OWNER/mai-github-sub` 是明确的发布占位地址；公开发布前必须替换为实际仓库地址及作者信息，再创建与清单版本一致的 `v1.0.0` Release。仅完成本地开发和安装，不代表已通过官方市场收录。

官方规范：[贡献指南](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)、[版本规范](https://github.com/Mai-with-u/plugin-repo/blob/main/VERSIONING.md)、[SDK](https://github.com/Mai-with-u/maibot-plugin-sdk)。

## 开发与验证

在安装有 SDK 的 Python 环境中运行（普通环境需先安装项目依赖）：

```bash
python -m unittest discover -s tests -v
```

单元测试使用模拟 GitHub 和消息能力，不向真实聊天发送。`tests/browser_smoke.py` 是可选的本机 Chrome 联调工具，额外需要 `websockets`；不属于插件运行依赖。具体本机验证证据见 `TEST_REPORT.md`。

GitHub Token 以明文保存在本地配置，网页使用密码输入框遮挡。推荐 fine-grained Token，仅给目标仓库的 Contents、Issues、Pull requests **读取**权限。classic 的 `repo` scope 同时包含写权限，不存在 `repo:read` 这种 classic scope。不要将本地凭据提交到仓库。
