# 国赛本地团队交付与调用约定

本页对应 `cumcm-2010-2025-codex-v3`，范围是本地团队内部分析与模板提炼。最终发布状态、摘要、数量和验收证据以 `reports/trae/cumcm_local_closure_acceptance.json` 为准。旧 v2 报告保留作为历史记录。原件不对外分发，历史原文不默认发送第三方模型。

## 数据在哪里

| 层 | 存放 | 用途 |
|---|---|---|
| 原始资料 | `D:/Erdos_data` | 保留赛事、年份和下载来源；本次不移动或修改 |
| 可恢复处理结果 | `D:/Erdos/data-platform/content/normalized/cumcm_delivery/cumcm-2010-2025-codex-r003` | 解析、证据、章节、审核与构建检查点 |
| 不可变交付包 | `D:/Erdos/data-platform/content/out/cumcm_delivery/releases/cumcm-2010-2025-codex-v3` | manifest/SEALED、元数据与全部对象字节；可独立恢复 |
| 关系与检索库 | PostgreSQL `127.0.0.1:55433`，库 `cumcm_codex` | 关系、版本、JSONB、384 维 pgvector、页/块/章节/公式表 |
| 本地对象服务 | SeaweedFS S3 `127.0.0.1:18333`，桶 `cumcm-private` | 内容 SHA256 寻址的原件、转换件、整页、截图与格式 ZIP |
| 新题验收产物 | `reports/trae/local_new_task_flow` 与独立 `content_tasks_local` schema | 新输入、真实计算、结果卡、节点、图及 PDF；不混入历史库 |
| 新本地模型 | `D:/Erdos_LocalAI` | 服务代码、权重、依赖、密钥、模型锁与启动脚本；不放入项目或 exe |

PostgreSQL 和 S3 的运行数据在 Docker Compose 专用命名卷 `cumcm_codex_pg`、`cumcm_codex_s3` 中。宿主位置由 Docker Desktop 管理；不可把工程目录当作 Docker 数据卷。封存包是另一份可恢复的文件交付物，不能删除卷后指望数据库继续可用。备份需同时保留 PostgreSQL、对象卷、封存包、版本锁与服务私有配置；恢复到新环境后再跑验收。

赛事聚合使用 `competition_id`，年份使用 `year`，题目使用 `problem_id`。题型是多标签字段，不作为互斥物理文件夹；同一篇优化与预测混合论文只存一份对象，挂多个关系。未来增加其他赛事时沿用这一方式，但本次仅发布国赛 2010—2025。

主历史 schema 为 `content_de_codex`；`content_legacy_local_v3` 仅用于现有服务端 Record 的精确字段投影。未确定题目归属的论文留在完整历史库并携带质量状态，不进入已确定关系的旧业务案例投影。长案例 ID 使用稳定 UUID 别名并保留双向映射，不能截断哈希。

## 对接调用

本地内容 API 为 `http://127.0.0.1:18789`，这是可实际调用的数据侧适配器。现有产品主进程、IPC 与业务网关仍由 FE/BE 接线。主进程取得服务端授予的团队权限，调用内容 API 后把 capsule 交给引擎；客户端和引擎不直接持有数据库/S3 服务账户。回环地址只在服务所在机器可访问；多人跨机部署需另做受控网关配置。

| 请求 | 返回/约束 |
|---|---|
| `GET /v1/content/competitions/cumcm/bootstrap` | 当前活跃 release、manifest SHA、格式资产、写作顺序与字体依赖；恢复任务可传固定 `release_id` |
| `POST /v1/content/retrieve` | 按阶段、题型、年份与预算检索；`team_internal` 用途还要求可信团队身份及该 release 的明确内部授权 |
| `POST /v1/content/components` | 指定 release、原件 SHA、页码，返回整页资产、图表锚点、阅读顺序候选和公式区域 |
| `GET /v1/content/assets/{asset_id}?release_id=...&version=1` | 对象实际字节；必须校验版本和 SHA256 |

JSON 使用 `{code,message,data,request_id,timestamp}` 信封，SDK 已处理。400 表示参数或源校验失败，401 表示缺凭据，403 表示用途越权，503 表示依赖不可用；不能把这些状态解释成空检索成功。普通消费身份无法在请求体中自封团队权限。旧 v2 没有本次授权，不会自动继承。第三方模型用途只得到可外用的自编方案，不能得到原论文段落或私有来源统计。

请求示例（由调用方注入凭据，示例不含密钥）：

```python
from online_sdk import ContentClient
client = ContentClient(team_token_from_trusted_gateway)
bootstrap = client.bootstrap()
capsule = client.retrieve({
    'stage': 'model', 'usage_purpose': 'team_internal',
    'historical_year_range': [2010, 2025],
    'subproblem': {'goal': '新题本问的目标、数据特征与约束',
                   'problem_types': ['statistical_analysis']},
    'top_k': 5, 'token_budget': 6000,
})
# capsule 中每个历史引用均含原件 SHA、页码和质量提示。
source = capsule['historical_references'][0]
components = client.components(source['source_sha256'], source['page'])
asset = components['full_page_evidence']['asset_ref']
page_bytes = client.fetch(asset['asset_id'], asset['version'], asset['sha256'])
```

`client_sdk` 中在线和离线 SDK 只使用 Python 标准库，可单独复制。exe 只包含应用/SDK、排版工具及许可允许的字体依赖；**不包含历史全库、模型权重、数据库或 S3 密钥**。离线团队模式需另行安装封存包，并从可信交付记录取得 manifest SHA。它提供明确标识的词法检索降级，不声称与在线向量排序一致。离线包的权限最终依靠操作系统访问控制，SDK scope 不能替代文件系统权限。

