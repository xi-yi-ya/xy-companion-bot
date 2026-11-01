# Bot 自主管理 QQ：核查与后续方案

2026-11-01。本文是实现方案，未安装下列插件、未修改 QQ 头像/昵称/签名或收藏表情。

## 已核查能力

现有 AstrBot + NapCat 可以把 QQ 操作封装成 LLM 工具，通过本地 OneBot 通道执行。NapCat 官方说明支持设置头像、在线状态、签名和获取收藏表情；本机安装包还包含 `add_custom_face` 的实际处理器。接口存在与真实账号成功执行分开验收，不以文档或处理器存在冒充换头像测试。

- [NapCat 官方接口](https://doc.napneko.icu/develop/api)：`set_qq_avatar`、`set_online_status`、`set_self_longnick`、`fetch_custom_face` 等。
- [QQ 资料工具插件](https://github.com/L1ke40oz/astrbot_plugin_llm_change_qqprofiles)：自然语言更换头像，模型调用昵称、签名及状态工具，各工具独立开关。
- [kai_presence](https://github.com/kumavulp/astrbot_plugin_kai_presence)：头像、签名、状态的简单 Function Tool 实现。
- [meme_magpie](https://github.com/Whereis-Alice/astrbot_plugin_meme_magpie)：收录、视觉标注、检索和发送表情包，带管理页面及可选审核。

“本地收录一张图供 Bot 发送”和“写进 QQ 客户端的收藏表情面板”是两个操作。建议先完成本地图库，再单独验收 `add_custom_face` 同步；图片文件存电脑，标签、来源、使用记录等元数据存 PG，不混入会衰减的情景记忆。

## 与本项目的结合

建议在现有插件增加一组明确的工具：搜索/收录/发送表情包、申请或设置头像、修改签名和状态。当前聊天工具只开放 `companion_` 前缀，不能认为安装其他插件就会自动让其工具进入模型；还需要处理权限、模型工具列表、PG 发送记录和消息引用/分段钩子的兼容。

每项 QQ 能力可在本地设置选择“自主执行／每次确认／关闭”，不要求每张表情都审批。头像等可先展示具体候选图与修改内容，再由用户决定是否长期开放自主执行。人格修改继续按已确定的人工审批要求，QQ 外观权限不代表人格编辑权限。

模型只选择动作与素材 ID，执行层核对当前 Bot 账号、目标、参数和权限，记录操作结果；群成员或网页文本不能改权限。执行失败不宣称成功；不把整个 NapCat 管理接口或电脑 Shell 直接交给模型。

验收应包含：仅改 Bot 小号、群成员不能越权、表情不强制每轮出现、发送后模型知道实际发送的图片、失败有真实回执、头像/昵称有具体预览、确认模式必须手动确认。上述是后续建议，不是已经开启的权限。
