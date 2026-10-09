# 国赛团队 v3 对接入口

新拉取仓库的完整安装步骤见 [content/README.md](../../README.md)。当前版本 cumcm-2010-2025-codex-v3，公开仓库仅交付代码/SQL/契约/SDK；私有资料包、模型和凭据单独取得。

- [LOCAL_V3.md](LOCAL_V3.md)：完整本地团队对接、原始语义质量边界。
- [BE.md](BE.md)、[FE.md](FE.md)、[EN.md](EN.md)：接收方字段、接口与集成责任。
- [OpenAPI](content_api_v3.openapi.json)、[SDK](client_sdk/online_sdk.py)：现有 loopback 联调接口。
- [BACKUP_V3.md](BACKUP_V3.md)：已执行备份恢复的证据及范围。
- [CODEX_LOCAL_HANDOFF.md](CODEX_LOCAL_HANDOFF.md)：旧 v2 历史记录；新安装以 content/README.md 的 v3 步骤为准。

API 地址在你本机默认 http://127.0.0.1:18789；它不等于其他电脑可以访问你的数据库。产品远程访问须部署服务端网关并落实鉴权，客户端 exe 仅携带 SDK、格式描述和按需缓存。