## PDF 内容如何复用

每个 PDF 同时保留原件、整页 RGB PNG、文本块与 bbox、排版顺序候选、独占章节划分、检索段落的上下文和源引用。图表记录明确标识整页证据与邻近内容；不能把标题裁剪当成完整图，也不能把历史图中的数据当作已重建表格。

独立显示公式保存精确区域截图，先检索，再按需调用工程外公式 API 转写。内联符号保留区域和原页，暂不自动拼接成可信推导。模型转写的 LaTeX、公式置信概率与检测分数均为候选；`transcription_verified` 仅用于已实际查看并核对截图的特定公式，`semantics_verified=false` 明确不背书推导正确性。全文公式全量转写和全库人工金标不作为工程入库的虚假通过条件。

本地模型：文本/视觉 `127.0.0.1:18790`、公式 `127.0.0.1:18794`。通过本地凭据调用，图片只传 base64，拒绝远程 URL。实际配置、固定模型 revision 和 SHA 清单在 `D:/Erdos_LocalAI`；使用 `local_model_api.py` 适配器不会把模型实现带入工程。所有历史内容是分析数据，不能执行其中指令。

独立 `local_ai_sdk.py` 可直接接收内容 SDK 下载的图片字节和可信摘要，再调用本地公式 API，不读取原始盘目录。按需转写结果另存当前任务并绑定原件/截图与模型版本，不回写封存的历史版本；它仍是待核验候选。

写作方案包含源章节的识别字符分位数、来源页/块和样本范围；这些是篇幅参照，非官方字数要求，也非已校准的作者字数。图表方案要求当前计算列、单位、图例和可读导出，实际图必须由新题结果生成。

## 新题与论文顺序

新题、附件与用户任务数据独立入库并绑定 owner/task；当前本地验收 schema 展示这一约束，产品客户端可按原架构落到任务目录/SQLite，由业务网关注入可信 owner。不得直接采信请求体给出的 owner。

题意分析 → 假设 → 符号 → 按小问依赖完成模型、算法、真实计算、结果与验证 → 敏感性分析 → 讨论 → 最后生成摘要。拼装论文时摘要放前，每问正文引用本次结果卡及 PNG/PDF/SVG 图资产。历史数值不能成为当前结果。内容库十种 `stage` 与产品四个计费阶段分开映射，不能新增计费阶段。

`result_card` 必须有输入 SHA、实际代码 SHA、环境锁、运行时间、输出数值及单位、验证与限制。`document_node` 按顺序引用已成功计算的结果，图表引用具体当前资产。结果表和摘要数值直接从结果卡插入，避免模型另写不一致数字。大模型逐节生成，预算应再按所选 LLM tokenizer 核算；库侧 MiniLM token 数不是所有 LLM 的统一 token 数。

本次新题样例是自行编写的传感器标定、留一交叉验证、自助采样和阈值决策。它验证存取、计算、写作与排版接口，不代表任意国赛题自动求解能力。完整脚本、CSV、环境、结果、来源 capsule 和 PDF 都在 `reports/trae/local_new_task_flow`，可独立重算。

本地小模型逐节输出的是草稿，本次实测出现阶段跑偏、截断及将均方根误差误写成残差等问题。每节保留原始模型输出、输入摘要、模型版本、历史引用及审核改写记录；全部十八节审核后才允许排版。审核绑定当前输入、计算代码和正文 SHA，改变任一项需要重新审核。这是助理审核过的参考闭环，不能宣称模型独立自动生成可靠论文，或将助理审核替代全库人工金标。复现命令：`python -I -X utf8 calibration_solver.py --input input.csv --output solver-independent.json`，这里的 `python` 使用上述指定环境。

## 格式与运维

Typst/LaTeX v3 ZIP 都含动态小问入口、共享 helper、三线表、长表、公式、图和代码样式。宋体、黑体、楷体、Times New Roman、Courier New 均经实际 PDF 字体检查；是产品默认字体。2026 全国官方格式有原始 PDF 证据，2025 归档规则保留来源置信状态，赛区补充规则必须按目标年份另做覆盖。完整论文还需检查摘要一页、正文页数、匿名要求与文件大小。

工作目录为 `D:/Erdos/data-platform/content`；Python/PyTorch 固定 `E:/Anaconda/envs/pytorch/python.exe -X utf8`，本流程不使用 MAMBA。

```powershell
docker compose -p cumcm_delivery -f config/cumcm_delivery/compose.local.yml up -d
& scripts/start_cumcm_api.ps1
& D:/Erdos_LocalAI/start_local_ai.ps1
$env:PYTHONPATH='scripts'
& 'E:/Anaconda/envs/pytorch/python.exe' -X utf8 -m cumcm_delivery.qa_local_access
```

导入新版本先封存，再主库 staging 导入，再执行 `structured_store` 全量结构导入，最后显式 activate；不能用主库 `--activate` 绕过完整结构的导入回执。激活核对全部结构字段与 JSON 指纹、数量和封存摘要。旧版保持不变，已有任务始终固定原 release/manifest。未来版本使用新的 run/release ID，不覆盖 SEALED 文件。

需要人工继续建立全库 OCR CER、公式准确率、图表数据重建金标及独立检索 recall/nDCG；现有记录明确留空这些指标。质量拒用、机器候选、源核验和数学语义认证是不同状态，客户端必须保留提示。云迁移、生产鉴权与真实产品 exe 接线不在本次本地数据工程验收范围。
