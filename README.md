# 本地陪伴 Bot

AstrBot v4.29.4（`61335414433db721b3f7ecc93f90deafe0a59d78`）+ 自研插件 + 本机 PostgreSQL。

## 启动与使用

1. 双击 `Start-Bot.cmd`，运行 `启动陪伴Bot.ps1`，或执行 `.venv\Scripts\python.exe start.py`。已经在线时不需要重复启动。
2. 打开 `http://127.0.0.1:6185/#/chatbox`，本机免账号登录。
3. 在 AstrBot 的 Chat 中创建会话并聊天。
4. 点击聊天页右上角 **Bot 设置**，可以编辑名字和人设、查看核心/活跃/归档记忆及来源、查看记忆规则并调整上下文预算。保存后从下一条消息生效，设置保存在 PostgreSQL。也可直接打开 `http://127.0.0.1:6185/#/plugin-view/astrbot_plugin_companion_memory/memories`。
5. 可手动整理今天；自动任务在北京时间 03:00 之后整理未完成日期。关闭电脑时不运行，下次在线补做。

`local_config.json` 保存模型、Key 来源文件等本地配置。Key 仅在启动时从用户提供的文本文件读取，AstrBot 配置中保存环境变量引用 `$DEEPSEEK_API_KEY`。不要删除或移动来源文件，除非同时修改路径。

PostgreSQL 连接凭据在 `%USERPROFILE%\.codex\credentials\postgresql-local.json`，由 Windows 当前用户 DPAPI 加密；`local_secrets.py` 负责读取。本项目只使用 `companion_bot` 数据库。此凭据不能直接复制到其他 Windows 账号或另一台电脑使用。

免登录模式通过本地适配层保留原来的内部用户身份，因此已有聊天和记忆不变。服务固定监听 `127.0.0.1`，并验证实际连接地址、Host、Origin；该模式用于本机浏览器，不用于公网部署。上游 AstrBot checkout 不做改动，页面入口通过本地适配层加载。

## 已实现的规则

- 普通记忆等级为 `max(0, 10 - floor(距离用户上次提及的秒数 / 172800))`，连续 8 天未提及归档；核心记忆不参与衰减。
- 每天一个整理任务；只处理新增消息，有日期检查点。长对话最多 8 批，每批有输入预算；失败最多自动尝试 3 次。
- 新记忆和变化必须引用原始用户消息。整理使用用户消息时间，不用任务运行时间恢复等级。
- 每轮随机读取一条有效归档记忆。检索、随机抽取、查看页面和 Bot 自己提起都不会恢复等级。
- 用户重新提及/确认由主聊天模型按需调用确认工具；程序验证用户身份、当前消息及逐字证据。不为每轮聊天再调用一个摘要模型。
- 用户否认时可立即失效旧记忆，更新内容由每日整理提取。旧版本与来源保留。
- 记忆默认最多约 2000 的保守 Token 估算预算；近期对话默认 4000。估算采用 UTF-8 字节上界，较实际 Token 数保守。工具 schema 和固定提示词另计。
- 通过 `mark_as_temp()` 注入单轮记忆，不把记忆块不断叠加进后续历史。
- 数据库故障时不继续无记录地调用聊天模型；界面返回明确失败，恢复后重试。

## 当前范围与限制

- 当前只验证本机 Web 私聊；QQ、微信后续接入。
- pgvector 0.8.7 已安装到现有 PostgreSQL 18，在 `companion_bot` 中启用。Windows 包来源为 `andreiramani/pgvector_pgsql_windows` 的 PG18 专用发布，SHA256 及安装文件清单记录在 `runtime/pgvector-install.json`；未替换 PostgreSQL 服务。
- 语义检索使用本地 CPU ONNX 模型 `BAAI/bge-small-zh-v1.5`，512 维；固定模型仓库 revision 为 `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`。查询和记忆向量在本机计算，模型路径为 `runtime/models/bge-small-zh-v1.5`。命中候选由主聊天模型判断相关性。
- 提及和纠错的语义判定仍依赖模型，来源校验不等于语义一定正确，需继续用真实对话评估。
- 只开放本机 127.0.0.1；未做公网部署。
- Windows 使用 Selector 事件循环以支持 psycopg。当前工具限制为本插件的记忆工具；没有启用命令执行等 Agent 功能。
- 原始聊天、记忆、事件等在 PostgreSQL。AstrBot 自身的运行设置、ChatUI 展示历史和管理信息仍使用它自己的本地数据文件，插件重建模型近期上下文时以 PostgreSQL 为准。

## 验证与备份

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check plugin tests *.py
.venv\Scripts\python.exe backup.py
```

测试使用独立的随机测试 schema，不修改正式记忆。`backup.py` 仅导出 `companion_bot`，输出到 `runtime\backups`。

插件源码在 `plugin/`，启动时同步到运行目录。修改插件后重启实例生效。前台运行时用 Ctrl+C 停止；后台实例使用 `停止陪伴Bot.ps1`。上游源码在 `vendor/AstrBot`，保持原状，升级前先备份数据库和 runtime；回退时恢复对应代码与备份，不删除原始素材。

当前验证：16 项自动化测试通过，覆盖 PostgreSQL 记忆/设置、免登录访问和本机访问边界；空白浏览器聊天、设置保存与刷新保留、记忆/规则切换通过；真实 DeepSeek 验证人设在下一条回复生效，真实每日整理→向量入库→归档语义召回→恢复 10 级通过。自动整理的跨日真实定时触发尚需运行观察，当前通过可控日期和手动触发验证相同任务逻辑。备份导出已执行，完整恢复流程尚未演练。

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
