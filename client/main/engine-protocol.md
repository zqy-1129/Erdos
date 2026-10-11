# 引擎拉起协议（SP3-1 客户端壳与 IPC）

本文档定义 Electron 主进程与 Python 引擎（`engine/`）之间的拉起与通信协议，
对齐 `contracts/engine-rpc.schema.json` 与 `client/shared/ipc.ts`。
**方法与事件清单以 contracts/engine-rpc.schema.json 为唯一权威**（v1：6 方法 + 3 事件；
CT-V2 增量后现行 10 方法 + 6 事件，待 10-10 冻结评审追认）。
修订记录：2026-10-07 引擎侧预走查（J-1024/W6 步骤②前置）——同步方法/事件清单、
补握手协商要求。

## 1. 拉起方式

开发态通过 `child_process.spawn` 拉起 Python 模块，工作目录为仓库根。Windows 默认
`engine/.venv/Scripts/python.exe`，其他平台默认 `engine/.venv/bin/python`；开发态可通过
`ERDOS_ENGINE_CMD` 覆盖解释器：

```
spawn("python", ["-m", "engine"], {
  stdio: ["pipe", "pipe", "pipe"],   // stdin/stdout/stderr
  env: { ...process.env, ERDOS_ENGINE_HOME: home },
})
```

> `ERDOS_ENGINE_MODE` 曾出现在本示例里，但引擎与主进程都不读它——已删除（2026-10-11 审计）。
> 开发态覆盖解释器的真实变量是 `ERDOS_ENGINE_CMD`，它由**主进程**读取
> （`client/main/index.ts:65` → `engineCommand()`），引擎侧不感知，因此不在 §8 登记。

正式安装包通过 `main/engine-launch.ts` 解析 `process.resourcesPath/engine` 中的已校验入口，
参数为 `[]`，工作目录为入口所在目录；不使用开发解释器或 `ERDOS_ENGINE_CMD`。
普通启动和打包预检共享同一完整性、版本与入口规则，缺资源时拒绝正式启动/打包。

### W17 资源交接要求（2026-10-10）

`client/resources/engine/` 应包含 PyInstaller onedir 完整产物、`version.txt` 和 `manifest.json`。
清单格式为 `{ version: 1, engineVersion: "<engine/pyproject.toml 版本>", entrypoint: "engine.exe", files: { "engine.exe": "<64位sha256>", "version.txt": "<64位sha256>", ... } }`。
所有运行依赖应登记到非空 `files`；路径为使用 `/` 的相对路径，拒绝父目录、绝对路径、
Windows ADS、反斜杠、空路径段、末尾点/空格以及符号链接。`version.txt` 必须有哈希，内容
与清单版本和客户端构建绑定版本一致。Windows 入口为清单内 `.exe`；只有一个根目录 `.exe`
的旧清单允许省略 `entrypoint`，多个入口或嵌套入口应显式指定。

`npm run pack` 强制资源存在。单独运行 `npm run verify:engine-version` 在开发期缺资源会
明确报告跳过；发布核验应运行 `npm run verify:engine-version -- --require-resources`。
哈希检查只读取清单内资源，时间复杂度 O(所列资源总字节数)，允许打包器额外产物。
清单用于完整性检查，不能替代发布签名、可信更新源和安装/升级实机验收。

## 2. 通信通道

| 方向 | 通道 | 协议 |
|---|---|---|
| 主进程 → 引擎 | stdin | JSON-RPC 2.0 请求（每行一个 JSON） |
| 引擎 → 主进程 | stdout | JSON-RPC 2.0 响应 + NDJSON 事件（按 `jsonrpc`/`event` 字段分流） |
| 引擎 → 主进程 | stderr | 引擎日志（不承载协议；诊断含拒启原因/沙箱镜像警告，经 secret-masker 落盘） |

- **编码基线（UTF-8 + LF）**：三条管道一律按 UTF-8 交换，NDJSON 行结束符为 LF。引擎在进程入口
  强制重配 stdio 编码（`engine/ipc/stdio.py`），主进程无需设置码页，也**不得**依赖
  `PYTHONIOENCODING`；Windows 控制台默认码页常为 cp936/GBK，若不强制，中文题面、门禁意见与
  工具摘要会被编成非 UTF-8 字节，主进程按 utf8 解码即得乱码并静默丢弃事件。
  **本条的机器判据是 `contracts/engine-rpc.schema.json` 的 `transport` 段（唯一权威）**：
  引擎侧守护见 `engine/tests/test_contract_alignment.py`（configure_stdio 参数逐字取自契约），
  客户端侧见 `client/tests/contract-transport.test.ts`（LF/UTF-8 写出与读入、单行上限取自契约）。
  改本节文字若不同步 `transport` 段，两侧守护会红。

