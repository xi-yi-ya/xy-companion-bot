# 开源、上游与数据范围

本项目基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 二次开发，增加陪伴记忆、人设更新与审批、本机 Web/托盘启动，以及 QQ 相关工具和权限管理。它不是 AstrBot 官方发行版，不表示上游对本项目背书。

## 软件许可

本项目的原始代码按 **AGPL-3.0-or-later** 发布，完整许可正文保留在 [LICENSE](../LICENSE)，归属及依赖见 [NOTICE](../NOTICE)。

核查的 AstrBot 基线为 **v4.29.4 / 61335414433db721b3f7ecc93f90deafe0a59d78**：

- 上游该提交的 [pyproject.toml](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/pyproject.toml) 明确标注 `AGPL-3.0-or-later`。
- 同一提交的 [LICENSE](https://github.com/AstrBotDevs/AstrBot/blob/61335414433db721b3f7ecc93f90deafe0a59d78/LICENSE) 是 GNU AGPL v3 正文。本轮在线下载的这两份文件与实际本机 checkout 一致。
- 上游源码由使用者单独获取，原版权、许可与其他上游通知保留。本项目通过插件和运行时适配集成，固定 checkout 保持原样；相关新增实现通过本仓库公开。

分发本项目或其修改版时保留许可和版权说明、标注修改并按 AGPL 提供相应源码。如果提供支持远程网络交互的修改版服务，还需要向实际交互用户显著提供获取相应源码的机会；仅把仓库设为 public 不代替运行服务里的源码入口。依据见 [AGPL 第 5、6、13 节](https://www.gnu.org/licenses/agpl-3.0.html) 与 [GNU 关于网络交互的 FAQ](https://www.gnu.org/licenses/gpl-faq.en.html#AGPLv3InteractingRemotely)。正式条款以 LICENSE 为准。

本项目设置页提供仓库/许可链接，私聊可以通过 `/源码`（别名 `/开源`、`/source`）获取源码地址。自托管修改版应把源码链接改为对应自己实际运行版本的仓库，保留上游归属。

## Bot 的“本人许可”

README 的“已获得 Bot 本人（阿深）的开源许可”是一句陪伴 Bot 项目的玩梗彩蛋，摘自维护者提供的聊天；Bot 回复不是法律主体的授权，也不改变 AGPL 或任何第三方许可证。无需提交你自己的聊天截图来使用这份代码。

## 数据与组件范围

| 内容 | 发布范围 |
| --- | --- |
| 本项目代码、默认人设模板、数据库结构、部署文档 | 本仓库公开 |
| 使用者的真实 L0/L1、聊天原文、PG 记忆与向量、共享经历 | 保存在使用者本机，不随源码发布 |
| QQ 空间读取/发布数据、附件、头像/表情素材、账号与登录状态 | 本地运行文件，不随源码发布 |
| API Key、数据库密码、QQ 令牌、私有环境配置、日志和数据库备份 | 不进入版本控制；示例配置不含真实凭据 |
| AstrBot、NapCat/QQ、Qt、模型、Tokenizer 及其他依赖 | 单独安装/下载，遵守各自许可；本仓库不打包再分发这些运行文件 |

特别是 NapCat/QQ 原生组件、第三方模型与 Qt 不因本仓库采用 AGPL 就自动变为同一许可证。当前 QQ 安装器下载的固定 NapCat 包有上游 Limited Redistribution License（含非商业限制），本项目不承诺 QQ 运行时可任意再分发或商用；部署与分发时分别阅读 [NOTICE](../NOTICE) 中的原始许可链接。不要把已经登录的整套 `runtime/` 上传作“开源安装包”。

开源的是可复用的系统和默认模板；每个人自己的聊天、记忆与人格属于自己的部署数据。它们不是提供源码所必需的内容。
