# 本地陪伴 Bot

AstrBot v4.29.4（`61335414433db721b3f7ecc93f90deafe0a59d78`）+ 自研插件 + 本机 PostgreSQL。

## 启动与使用

1. 双击桌面“陪伴Bot”快捷方式或本机 `runtime/desktop/CompanionBot/CompanionBot.exe`。也可用 `Start-Bot.vbs`、`Start-Bot.cmd` 或 `启动陪伴Bot.ps1` 打开托盘 GUI。服务已运行时会连接现有实例，重复打开只显示已有窗口。
2. 打开 `http://127.0.0.1:6185/#/chatbox`，本机免账号登录。
3. 在 AstrBot 的 Chat 中创建会话并聊天。
4. 点击聊天页右上角 **Bot 设置**，可以编辑 L0 固定人设、查看 L1 长期习惯、审批 Bot 修改申请、查看 L2 记忆及规则、管理自主上网。保存或批准后从下一条消息生效，设置保存在 PostgreSQL。也可直接打开 `http://127.0.0.1:6185/#/plugin-view/astrbot_plugin_companion_memory/memories`。
5. 可手动整理今天；自动任务在北京时间 03:00 之后整理未完成日期。关闭电脑时不运行，下次在线补做。

## 托盘与桌面 GUI

托盘主要显示 **正在运行 · 空闲** 和 **正在运行 · 忙碌**。回复、搜索/读网页、每日记忆整理和设定检查期间，状态跟随真实任务变化；结束、异常或取消都会清除忙碌。图标空闲为绿色，忙碌为暖黄色。启动、停止或故障时显示对应的必要状态。

右键托盘可打开聊天、Bot 设置和小控制窗口，或停止/启动、退出。关闭控制窗口会收进托盘，Bot 继续在线；“退出（停止 Bot）”先调用 AstrBot 原生关闭流程，清理插件与连接，再退出托盘。PostgreSQL 服务保持运行。后端未及时响应时，控制器只会结束此项目确切路径对应的 Python 进程，不处理其他项目或数据库进程。

GUI 与后端分别限制单实例。后台进程存在且 Web、插件、数据库都就绪才显示运行中。打包程序只包含托盘与控制窗口，后端继续使用项目 `.venv`，模型、记忆和凭据仍留在原位置；因此它是当前本机安装的 EXE 启动器，**不能只复制 EXE 到另一台电脑使用**。请保留整个 `CompanionBot` 输出目录及现有项目环境。

