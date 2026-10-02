# 引擎拉起协议（SP3-1 客户端壳与 IPC）

本文档定义 Electron 主进程与 Python 引擎（`engine/`）之间的拉起与通信协议，
对齐 `contracts/engine-rpc.schema.json` 与 `client/shared/ipc.ts`。

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
| 引擎 → 主进程 | stderr | 引擎日志（不承载协议） |

## 3. Key 注入（SP1-5 红线）

- **一次性注入**：引擎拉起后，主进程将解密后的 API Key 经 stdin 写入一行，引擎内存持有；
- **不落盘、不入日志**：Key 仅经 stdin 注入，引擎 `KeyStore.inject_from_stdin()` 读取；
- **脱敏**：日志/遥测中 Key 均脱敏（`sk-...尾4位`）。

## 4. 生命周期管理

| 事件 | 主进程行为 |
|---|---|
| 启动 | 懒启动（冷启动不拉起引擎，首次任务才 spawn） |
| 崩溃（exit code 非 0） | 看门狗重启引擎（最多 3 次），重启后 `get_status` 恢复正常 |
| 优雅退出 | 发 SIGTERM，引擎 300ms 内落盘检查点标记后退出 |
| 单实例锁 | 主进程持有单实例锁，重复启动聚焦已有窗口 |

## 5. 事件转发

引擎 stdout 的 NDJSON 事件（`stage.progress`/`artifact.ready`/`gate.failed`），
主进程按 `engine:event` 通道转发给渲染层，UI 实时更新。

## 6. 与引擎 IPC 的对应

| 客户端通道（ipc.ts） | 引擎 RPC 方法 |
|---|---|
| `engine:start_stage` | `start_stage` |
| `engine:pause` | `pause` |
| `engine:resume` | `resume` |
| `engine:cancel` | `cancel` |
| `engine:get_status` | `get_status` |
| `engine:answer_gate` | `answer_gate` |
| `engine:event` | 3 个 NDJSON 事件转发 |
