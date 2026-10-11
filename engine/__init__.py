"""Erdos Agent 引擎（P1，SP1-1 运行时与 IPC 骨架）。

职责边界（SP1-1 仅通信骨架，禁止阶段业务逻辑）：
- 可被主进程拉起/重启/优雅退出的 Python 子进程；
- stdio JSON-RPC 2.0 server，6 个方法全部注册；
- NDJSON 事件发射器（traceID + 时序 + schema 校验）。
"""

from engine._runtime_env import sanitize_framework_env

__version__ = "0.1.0"

# 必须先于任何 engine 子模块导入（lang* 依赖的 env 判定带 lru_cache，见 _runtime_env）。
sanitize_framework_env()
