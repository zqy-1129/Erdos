# 国赛数据工程交付（2010—2025，本地团队 v3）

当前交付版本是 `cumcm-2010-2025-codex-v3`。数据库采用 PostgreSQL 16 + pgvector；文件使用本地 SeaweedFS S3；内容 API 负责检索、固定版本资源下载和原始页面/公式/图表证据查询。客户端使用 stdlib SDK 按需下载，无须把历史论文、模型或数据库装进 exe。

Git 中包含代码、SQL、契约、配置规范、SDK 和独立测试。历史资料包（115,960 个封存文件、约 10 GB）、数据库备份、对象字节、模型和实际凭据单独通过团队内部渠道交付。**只拉 Git 可以运行独立回归；检索真实历史论文还必须安装私有资料包和固定 embedding 模型。**

## 实验室同事：直接接入统一数据节点

你们共用交付机的本地库，服务端/客户端拉取 develop 后默认采用 API 模式：取得数据机的局域网地址、端口和团队 token，直接使用 handoff/cumcm/client_sdk/online_sdk.py。不用安装 Docker、PostgreSQL、S3、embedding，也不用复制 2010—2025 全库。SDK 只需要 Python 标准库，客户端引擎可使用自己的 Python 3.12 环境。

调用先 bootstrap 固定 release/manifest，再 retrieve 获取当前小问/写作阶段需要的参考。模板、图片和页面证据的字节只在客户端真实需要时 fetch，按 SHA256 缓存；不做全库离线下载。若只需分析内容，retrieve 返回的数据即可使用。完整安装章节仅供数据机运维、恢复或换机，不要求每位同事执行。

具体连接示例和防火墙规则见末尾“同实验室共用你的本地库”。

## 新拉取仓库：先运行独立回归

以下命令从本目录执行。Windows 用你的 Python 路径替换 `$py`，本机约定路径如下；其他 Windows 机器可用自身 Python 3.8 或 3.11。数据服务与 server/engine 的 Python 3.12 环境相互独立。

```powershell
$py = 'E:/Anaconda/envs/pytorch/python.exe'
& $py -X utf8 scripts/clone_setup.py install-deps --schemas-only
& $py -X utf8 -m unittest discover -s tests -p 'test_*.py' -v
```

离线环境或本机 Python TLS 异常时，可从可信渠道取得对应平台的固定依赖 wheel 集合，执行 `install-deps --wheelhouse <文件夹>`，无需关闭 TLS 校验。依赖安装到忽略目录，不改全局环境；已有非空依赖目录不会自动覆盖。部分旧阶段的集成回归需要旧 seed/快照，默认明确标为 skipped。拿到完整旧快照后设置 `$env:ERDOS_PRIVATE_CORPUS_TESTS='1'` 再运行；缺失资料会报错，不会用假数据冒充全库验收。新版公开回归使用自建正负例，不需要论文库。

## 安装本地真实服务

需要 Docker Compose v2、约 30 GB 可用空间（私有包、对象存储、PG 和安装暂存），以及团队提供的完整封存 v3 文件夹。封存文件夹带 `manifest.json` 和 `SEALED.json`，不能只拿 PDF 或数据库目录。安装时全部文件重算 SHA256，并与 [可信发布凭据](config/cumcm_delivery/release_v3_receipt.json) 比对。

1. 完整安装依赖：新环境执行 `scripts/clone_setup.py install-deps`。若已安装 schemas-only，先只把 `requirements-local-api.txt` 安装到 `.runtime/py38`：

```powershell
& $py -m pip install --target .runtime/py38 -r requirements-local-api.txt
& $py -X utf8 scripts/clone_setup.py init
docker compose --env-file .runtime/local/compose.env -p erdos-cumcm -f config/cumcm_delivery/compose.local.yml up -d
docker compose --env-file .runtime/local/compose.env -p erdos-cumcm -f config/cumcm_delivery/compose.local.yml ps
```

