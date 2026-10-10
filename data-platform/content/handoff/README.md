# 数据侧对接入口

当前包：`out/releases/content-candidate-codex-v3`，状态 `staging`。以下均以 `D:/Erdos/data-platform/content` 为工作目录。版本切换和下载由实际 CLI 演示；HTTP、数据库、IPC 和 LLM API 是待对端实现的接口建议。

| 接收方 | 交付材料 | 负责事项 |
|---|---|---|
| 服务端 BE | [BE.md](BE.md) | 审核入库、版本索引、鉴权下载、完整快照切换 |
| 客户端 FE | [FE.md](FE.md) | 选择赛事/格式、核对哈希、缓存、安全解包、状态展示 |
| Agent/引擎 EN | [EN.md](EN.md) | 新题小问标签、画像检索、动态章节、当前结果写作 |
| 负责人 | [ARCH.md](ARCH.md) | 当届规则、许可、接口冻结、价格档位、上线验收 |

契约有 17 个 JSON 文件（包含 _common.json）以及 README；schema_version 始终为 1，内容实体版本可以递增。`figure_style.version` 已修复为正整数，原先误限定为 1；其余 JSON schema 不变。

必读顺序：当前 [README](../README.md) → 对应角色文档 → 候选包 catalog/index/bundle → [独立验收报告](../../../reports/trae/continuous_codex_acceptance.md)。历史阶段报告保留，当前报告覆盖其未核实的完成声明。
