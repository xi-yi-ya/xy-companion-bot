# Windows 从零部署

这份指南部署的是你自己的实例：全新的数据库、默认人设，以及你自己的 DeepSeek Key 和 QQ 登录。仓库没有作者的记忆、聊天、人格数据库、模型或已登录的 QQ。

目前实际验证的平台是 **Windows x64 + Python 3.12 + PostgreSQL 18**。向量检索在本地 CPU 上运行，不需要显卡。当前模型接入固定为 DeepSeek 官方 `deepseek-flash`；这不是通用的任意模型安装包。macOS/Linux、Docker 和公网多用户部署尚未做适配。

## 1. 准备环境

- [Git for Windows](https://git-scm.com/download/win)。
- [Python 3.12](https://www.python.org/downloads/windows/)，安装时启用 Python Launcher（下文使用 `py -3.12`）。
- [PostgreSQL 18 for Windows](https://www.postgresql.org/download/windows/)，包含命令行工具 `psql`；记住管理员 `postgres` 的密码。
- 自己的 [DeepSeek API Key](https://platform.deepseek.com/api_keys)。API 调用按服务商规则计费，本地向量模型不用另付 Embedding API 费。

以下命令在 **PowerShell** 中执行，从项目根目录运行。克隆位置可以自选；不依赖作者的 D 盘目录，也不需要安装 Codex。

```powershell
git clone https://github.com/xi-yi-ya/xy-companion-bot.git
Set-Location xy-companion-bot
git clone --depth 1 --branch v4.29.4 https://github.com/AstrBotDevs/AstrBot.git vendor/AstrBot
git -C vendor/AstrBot rev-parse HEAD
```

最后应输出 `61335414433db721b3f7ecc93f90deafe0a59d78`。先用这个固定版本，避免上游接口变动；仓库不包含也不篡改上游 checkout。许可与版权说明见 [开源说明](OPEN_SOURCE.md)。

## 2. 创建 Python 环境

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
Copy-Item local_config.json.example local_config.json
```

新安装才复制配置；已有实例不要覆盖 `local_config.json`。使用虚拟环境中 Python 的完整相对路径，无须激活 venv。锁定依赖安装失败时先核对 Python 版本、x64 架构和包下载网络，不要随意把整套依赖升级到最新版。

## 3. 安装 pgvector

PostgreSQL 服务需要已经安装并运行。下面的安装器使用固定的 **pgvector 0.8.7 / PG18 x64** 社区编译包，下载后核验仓库里固定的 SHA256，只复制 `vector.dll` 和扩展 SQL/control 文件。来源为 [pgvector_pgsql_windows 0.8.7](https://github.com/andreiramani/pgvector_pgsql_windows/releases/tag/0.8.7)。

```powershell
$pgRoot = 'C:\Program Files\PostgreSQL\18'
.\.venv\Scripts\python.exe install_pgvector.py --pg-root "$pgRoot" --files-only
```

把 `$pgRoot` 改成自己的 PostgreSQL **安装目录**，不是数据目录。安装到 Program Files 时可能需要在管理员 PowerShell 中执行这一条。脚本验证 `pg_config.exe` 的 PG 主版本；不接受 PG17 或其他主版本的 DLL。其他版本请按 [pgvector 官方安装说明](https://github.com/pgvector/pgvector#windows) 自行安装匹配的扩展。

如果已经有匹配扩展，可跳过复制文件，直接继续创建数据库并启用扩展。只想验证路径和安装包，不修改系统时使用：

```powershell
.\.venv\Scripts\python.exe install_pgvector.py --pg-root "$pgRoot" --check-only
```

## 4. 创建独立数据库与账号

```powershell
& "$pgRoot\bin\psql.exe" -h 127.0.0.1 -p 5432 -U postgres -d postgres -W
```

输入 PostgreSQL 管理员密码，在 `psql` 中逐条执行：

```sql
CREATE ROLE companion_bot LOGIN;
\password companion_bot
CREATE DATABASE companion_bot OWNER companion_bot;
\connect companion_bot
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
GRANT USAGE, CREATE ON SCHEMA public TO companion_bot;
SELECT extversion FROM pg_extension WHERE extname = 'vector';
\q
```

`\password` 会隐藏输入，为应用账号设置一个独立密码；不要把密码写进公开 SQL、聊天或命令历史。已有同名账号/数据库时不要删除重建，核对归属后跳过相应创建语句。服务运行时使用普通应用账号，管理员只用于安装扩展等初始化工作。

回到项目目录，验证应用账号并保存加密连接信息：

```powershell
.\.venv\Scripts\python.exe configure_database.py --host 127.0.0.1 --port 5432 --user companion_bot
```

密码隐藏输入；脚本连接固定的 `companion_bot` 数据库，检查 pgvector 和建表权限，成功后以 **Windows 当前用户 DPAPI** 加密保存到 `runtime/secrets/postgresql.json`，配置只记录文件路径。不创建/修改数据库角色，不改变其他数据库。

应用表在首次启动时自动创建或增量补齐，不需要手动导入作者的数据库。旧安装仍能读取已有全局 DPAPI 配置；新用户不必创建 `.codex` 目录。

## 5. 配置 DeepSeek Key

推荐用隐藏输入保存为当前 Windows 用户的 `DEEPSEEK_API_KEY`：

```powershell
.\.venv\Scripts\python.exe -c "import getpass; from api_credentials import save_user_key; save_user_key(getpass.getpass('DeepSeek API Key: '))"
```

此变量按用户选择保存在本机用户环境中，不是数据库密码的 DPAPI 文件。程序优先读取当前用户环境，再读取进程环境；也支持在 `local_config.json` 的 `deepseek_key_file` 指定自己保存的 TXT/DOCX。只选一种清晰的凭据来源即可，不把真实 Key 放进仓库。

`local_config.json` 的默认 Web 端口是 `6185`；需要改端口时在首次启动前调整。当前实现启动时固定使用 `deepseek-flash`，仅改 JSON 中模型名不会切换为 GPT 或其他服务。

## 6. 下载本地模型与 Tokenizer

```powershell
.\.venv\Scripts\python.exe download_embedding.py
.\.venv\Scripts\python.exe download_tokenizer.py
```

向量模型为固定 revision 的 `BAAI/bge-small-zh-v1.5` ONNX 文件（约 95 MB 主文件），主模型核验 SHA256；Tokenizer 下载官方固定校验包，只提取 JSON，不执行包内 Python。文件位于 `runtime/models/`。

向量模型下载默认走 Hugging Face，并遵循当前 `HTTPS_PROXY` 等环境变量；没有代理也能直接访问时不必设置。需要自选兼容镜像可在本次 PowerShell 会话设置 `$env:HF_ENDPOINT='https://你的镜像域名'` 后再运行。校验失败就停止，不能把失败页面改名充当模型。Tokenizer、QQ 安装脚本各有自己的下载逻辑；遇到网络问题按实际失败的域名排查。

## 7. 先跑通 Web

```powershell
.\.venv\Scripts\python.exe start.py
```

首次启动创建 AstrBot 运行配置、同步本项目插件并初始化数据库。AstrBot 会按固定版本准备对应 WebUI 资源；首次准备需要网络。看到服务就绪后打开：

- 聊天：<http://127.0.0.1:6185/#/chatbox>
- 设置：<http://127.0.0.1:6185/#/plugin-view/astrbot_plugin_companion_memory/memories>
- 状态：<http://127.0.0.1:6185/api/v1/plugins/extensions/astrbot_plugin_companion_memory/status>

若改过端口，以上地址也相应替换。新建会话发一句话，确认可以回复；在设置里编辑自己的 L0 人设、查看记忆与修改审批。新实例记忆为空，普通记忆每天整理一次，并不保证第一条消息后立即产生摘要。

本机 Web 免账号登录，访问适配器检查真实 loopback 地址、Host 和 Origin。**不要把这个免登录入口直接暴露到公网**；源码公开不代表本地管理服务要公开。

前台按 `Ctrl+C` 停止；日常也可使用托盘启动器，见第 9 步。

## 8. 可选：接入 QQ

先停止 Bot，再安装本项目独立的 QQ 运行时：

```powershell
.\.venv\Scripts\python.exe install_qq.py
.\.venv\Scripts\python.exe start.py
```

安装器下载并验证固定 NapCat Windows Node 包，放在 `runtime/qq/`，不复用或改动平时的桌面 QQ。它不随本仓库再分发，许可与限制见 [NOTICE](../NOTICE)。

1. 打开 <http://127.0.0.1:6185/companion/qq>，用准备交给 Bot 的 QQ 小号扫码，在手机确认登录。
2. 用自己的主号加 Bot 小号为好友。
3. 用主号把本机页面生成的 `/绑定 …` 整行私聊发给 Bot，别使用文档中编造的固定口令。
4. 页面显示主号已绑定、QQ 在线后再聊天。主号与 Web 共享自己的记忆与长期习惯。
5. 群聊在“QQ 连接”里配置群号、逐群读取/回复/附件权限；私聊合并窗口和群聊扫描周期也在这里。外观与表情、空间浏览/发布、主动联系各有独立设置。

默认端口：Web `6185`、OneBot 反向 WS `6199`、NapCat 本地 WebUI `6099`。主号、登录状态和令牌由本机生成保存，不复用作者账号；不要上传 `runtime/qq/`。微信尚未接入。详细流程见 [QQ_SETUP.md](QQ_SETUP.md)。

## 9. 托盘 GUI 与 EXE

先确认上一节 Web 能正常运行，再选择 GUI：

```powershell
.\.venv\Scripts\python.exe desktop_entry.py
```

窗口可打开聊天/设置、显示空闲或忙碌、收进托盘、退出并停止 Bot。源码带有 `Start-Bot.cmd` / `Start-Bot.vbs` 等启动入口；无需每次手动进入虚拟环境。

需要本机 EXE 时：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-gui.txt
.\.venv\Scripts\python.exe build_gui.py
```

输出为 `runtime/desktop/CompanionBot/CompanionBot.exe`。这是当前安装目录的启动器，不是无需 Python/数据库的单文件便携版；保留整个输出目录、项目 venv 和本地环境。当前公开发布源码，不提供内含 QQ 原生组件、模型或私人数据的安装包。

## 更新、停止和回滚

停止实例后更新源码和依赖：

```powershell
.\.venv\Scripts\python.exe manage_bot.py stop
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe start.py
```

更新不覆盖自己的 `local_config.json`、`runtime/` 或 PG 数据。上游 AstrBot 仍固定在对应版本，不要顺手把 `vendor/AstrBot` 切到最新主分支。升级涉及结构时先保留自己需要的数据库与运行文件；回滚使用上一个兼容代码版本及自己的数据恢复方案，切代码不会自动撤销数据库迁移。

运行状态也可用 `manage_bot.py status` 查看；托盘“退出”会停止本项目 Bot，PostgreSQL 服务继续运行。重载插件可能短暂重连 QQ，不要重复扫码或启动多个后端。

## 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| 未找到数据库凭据 | 完成第 4 步，以运行 Bot 的同一 Windows 用户执行配置；不要直接复制别人的 DPAPI 文件 |
| password authentication failed | 用该应用账号和密码测试 `psql`，确认是正确服务/端口，再重新配置；不必重置所有数据库账号 |
| vector 类型不存在 / extension unavailable | 文件安装和 `CREATE EXTENSION` 是两步；确认启用的是 `companion_bot` 中 `public.vector`，且 DLL 对应 PG18 |
| 未找到 DeepSeek 凭据 / 401 | 检查用户环境变量或自己设置的文件；旧用户变量优先于临时进程变量，修改后重启 Bot |
| 模型或 Tokenizer 校验失败 | 文件未下载完整或上游包变化；保留错误、核对固定版本，不禁用校验继续跑 |
| API Connection error | Web/QQ 共用服务商与网络路由；先看真实日志和官方接口连通性，别反复重新绑定 QQ |
| 端口占用 | 检查已有 Bot 是否在线；只停止这个项目的实例或调整配置，不杀其他 QQ/PostgreSQL 进程 |
| 页面空白或资源下载失败 | 确认 AstrBot 固定版本、WebUI 资源下载与网络正常，重启后再查 `runtime/stdout.log` / `stderr.log` |
| QQ 在线但不回复 | 先检查主号绑定、急停和群权限；群聊还受扫描/参与判断控制 |
| 修改申请“已提交”但没生效 | 到“修改审批”看真实编号、检查状态与确认/自主模式；待审不等于生效，旧待审不会因开关自动批准 |

自动化开发测试使用独立的随机 PG schema。`python -m pytest -q` 需要已经可用的专用数据库；浏览器回归另需 Chrome/Playwright。真实聊天验收脚本会调用 API，可能操作自己的 QQ，请先阅读脚本内容，不要把所有 live_test 当作纯离线单元测试一键运行。

验证范围：已在维护者 Windows 环境通过受保护凭据读写、PG18 安装包只读校验、依赖检查及现有回归测试。文档不是“任意全新 Windows 裸机都已实测”的声明；部署遇到问题可附去除凭据与私人内容后的错误信息提交 Issue。