`init` 每台机器产生自己的私有数据库/S3/API 凭据；已有配置会拒绝覆盖。默认端口为 PG `55433`、S3 `18333`、API `18789`，全部只绑定 `127.0.0.1`。端口冲突时在 init 时指定 `--pg-port / --s3-port / --api-port`，Compose 必须使用生成的 compose.env。已有工作的原始部署无需再次 init。

2. 从团队内部渠道取得 **MiniLM embedding 的固定五个文件**，放在工程外，例如 `D:/Erdos_Models/cumcm-embedding`，设置：

```powershell
$env:ERDOS_EMBEDDING_DIR = 'D:/Erdos_Models/cumcm-embedding'
```

模型是 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`、revision `faf4aa4225822f3bc6376869cb1164e8e3feedd0`。五个文件为 config.json、model_optimized.onnx、special_tokens_map.json、tokenizer.json、tokenizer_config.json；哈希必须与封存包的 model_manifest.json 完全一致。不要随意下载同名不同版本替代。资料检索不需要 Qwen2-VL 或 Pix2Text；那两项是可选的本地图文辅助 API，详见 LOCAL_V3.md，模型候选输出仍需核验。

原库索引由 CUDA ONNX 1.19.2 构建，新安装默认显式用 ONNX 1.19.2 CPU 查询，使用同一模型字节、完整文本分窗、池化和归一化。CPU 查询不改写历史向量，也不改原索引运行环境声明；新索引构建仍须具备原声明的 GPU 环境或创建新的发布版本。

3. 安装私有包、真实入库并启动 API（路径替换成团队交付位置）：

```powershell
& $py -X utf8 scripts/clone_setup.py install-release 'D:/TeamDelivery/cumcm-2010-2025-codex-v3'
& $py -X utf8 scripts/clone_setup.py import-release
& $py -X utf8 scripts/clone_setup.py check
& $py -X utf8 scripts/clone_setup.py start-api
```

等待 Compose 两项服务可用后再导入。流程为封存验签 → S3 全字节写入与读回核对 → PG 结构/向量入库 → PDF 页/块/章节/图表/公式入库 → 外键与指纹检查 → 原子激活。耗时取决于本机磁盘；导入中断可重跑，staging 不会暴露给客户端。已经 active 的同版本只核对，不重写。新调用版本只需要 v3 包，不要求以前的 v2 构建目录。

API `/health` 的 up 仅表示进程存活，数据库就绪必须通过 check 和 bootstrap；不能把健康检查当成数据验收。保留旧包与备份后才切换版本。

## 服务端与客户端对接

接口定义见 [OpenAPI](handoff/cumcm/content_api_v3.openapi.json)，源码 SDK 见 [online_sdk.py](handoff/cumcm/client_sdk/online_sdk.py)。三项固定标识：competition_id、release_id、manifest_sha256；下载还必须指定 asset_id、version、sha256。第一次 bootstrap 固定版本，后续查询/下载不得混用其他版本。

```python
import json, sys
from pathlib import Path
sys.path.insert(0, 'handoff/cumcm/client_sdk')
from online_sdk import ContentClient
cfg = json.loads(Path('.runtime/local/services.json').read_text(encoding='utf-8'))
client = ContentClient(cfg['api']['team_token'], 'http://127.0.0.1:'+str(cfg['api']['port']))
boot = client.bootstrap('cumcm-2010-2025-codex-v3')
capsule = client.retrieve({'stage':'model', 'subproblem':{'goal':'建立优化调度模型','problem_types':['optimization']},
                           'usage_purpose':'team_internal','top_k':5,'token_budget':6000})