从源码重新打包：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-gui.txt
.venv\Scripts\python.exe build_gui.py
```

输出为 `runtime/desktop/CompanionBot/CompanionBot.exe`。构建使用临时的干净 PATH，防止 PostgreSQL 的同名 ICU DLL 被误打包；不会修改系统 PATH 或 PostgreSQL 安装。命令行回退入口仍可用 `.venv\Scripts\python.exe start.py`，管理入口为 `.venv\Scripts\python.exe manage_bot.py status|start|stop`。

`local_config.json` 保存模型、Key 来源文件等本地配置。Key 仅在启动时从用户提供的文本文件读取，AstrBot 配置中保存环境变量引用 `$DEEPSEEK_API_KEY`。不要删除或移动来源文件，除非同时修改路径。

PostgreSQL 连接凭据在 `%USERPROFILE%\.codex\credentials\postgresql-local.json`，由 Windows 当前用户 DPAPI 加密；`local_secrets.py` 负责读取。本项目只使用 `companion_bot` 数据库。此凭据不能直接复制到其他 Windows 账号或另一台电脑使用。

免登录模式通过本地适配层保留原来的内部用户身份，因此已有聊天和记忆不变。服务固定监听 `127.0.0.1`，并验证实际连接地址、Host、Origin；该模式用于本机浏览器，不用于公网部署。上游 AstrBot checkout 不做改动，页面入口通过本地适配层加载。

## 人设和人工授权

| 层级 | 内容 | 生效与生命周期 |
| --- | --- | --- |
| L0 固定人设 | 名字、性格、说话方式、行为边界、世界观 | 每轮注入，最高人设优先级，不衰减 |
| L1 长期习惯 | 称呼、表达习惯、共同梗、互动风格 | 只注入已批准且未暂停的习惯，不参与普通记忆衰减 |
| L2 记忆 | 用户事实、共同经历、日常事件 | 按预算注入，普通记忆每两天降一级；用户核心事实不衰减 |

L0 / L1 与 L2 使用不同的表、写入路径和更新任务。旧版人设原文迁移到 L0 的“性格与核心设定”，不自动拆分或重写。固定人设建议约 300～600 Token，界面提供长度提示，不强制这个推荐区间。

Bot 可以调用 `companion_propose_persona` 提交具体的 L0 / L1 修改申请，**工具没有执行或批准权限**。申请记录旧值、新值、理由、真实用户消息来源及人设版本，经模型一致性检查后进入待审。你需要在“修改审批”中逐条点击批准并确认，程序才写入生效设定；聊天中的“同意”不算授权。检查失败、被拒绝或已过期的申请不能批准。

L1 每日候选从近 30 天最多 100 条用户消息中发现，至少有 6 条消息、跨 3 天才调用模型；每条自动候选需至少 3 条消息、跨 2 天的证据，每天最多 3 条。每天只生成候选，不自动应用。手动检查与定时任务共享日期任务，不在每轮聊天额外生成习惯摘要。

修改 L0 会让旧申请过期，并暂停已有 L1。暂停习惯可点击“重新申请检查”，检查后仍需再次批准，或直接撤回。批准或撤回 L1 后，基于旧习惯的一批待审申请也会过期，避免互相冲突的旧申请覆盖新状态。审批和人设修改保留审计记录；一致性判断仍依赖模型，最终由你决定。

## 记忆规则

- 普通记忆等级为 `max(0, 10 - floor(距离用户上次提及的秒数 / 172800))`，连续 8 天未提及归档；核心记忆不参与衰减。
- 每天一个整理任务；只处理新增消息，有日期检查点。长对话最多 8 批，每批有输入预算；失败最多自动尝试 3 次。
- 新记忆和变化必须引用原始用户消息。整理使用用户消息时间，不用任务运行时间恢复等级。
- 每轮随机读取一条有效归档记忆。检索、随机抽取、查看页面和 Bot 自己提起都不会恢复等级。
- 用户重新提及/确认由主聊天模型按需调用确认工具；程序验证用户身份、当前消息及逐字证据。不为每轮聊天再调用一个摘要模型。
- 用户否认时可立即失效旧记忆，更新内容由每日整理提取。旧版本与来源保留。
- 记忆默认最多约 2000 的保守 Token 估算预算；近期对话默认 4000。估算采用 UTF-8 字节上界，较实际 Token 数保守。工具 schema 和固定提示词另计。
- 通过 `mark_as_temp()` 注入单轮记忆，不把记忆块不断叠加进后续历史。
- 数据库故障时不继续无记录地调用聊天模型；界面返回明确失败，恢复后重试。

## 自主上网

聊天模型可按需调用 `companion_web_search` 搜索公开资料，调用 `companion_read_web` 读取网页 Markdown，回答时附来源链接。需要最新信息、核实不确定事实或用户要求查询时使用，普通闲聊不强制联网。当前接入 [DeepSeek 官方 Search / Fetch](https://api-docs.deepseek.com/guides/search/)，复用现有 DeepSeek Key，不增加搜索供应商凭据。

“自主上网”页提供开关、每轮最多调用次数、网页内容预算和最近 20 次记录。默认启用，每轮最多 3 次（可设 1～5）、内容预算 12000（可设 2000～24000，UTF-8 字节作为保守 Token 上界）。并发工具共享同一轮预算；网络错误不自动重试。Key 只发往固定的 DeepSeek API 域名；用户 URL 作为 Fetch 数据传递，不开放本机文件、浏览器账号或本地服务。

搜索和网页读取按服务商成功请求数计费，费用以官方说明为准。只发送必要查询词或页面 URL，网页结果属于外部参考，不授予修改人设的权限，也不自动写入用户记忆。Bot 在当前聊天中决定何时上网，后台没有持续爬取任务。

## 当前范围与限制

- 当前只验证本机 Web 私聊；QQ、微信后续接入。
- pgvector 0.8.7 已安装到现有 PostgreSQL 18，在 `companion_bot` 中启用。Windows 包来源为 `andreiramani/pgvector_pgsql_windows` 的 PG18 专用发布，SHA256 及安装文件清单记录在 `runtime/pgvector-install.json`；未替换 PostgreSQL 服务。
- 语义检索使用本地 CPU ONNX 模型 `BAAI/bge-small-zh-v1.5`，512 维；固定模型仓库 revision 为 `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`。查询和记忆向量在本机计算，模型路径为 `runtime/models/bge-small-zh-v1.5`。命中候选由主聊天模型判断相关性。
- 提及和纠错的语义判定仍依赖模型，来源校验不等于语义一定正确，需继续用真实对话评估。
- 只开放本机 127.0.0.1；未做公网部署。
- Windows 使用 Selector 事件循环以支持 psycopg。当前只开放本插件的记忆、人设申请和公开网页搜索/读取工具，没有命令执行或人设直接写入工具。
- 原始聊天、记忆、事件等在 PostgreSQL。AstrBot 自身的运行设置、ChatUI 展示历史和管理信息仍使用它自己的本地数据文件，插件重建模型近期上下文时以 PostgreSQL 为准。

## 验证与备份

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check plugin tests *.py
.venv\Scripts\python.exe backup.py
```

