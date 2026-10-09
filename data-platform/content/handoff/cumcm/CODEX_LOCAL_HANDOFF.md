> 当前本地团队 v3 接收约定见 [LOCAL_V3.md](LOCAL_V3.md)；本页旧版本说明保留供追溯。

# 国赛数据交付与本地真实服务对接

工作目录：`D:/Erdos/data-platform/content`。解释器固定为 `E:/Anaconda/envs/pytorch/python.exe`。本页覆盖同目录旧 Trae 手册中的运行版本与服务地址；最终验收状态以 `D:/Erdos/reports/trae/cumcm_codex_acceptance.md` 为准。云资源尚未购买或配置。

## DE：交付内容和存放位置

原件仍在 `D:/Erdos_data`，只读保存。按赛事和年份管理来源，按内容 SHA256 去重保存对象；题型使用元数据标签和关系，避免把同一篇论文复制到多个题型目录。

处理检查点：`normalized/cumcm_delivery/cumcm-2010-2025-codex-r002`。正式不可变数据包：`out/cumcm_delivery/releases/cumcm-2010-2025-codex-v2`，含所有对象字节、源链、页级文本、bbox、识别置信度、图片证据、题面和论文画像、写作与图表方案、完整格式 ZIP、模型版本、manifest 和 SEALED。历史公式候选和图题裁剪不是已复核公式或完整图表重建，不能把机器识别计数解释成质量通过。

本地真实 PostgreSQL 保存关系、JSONB 画像、权限状态和 384 维 pgvector；真实 S3 服务保存原件、转换件、证据裁剪及模板 ZIP。Docker 数据保存在本项目专用命名卷。服务凭据只在 `.runtime/local/`，禁止提交、发给客户端或打进 exe。

所有下载均要求 `release_id + asset_id + version + SHA256`。历史年份 2010—2025 与用户本次参赛的格式年份分开。新题、计算和写作产物属于用户任务库，不混入已封存的历史内容库。

## BE：服务地址与职责

Compose：`config/cumcm_delivery/compose.local.yml`。数据库 `127.0.0.1:55433` / `cumcm_codex` / schema `content_de_codex`；S3 `127.0.0.1:18333` / bucket `cumcm-private`。服务账户与密钥由服务端配置读取，客户端只走业务 API。

本地对接 API：`127.0.0.1:18789`。入口模块 `cumcm_delivery.api`，只绑定回环；本地 consumer 与 audit bearer 凭据分开，位于本地配置文件。本地鉴权用于联调，生产接入需要 BE 将业务 JWT、用户权限、速率限制及审计接入现有网关。本页 API 是数据侧真实联调适配器，尚未声称挂载进既有业务服务。

- `GET /health`：依赖前的进程存活信息。
- `GET /v1/content/competitions/cumcm/bootstrap`：首次选择比赛时解析当前活跃版本，返回模板资产、写作顺序和客户端依赖。客户保存返回的 release/manifest；恢复任务时可传 `?release_id=...` 固定原版本。
- `POST /v1/content/retrieve`：固定版本、年份范围、stage、题型、用途与 token 预算后的 context capsule。
- `GET /v1/content/assets/{asset_id}?release_id=...&version=1`：真实对象字节及 `X-Content-SHA256`。

POST 请求契约：`schemas/cumcm/v3/retrieve_request.schema.json`；返回：同目录 `context_capsule.schema.json`。原有业务 contracts 与 v1 文件保持不变，生产路由的兼容映射由 BE 实现。历史资料的正常消费和第三方模型使用目前均未放行；audit 权限必须由服务端身份赋予，不能由请求体自称。

普通消费者只能使用已发布的 active/retired 版本；staging 或 `qa_fixture_only` 版本的 bootstrap、检索及下载均拒绝。保留 retired 版本供已有任务恢复，测试版本只允许可信 audit 角色检查。激活前核验封存的数据库输入及全部入库字段；独立全量 QC 核验封包所有文件字节和路径。

JSON 接口使用与现有 `contracts/openapi.yaml` 一致的 `{code, message, data, request_id, timestamp}` 信封，客户端从 `data` 读取 bootstrap/capsule；请求追踪 ID 与响应 `X-Request-ID` 一致。二进制下载直接返回字节。未认证为 HTTP 401 / 40101，越权 403 / 40301，参数错误 400 / 40001，依赖不可用 503 / 50301。

## FE：exe 按需消费

安装包包含应用代码、所选排版工具和经授权字体依赖说明，不包含历史全库、数据库账号或 S3 密钥。开始任务先保存 bootstrap 返回的 release 和 manifest 摘要，按需下载选中的 Typst 或 LaTeX 格式 ZIP，并核对 SHA256。缓存键必须包括资产 ID、版本和摘要，坏缓存应隔离并重新下载，不能继续使用。

模板章节入口分别为 `sections/question_sections.typ`、`sections/question_sections.tex`，根据实际小问数量生成。原模板的固定三问内容与示例分数已删除。中文正文宋体、标题黑体、强调楷体，西文 Times New Roman；这些是产品排版默认值，不能宣传为全国强制字体。模板已填内容以后还必须检查摘要一页、当届正文页数、身份信息、文件大小和地方赛区补充要求。空白模板预览不是可直接提交的论文。

