# QQ 接入（2026-11-01）

范围：本机 QQ 小号私聊，与已有 Web 共用 L0、已批准 L1 和 PostgreSQL 记忆；扫码由用户在手机完成。验收为独立客户端可生成与刷新真实二维码、OneBot 本机鉴权和消息门禁正常、账号显式绑定后共用同一身份、随 Bot 启停。未扫码前不声称真实 QQ 收发已经验收。

## 使用

打开 <http://127.0.0.1:6185/companion/qq>，或运行项目中的 `打开QQ登录.ps1`。Bot 设置也新增了“QQ 连接”页。

1. 用要交给 Bot 的小号，在手机 QQ 扫码并确认。
2. 用日常聊天的主号加 Bot 小号为好友。
3. 将本机扫码页显示的 `/绑定 …` 整行私聊发给 Bot。成功后仅该主号私聊进入陪伴模型，QQ 与 Web 共用同一份记忆与习惯。

这个步骤用于证明哪个 QQ 用户对应本机 Web 的 Owner，不根据昵称或收到消息的先后顺序猜身份。口令随机生成、绑定后失效，不写入聊天记忆。绑定前的 QQ 私聊、群聊和其他账号被早期门禁拦截，不调用模型；群聊不在本次范围内。人设修改审批仍在本机设置中完成，QQ 中“同意”不会批准修改。

## 安装和运行

使用 [NapCatQQ v4.18.42 官方发布](https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.42) 的 `NapCat.Shell.Windows.Node.zip`，发布时间 2026-10-30。包内 Node、QQ 原生组件与 NapCat 放在 `runtime/qq/napcat`，无需修改桌面 QQ；实际自带 QQ 组件版本 9.9.31-49738，NapCat 4.18.42。SHA256：

`77397ac38d66f46383e48f08f35d5f9d37ec575c22069f62d6c600e73b4521ed`

`install_qq.py` 固定来源和校验值，检查解压路径；重复执行保留已安装文件和登录数据。上游包仅在本机，不上传 GitHub。

- QQ 数据：`runtime/qq/data`，通过官方 `NAPCAT_QQ_DATA_DIR` 指定，独立于桌面 QQ。
- 本版实际运行配置：`runtime/qq/data/NapCat/config`。只写包内的 `napcat/config` 会被全局迁移路径绕过，安装脚本已针对本版处理。
- 反向 WebSocket：`ws://127.0.0.1:6199/ws`，随机 Token。
- NapCat 本地管理 API：`127.0.0.1:6099`，独立随机 Token；凭据在服务端使用，不透传到扫码页面。
- 本机扫码页：6185 端口，复用已有的 loopback、Host、Origin 限制。
- 配置与绑定信息：`runtime/qq/settings.json`；平台账号映射存 PostgreSQL `platform_accounts`，绑定主号映射到 `webchat:owner`。

启动本项目 EXE 时，AstrBot 插件启动并托管独立 QQ Node 客户端。正常退出 Bot 时，先取消轮询再结束该独立进程树；数据库服务与桌面 QQ 继续运行。客户端进程启动失败最多尝试三次，之后需要重启 Bot；API 临时不可用会保留错误状态并按五秒间隔检查。Windows 快速重启实测曾发生端口 EACCES：如果进程存活但 API 连续二十秒仍不可用，会只结束本项目客户端，等待十秒后重试，仍受三次启动上限限制。

后台约每五秒检查登录状态；未登录二维码约九十秒请求更新，扫码确认及初始化阶段不会主动刷新。WebUI 管理凭据不到一小时就重新获取，因此网页可以留到早上。首次确认在线后保存该小号供下次快速登录；腾讯要求重新扫码时，页面继续显示新的二维码。

## 验证和边界

- 73 项自动化测试通过，覆盖平台配置幂等、跨入口 PG 身份与历史共享、口令绑定、只允许主号、初始化前门禁、失败关闭、二维码刷新/恢复、确认阶段不打断、凭据续期、启动次数限制和进程范围。
- 本机真实 OneBot WebSocket 测试：错误 Token 被拒绝，正确 Token 可连接；模拟未绑定私聊与群聊均未写陪伴聊天、未增加模型请求、未发出 QQ 消息。`qq_transport_test.py` 只连接本机协议接口，不发送第三方消息。
- 浏览器验证真实 NapCat 二维码、手动刷新、本机扫码页与设置页 QQ 标签；结果及截图保存在 `runtime/qa`，不上传。
- 实际观察到后台在 110 秒观测窗内自动更换二维码，无需浏览器点击；证据为 `runtime/qa/qq-auto-refresh-evidence.json`。管理凭据跨小时续期由模拟服务测试验证，未实际等待一小时。
- 实际 EXE 退出测试已确认后端和独立 QQ 都结束，原桌面 QQ 与 PostgreSQL 继续在线；重新启动后恢复扫码状态。QQ 管理端与 OneBot 均只绑定 127.0.0.1。
- 真实 Web 聊天、读网页工具和次轮承接已回归，测试会话已经移除。QQ 门禁适配保持上游 WakingCheckStage 的协程形式，避免影响 Web 管道。
- 尚待用户手机扫码后验证真实 QQ 账号上线及主号私聊往返；本地模拟测试不替代腾讯侧实际消息验收。

不持续运行 Codex 来等待扫码。二维码与登录轮询由 Bot 本地后台维持；只要电脑、Bot 和网络在线即可。Windows 睡眠后扫码服务也会暂停。

## 回退

停止 Bot，将 `runtime/qq/settings.json` 中 `enabled` 设为 false 后重新启动，`companion_qq` 适配器会禁用，Web 聊天和已有 PG 数据继续使用。代码可回退 Git 提交；此项安装没有创建数据库备份，也不清空已有聊天。

依据：[AstrBot OneBot 接入](https://docs.astrbot.app/platform/aiocqhttp.html)、[NapCat 安装](https://napneko.github.io/guide/boot/Shell)、本次固定版本源码中的 data-path、WebUI 登录/续期和 OneBot 配置实现。未来升级需重新核实路径和 API。