## 3. Key 注入（SP1-5 红线）

- **一次性注入**：引擎拉起后，主进程将解密后的 API Key 经 stdin 写入一行，引擎内存持有；
- **首行分类约定**（EN-WIRE W2 落地）：引擎启动后先读 stdin 第一行——空行或 `ERDOS_NO_KEY` 进入无 Key 模式（FakeLLM，仅测试/演示）；首行为含 `method` 字段的合法 JSON-RPC 请求时按无 Key 模式运行并回放执行该行（兼容无密钥调用方）；其余首行视为 Key 注入（读后仅内存持有）；
- **Key 模式环境要求**：主进程须同时提供 env `ERDOS_MODEL_BASE_URL` 与 `ERDOS_MODEL_NAME`（引擎不猜测默认厂商端点，缺失以退出码 2 拒启）；
- **不落盘、不入日志**：Key 仅经 stdin 注入，引擎 `KeyStore.inject_from_stdin()` 读取；
- **脱敏**：日志/遥测中 Key 均脱敏（`sk-...尾4位`）。

## 4. 生命周期管理

| 事件 | 主进程行为 |
|---|---|
| 启动 | 懒启动（冷启动不拉起引擎，首次任务才 spawn） |
| 崩溃（exit code 非 0） | 看门狗重启引擎（最多 3 次），重启后 `get_status` 恢复正常 |
| 优雅退出 | 发 SIGTERM，引擎 300ms 内落盘检查点标记后退出 |
| 单实例锁 | 主进程持有单实例锁，重复启动聚焦已有窗口 |

## 5. 握手协商（CT-V2-2 / W14）

v2 客户端在拉起后、首个任务前必须调用 `initialize`：

```
{"jsonrpc":"2.0","id":"ui-0","method":"initialize",
 "params":{"client_protocol_version":2}}
```

- 返回 `protocol_version` / `engine_version` / `compatible` / `capabilities`
  （`tool_mode`、`isolation_mode` 如实上报，DEC-006）；
- 版本不匹配：引擎置协商失败态，后续 `start_stage` 以 SCHEMA_UNSUPPORTED 拒发
  （客户端升级后再试）；未握手（不传 initialize）引擎按兼容模式放行；
- 断线重连恢复：`events_replay(after_seq)` 补发缓冲的终态/评审事件；
  `model.delta` **不补发**（W15：token 流允许丢帧，终态以 stage.progress/artifact 为准）。

## 6. 事件转发（现行 6 类，随契约增删）

引擎 stdout 的 NDJSON 事件全集见 schema `events`：
`stage.progress` / `artifact.ready` / `gate.failed` / `tool.call` / `tool.result` / `model.delta`。
主进程按 `engine:event` 通道转发给渲染层；转发前按白名单校验事件名，
未知事件拒绝并计数（EC-N4）。

## 7. 与引擎 IPC 的对应（现行 10 方法）

| 客户端通道（ipc.ts） | 引擎 RPC 方法 |
|---|---|
| `engine:initialize` | `initialize`（§5 握手） |
| `engine:task_create` | `task_create`（题面登记；CT-V2 P2，待冻结追认） |
| `engine:start_stage` | `start_stage` |
| `engine:pause` | `pause` |
| `engine:resume` | `resume` |
| `engine:cancel` | `cancel` |
| `engine:get_status` | `get_status` |
| `engine:answer_gate` | `answer_gate` |
| `engine:provider_test` | `provider_test`（EN-CAP 端点探测；Key 用引擎已注入的） |
| `engine:events_replay` | `events_replay`（断线重连事件补发；§5） |
| `engine:event` | 6 类 NDJSON 事件转发（§6） |

## 8. 引擎环境变量（2026-10-11 补：编排后端回退开关与 env 消毒）

主进程按 §1 的方式把 `process.env` 原样交给引擎子进程，因此下列变量在**用户环境里设置即生效**，
不需要改客户端代码、也不需要发版。清单与 `docs/acceptance/engine-facts.json` 的 `env_vars`
分区同源（该分区由 `scripts/engine_fact_sheet.py` 从代码机器抽取，含常量间接读取）。