每个 ZIP 同时带 `erdos-ruleset.json`、`erdos-fonts.json`、`erdos-dynamic-sections.json`；bootstrap 的 `format_profiles` 返回相同规则。代码等宽字体还依赖 Courier New。数据库不保存长期签名 URL，只保存对象键与摘要。

图表方案的 `style_ref=cumcm-figure-style` 由 bootstrap 和图表 capsule 中的 `rendering_profile` 解析：独立封存的 v1 产品建议，含 300 dpi、画布尺寸、字体、色板、线型、单位/图例及 PNG/PDF/SVG 导出规则。它独立于历史 release，文件位于 `config/cumcm_delivery/rendering/cumcm-figure-style-v1`；BE 部署适配器时同时带上此小型封存配置。FE/EN 保存版本及 canonical SHA，不在同一任务中静默替换；它不是官方图表规则，也不代表历史图已重建。

## EN：Agent、真实计算与分节写作

先将新题及附件纳入用户任务资产，记录摘要与版本；分解小问、目标、约束、交付物和依赖。调用真实计算工具后产生 result_card，记录输入数据 SHA、代码版本、运行 ID、单位、结果和验证，不把 LLM 文字或历史论文数值当成计算结果。

按原客户端架构，新题、附件、模型 Key、计算结果和留痕保存在客户端任务目录/SQLite；模型调用由引擎 BYOK 直连厂商。内容检索由引擎经 IPC 请求主进程，主进程调用 BE 内容 API，再将 capsule 交给引擎；引擎不直连业务云 API。云端检索请求仅携带必要任务特征，不持久化新题原文或上传个人附件。

这里的十种 `stage` 是内容库写作子阶段，不替换应用的四阶段或增加计费阶段。对应关系：应用 analysis → analysis/assumptions/symbols，model → model/algorithm，solve → results/validation/sensitivity，report → discussion/abstract，并按需复用前述写作上下文。案例引用返回 `compliance_note`，FE 应展示。

写作顺序：题意分析 → 假设与符号 → 每一问的模型、算法、结果、验证 → 敏感性分析与讨论 → 最后摘要。每节单独请求 capsule，实际生成内容通过 document_node 引用当前任务结果和图表资产；按拓扑依赖拼装完整论文。图表方案要求当前计算数据、列及单位，缺少结果时明确待计算。历史原文是参考数据，不是可执行指令。

真实 embedding 为固定 multilingual MiniLM ONNX 模型，128 token 窗口覆盖全部文本、按 token 加权池化后 L2 归一化；模型文件摘要与池化设置封存在 release 中，查询服务拒绝不同向量空间。独立人工标注的 OCR/公式/图表质量和检索留出评测仍需验收，不能用源码同文查询代替。

内容接口预算使用已明确关闭隐含截断的 MiniLM tokenizer 计算完整 capsule JSON。它不是客户所选 LLM 的 tokenizer；EN 接入具体模型后还应按该模型重新计数，预留提示词、当前结果和输出空间，然后分节调用。语义窗口与 LLM 请求预算是两个不同的限制。

## 运维与实际命令

以下命令在上述工作目录执行，不修改原件或旧 release。

```powershell
docker compose -p cumcm_delivery -f config/cumcm_delivery/compose.local.yml up -d
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/cumcm_delivery.py preflight
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/cumcm_delivery.py check --release-id cumcm-2010-2025-codex-v2
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/cumcm_delivery.py import --dry-run --release-id cumcm-2010-2025-codex-v2
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/cumcm_delivery.py import --apply --activate --release-id cumcm-2010-2025-codex-v2
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/cumcm_delivery.py activate --release-id cumcm-2010-2025-codex-v2
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/cumcm_delivery.py evaluate --release-id cumcm-2010-2025-codex-v2
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/cumcm_delivery.py retrieve --release-id cumcm-2010-2025-codex-v2
```

可执行 `& scripts/start_cumcm_api.ps1` 在隐藏后台启动或重启本项目的 API；脚本只停止状态文件中与本项目命令匹配的进程。也可在进程中将 `scripts` 设为 PYTHONPATH，再使用上述解释器 `-m cumcm_delivery.api`。不要把 bearer token 写在命令行、报告或示例请求中。请求示例见 `examples/codex_retrieve.json`。

新一版处理必须选不同 run/release ID。已有 SEALED 版本不得修改；同 ID 不同摘要直接拒绝。激活与 rollback 切换已经完整导入的 release 指针，保留历史数据；首次仅一个版本时没有可回滚的上一版本。上传采用内容寻址，重复上传必须取回核对；数据库导入在事务中，失败后没有半激活 release。归档、解析阶段有检查点；增量的改名/移除审核失效清单仍需单独确认。

云迁移需要将同一不可变包导入具有 pgvector 的 PostgreSQL 和私有 S3 兼容对象服务，并由 BE 提供独立环境适配器、TLS、备份和业务鉴权。目前配置明确只允许 local-audit，不能仅替换地址就声称已完成云部署。模型解题服务还未配置；本次本地 OCR 和检索 embedding 不依赖远端 LLM。