```

示例凭据仅供本地可信团队联调。产品登录/会员鉴权在服务端完成；历史资料对公网用户、第三方模型的授权仍关闭。客户端不应获得 PG/S3 管理密码，也不能靠请求字段自行授予 team_internal 权限。默认 API 绑定同机 loopback；实验室共享方式见下节，使用明确的私有网卡地址和团队 token。团队也可各自私有安装，后续由服务端部署统一受控网关。

新题和用户计算结果应进入任务库，与历史库分开；检索返回写作阶段/图型配方和可回溯证据，客户端 Agent 完成真实计算、逐节写作与排版。数据侧已有本地计算/图文成文参考链路及结果引用约束；产品中的上传、会员流程、Agent 全自动写作和 exe 打包仍由对端集成。详见 [本地 v3 对接手册](handoff/cumcm/LOCAL_V3.md)、[BE](handoff/cumcm/BE.md)、[FE](handoff/cumcm/FE.md)、[EN](handoff/cumcm/EN.md)。这些文档中的 D:/Erdos 路径是交付机示例。

## 已交付数据与验收边界

385 份唯一 PDF、9,713 页，71 个逻辑题目、183 个小问、261 篇论文，17,985 个检索单元和 94,190 个对象资源；章节、图表与公式均保留来源坐标、页图和版本指纹。已验证本地 PostgreSQL、S3、客户端接口、格式输出及隔离数据库恢复。

四类质量工作仍未全部达到人工 gold：部分论文与赛题关联、独立检索相关性评测、公式/图表完整语义核验、复杂任务自动成文质量。未经核验项维持 candidate/pending/abstained，不假报 verified。当前可交付团队数据/API 联调，不能据此宣称任意新题自动高质量成文已全面验收。原字体/赛事规范证据保留在包中；新届次/赛区要求还需选择并核验对应 format_profile，不能把历史格式当作所有未来比赛的统一规则。

本地备份脚本 `python -m cumcm_delivery.backup_local` 从 scripts 目录运行；新 Compose 的默认容器是 erdos-cumcm-postgres-1。如更改 Compose project 名称，应在私有 services.json 的 postgres_container 同步实际容器名。旧部署默认兼容 cumcm-codex-postgres。

## 同实验室共用你的本地库

推荐连接是：同事客户端/服务端 → 你电脑的局域网内容 API → 你本地 PostgreSQL + S3。其他电脑只拉代码和 SDK 就能接入，不需要重建全库，也不需要 MiniLM/GPU。数据库和 S3 继续绑定 loopback，管理员凭据留在数据机。

数据机执行（先确认该地址仍属于实验室有线网卡；不要使用 VPN/WSL 虚拟网卡地址）：

```powershell
& $py -X utf8 scripts/clone_setup.py start-api --lan-host 172.27.50.249
```

SDK 必须显式启用实验室模式：

```python
client = ContentClient(team_token, 'http://172.27.50.249:18789', allow_lan=True)
boot = client.bootstrap('cumcm-2010-2025-codex-v3')
```

地址是交付机 2026-10-09 的网卡示例，IP 变化后要更新客户端。API 只允许明确的 RFC1918 私有 IPv4 地址，SDK 禁止重定向、不走系统代理。team_token 通过团队内部渠道单独配置，不能提交 Git、写进产品 exe 或公开发送；audit_token 与 PG/S3 管理凭据不给一般客户端。局域网 HTTP 只用于可信实验室联调，跨网络或产品部署由服务端启用 HTTPS 与产品鉴权。

Windows 防火墙只为该地址/TCP 18789/LocalSubnet 添加专用规则（管理员 PowerShell），不要关闭整个防火墙：

```powershell
New-NetFirewallRule -DisplayName 'Erdos CUMCM lab API' -Direction Inbound -Action Allow -Protocol TCP -LocalAddress 172.27.50.249 -LocalPort 18789 -RemoteAddress LocalSubnet -Profile Any
```

连接验证应先访问 `/health`，再使用 team_token 做 bootstrap、检索、资源下载和 SHA256 核对。单有 health 成功不能证明有权限或数据就绪。数据机需要保持开机、Docker/API 运行，同事需要能路由到该私有地址；同一实验室 Wi-Fi 的客户端隔离或不同 VLAN 仍可能阻断，需要网络管理员排查。Git 交付不代表本机防火墙规则或异机网络已自动配置。

高级部署：代码与资料目录可分离；ERDOS_DATA_CONTENT_DIR 指向完整 content 资料根目录，ERDOS_RUNTIME_DIR 指向隔离依赖和私有配置目录。两个变量均为运维选项，同事的 SDK 无须设置。默认读取当前 checkout 下的 content 与 .runtime。
