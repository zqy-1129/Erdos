# 引擎拉起协议（SP3-1 客户端壳与 IPC）

本文档定义 Electron 主进程与 Python 引擎（`engine/`）之间的拉起与通信协议，
对齐 `contracts/engine-rpc.schema.json` 与 `client/shared/ipc.ts`。
**方法与事件清单以 contracts/engine-rpc.schema.json 为唯一权威**（v1：6 方法 + 3 事件；
CT-V2 增量后现行 10 方法 + 6 事件，待 10-10 冻结评审追认）。
修订记录：2026-10-07 引擎侧预走查（J-1024/W6 步骤②前置）——同步方法/事件清单、
补握手协商要求。

## 1. 拉起方式

主进程通过 `child_process.spawn` 拉起引擎子进程：

```
spawn("python", ["-m", "engine"], {
  stdio: ["pipe", "pipe", "pipe"],   // stdin/stdout/stderr
  env: { ...process.env, ERDOS_ENGINE_MODE: "subprocess" },
})
```

## 2. 通信通道

| 方向 | 通道 | 协议 |
|---|---|---|
| 主进程 → 引擎 | stdin | JSON-RPC 2.0 请求（每行一个 JSON） |
| 引擎 → 主进程 | stdout | JSON-RPC 2.0 响应 + NDJSON 事件（按 `jsonrpc`/`event` 字段分流） |
| 引擎 → 主进程 | stderr | 引擎日志（不承载协议；诊断含拒启原因/沙箱镜像警告，经 secret-masker 落盘） |

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
