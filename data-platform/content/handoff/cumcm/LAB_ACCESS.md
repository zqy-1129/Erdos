# 实验室国赛内容 API（2026-10-09）

统一入口为 `http://172.27.50.249:18789`。API 已在数据机运行，Windows 防火墙已只允许该本机地址、TCP 18789 和 LocalSubnet；用户已在另一台实验室电脑验证 `/health` 返回 `status: up`。

数据库 PostgreSQL 16 + pgvector 和 S3 文件库继续留在数据机。同事无需下载历史资料全库、模型或数据库。检索接口返回当前小问、当前写作阶段需要的参考内容；只有真正用于排版的模板或图片才按需获取。

接入需要 stdlib SDK `client_sdk/online_sdk.py` 和单独提供的 team_token。不要把包含数据库/S3 管理密码的 services.json 给其他客户端，不要将凭据提交 Git 或硬编码进 exe。

不需要每次向数据机负责人请求批准：在可访问的本地子网内，持有有效 team_token 的客户端可以直接调用。未配置凭据时只能检查 `/health`；受保护接口返回 401。一般 consumer 凭据不能自行升级为 team_internal。只开放 LocalSubnet 不等于跨 VLAN 的所有电脑必然能访问，异机开发者应先检查连通，再完成鉴权调用。

```python
import os
from online_sdk import ContentClient

client = ContentClient(
    os.environ['ERDOS_CONTENT_API_TOKEN'],
    'http://172.27.50.249:18789',
    allow_lan=True,
)
bootstrap = client.bootstrap('cumcm-2010-2025-codex-v3')
capsule = client.retrieve({
    'stage': 'model',
    'subproblem': {'goal': '建立优化调度模型', 'problem_types': ['optimization']},
    'usage_purpose': 'team_internal',
    'top_k': 5,
    'token_budget': 6000,
})
```

首次 bootstrap 固定 release_id 和 manifest_sha256；后续调用不混用版本。资料检索和组件查询的真实鉴权已在数据机通过，异机健康检查已由用户确认。其他开发电脑仍应执行一次带 team_token 的 bootstrap 和 retrieve，完成各自接入验收。

长期服务使用主项目源码与工程外 CPU 运行目录 `D:/Erdos_LocalAI/content-api-runtime`，MiniLM 继续使用原有固定模型。配置入口为私有 `.runtime/local/lab-launch.json`；进程记录为 `.runtime/local/lab-api-process.json`。

数据机需要启动服务时，在仓库根目录运行：

```powershell
powershell -File data-platform/content/scripts/start_cumcm_lab_api.ps1 -Address 172.27.50.249
```

IP 改变后需同步客户端地址和防火墙规则。客户端 Agent 负责真实计算、逐节写作与排版；此数据接口不代表产品 exe 已自动完成集成。

## 代码交付与验收范围

国赛本地库与接口已可用于实验室联调。完整私有回归 130 项通过；从 Git 导出的干净副本 78 项独立回归通过，52 项私有集成测试明确分开；CPU 与真实封存 GPU 向量比对通过。

本次合并先拉取最新远端 develop，再加入国赛交付；远端客户端、服务端、引擎和契约目录保持原样。新增内容限于数据工程代码、契约、SQL、测试、对接说明及相关 CI。Trilium 笔记、临时工具、数据库、全库资料、模型、备份和真实凭据不进入 Git。

同事拉取 develop 后，按本页配置 SDK 与团队凭据即可调用统一数据节点；不要求每个人部署 PostgreSQL、S3 或下载模型。独立测试和数据机换机/恢复步骤见 content/README.md。

历史资料质量标记仍按原验收口径保留；未核验的公式/图表语义和独立相关性评测不冒充 verified。客户端实际产品流程的接入由对应开发者完成。