测试使用独立的随机测试 schema，不修改正式记忆。`backup.py` 仅导出 `companion_bot`，输出到 `runtime\backups`。

插件源码在 `plugin/`，启动时同步到运行目录。修改插件后重启实例生效。前台运行时用 Ctrl+C 停止；后台实例使用 `停止陪伴Bot.ps1`。上游源码在 `vendor/AstrBot`，保持原状，升级前先备份数据库和 runtime；回退时恢复对应代码与备份，不删除原始素材。

当前验证：44 项自动化测试通过，覆盖 PostgreSQL 记忆/设置、免登录访问、本机访问边界、人工审批、拒绝/冲突、版本变化、跨日候选、L1 重审、联网开关、调用/内容预算、忙闲与取消清理、单实例、停止范围、关闭窗口留托盘和启动中退出。

真实 DeepSeek 验收通过：Bot 工具申请→检查通过但不生效，聊天“同意”仍不执行；跨日发现 L1→页面显示差异与来源→取消保持待审→手动确认批准→刷新保留。合成用户数据已清理，正式 L0 保持原样。真实搜索→读取官方网页→带来源回答通过，调用记录证实两种联网工具均执行；联网开关保存与刷新保留通过。插件沙箱内使用页面对话框进行二次确认，不依赖被沙箱拦截的浏览器原生 `confirm()`。

对应脚本为 `persona_live_test.py`、`persona_browser_test.cjs`、`web_live_test.py`、`web_browser_test.cjs`；运行真实测试会消耗 API 调用。原有 Web 聊天、人设下一轮生效、每日整理→向量入库→归档语义召回→恢复 10 级的验证脚本仍保留。自动整理的跨日真实定时触发尚需运行观察，当前通过可控日期和手动触发验证相同任务逻辑。备份导出已执行，完整恢复流程尚未演练。

`desktop_live_test.py` 已验证真实打包 EXE 在 Windows 平台打开窗口并注册托盘、重复打开复用实例、真实 DeepSeek 读网页时忙→闲、退出后 GUI/后端均结束且 PostgreSQL 继续在线、重新打开 EXE 自动启动后端。Qt 自身的窗口测试验证了关闭收进托盘和启动途中退出。Computer Use 内核在本环境报路径初始化错误，因此本次没有通过该工具实际点击 Windows 托盘区域。

## 依赖安装

```powershell
git clone --depth 1 --branch v4.29.4 https://github.com/AstrBotDevs/AstrBot.git vendor/AstrBot
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-lock.txt
Copy-Item local_config.json.example local_config.json
```

编辑本地配置中的凭据来源路径。数据库凭据、数据库内容、API Key、模型、运行日志和上游源码不随本仓库上传；新环境需先配置 PostgreSQL 连接、启用 pgvector 并下载向量模型。模型由 `download_embedding.py` 下载并校验，Windows PG18 扩展由 `install_pgvector.py` 验证并启用，详细版本及来源固定在对应脚本内。浏览器回归测试使用本地 Chrome 与 `runtime/browser` 下安装的 `playwright-core`。

上游仓库：https://github.com/AstrBotDevs/AstrBot

插件接口：https://docs.astrbot.app/dev/star/guides/listen-message-event.html