| 变量 | 作用 | 谁负责 |
|---|---|---|
| `ERDOS_ENGINE_HOME` | 任务/库数据根目录（缺失以退出码 2 拒启） | 主进程必给（§1） |
| `ERDOS_MODEL_BASE_URL` / `ERDOS_MODEL_NAME` | Key 模式的模型坐标（禁猜默认端点，缺失退出码 2） | 主进程必给（§3） |
| `ERDOS_MODEL_PROVIDER` | 厂商族（离线能力矩阵键，缺省 `openai-compat`）；矩阵键单源 `engine/adapters/capabilities.py:KNOWN_PROVIDERS`，未知厂商保守降级 `stage_level` | 主进程给 |
| `ERDOS_TOOL_MODE` | 强制求解路径 `tool_loop` / `stage_level`；**非法取值以退出码 2 拒启**（§8.1） | 排障用，产品路径不设 |
| `ERDOS_SANDBOX_PYTHON` | 沙箱执行体解释器（解析顺序 `ERDOS_SANDBOX_PYTHON` → `sys.executable` → PATH 探测） | 打包/CI 显式给；Windows 上 PATH 的 `python` 可能是商店占位 stub |
| `ERDOS_SANDBOX_IMAGE` | Docker 沙箱镜像名 | 仅 Docker 模式 |
| `ERDOS_SANDBOX_REQUIRE_DOCKER` | `=1` 时强制 Docker，探针未过即 `SandboxUnavailableError`（DEC-006）；**客户端目前从不设置**，因此 Windows 无 Docker 时静默降级 subprocess | 正式构建/CI |
| `ERDOS_ORCH_IMPL` = `native`（默认）/ `langgraph` | **编排后端回退开关**：引擎四阶段编排与求解内循环用自研内核还是 LangGraph。灰度期发现编排侧回归时设 `langgraph` 即时回滚（秒级、不发版）；两套后端读写同一张 `checkpoints` 表，回滚不需要数据迁移（兼容性由 `engine/tests/test_persistence_compat.py` 守护）。LangGraph 在 SP5-3 摘除依赖后该变量作废 | 引擎；客户端不主动设置 |
| `LANGCHAIN_*` / `LANGSMITH_*` | **引擎在包导入期无条件剥离**。原因：宿主若设过 `LANGCHAIN_TRACING_V2=true`，LangSmith 会把每次图执行的输入/输出（含题面与论文正文）POST 到外部端点，与「题面不出本机」(DEC-010) 和 BYOK 不中转直接冲突。主进程**不要**为了"传参"往这两个命名空间写任何东西 | 引擎 |

### 8.1 三条与变量有关的行为口径（不写清就会被读成"设了就生效"）

- **`ERDOS_TOOL_MODE` 严格校验**（W39/G30 起）：只接受 `tool_loop` / `stage_level`，大小写与首尾
  空白不敏感；其余取值**退出码 2 拒启**，stderr 给出合法值清单与"取消该变量即回到按能力探测"的
  出路。选择拒启而非静默回落：该值原样进 `initialize.capabilities.tool_mode`，而契约把它约束成
  二选一枚举——静默回落会让拼错表现为"设了但行为不对"，客户端还会收到契约外的枚举值。
- **默认装配已可达工具循环**（W39/G31 起）：`openai-compat` 已进离线能力矩阵
  （`engine/adapters/capabilities.py:FIXTURE_MATRIX`），`ERDOS_MODEL_PROVIDER` 缺省即命中 →
  真实 Key 冷启动默认 `tool_loop`，`solve_loop` 与 `tool.call` 留痕在主路径可达。矩阵行**不含
  `structured_output`**：那是凭接口名假定兼容（开发文档 §8.2 红线）。降级路径仍可达，
  `ERDOS_TOOL_MODE=stage_level` 显式进入；两条都有进程级用例
  （`engine/tests/test_rpc_flow.py::test_real_key_channel_full_chain_with_mock_vendor` /
  `::test_real_key_channel_stage_level_degradation`）。矩阵 `probe_source=fixture`，真实端点是否
  真支持 tools 仍需 `provider_test` 实测校准（W33）。
- **流式请求现在向厂商索取 usage**：引擎在 `stream=true` 时带 `stream_options={"include_usage":
  true}`（W8 契约的请求侧，此前只实现了消费侧，真实厂商流式恒走本地估算）。这是**出站请求体**
  变化，不属本进程间协议，但对厂商可见：不接受未知字段的严格网关会报 400，属 W33 真厂商通道待
  验证项。估算标记 `estimated` 目前不出留痕与事件面（登记为 G35）。

*修订记录追加：2026-10-11 编排内核自研替换批次——新增 §8（回退开关 + env 剥离）、§8.1；
删除 §1 示例中的幻象变量 `ERDOS_ENGINE_MODE`；修复 §7 表尾行被 §8 顶出的排版错误。*

*修订记录追加：2026-10-11 W39 · EN-PROVIDER-ALIGN（G30/G31）——§8 两行按实况改写（provider 矩阵
同源、`ERDOS_TOOL_MODE` 非法值拒启），§8.1 由两条扩为三条，新增流式 usage 索取口径。*
