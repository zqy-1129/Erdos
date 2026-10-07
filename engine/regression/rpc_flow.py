"""RPC 任务流驱动（SP1-7 真实通道）：经 JSON-RPC 子进程驱动真实引擎。

实现 TaskFlow 端口，让验收运行器能驱动 `python -m engine` 真实进程（而非进程内
编排器）——SP1-7 验收方案步骤 3（真实 Key 全量回归）与步骤 4（进程级 kill/恢复）
的执行载体；无 Key 时走 ERDOS_NO_KEY 假模型模式（系统级回归护栏，链路同真实通道）。

设计约定：
- 协议面：仅使用既有 RPC 方法（initialize/task_create/start_stage/answer_gate/
  get_status），不改契约（无需 CT-V2 登记）；
- 阶段定位：current_stage 经引擎检查点库（engine_home/checkpoints.db）解析，与
  StageOrchestrator.restore 同口径；阶段数据（usage/产物哈希）经留痕库
  （engine_home/audit.db）回读——两处均为驱动自建引擎 home 的本地只读访问，
  避免为回归工具扩展产品契约；
- 失败归因：阶段 failed 时按引擎 last_error 的异常类型重映射（Connection→adapter、
  Timeout/Sandbox→sandbox），运行器据此分层归因；
- 传输为阻塞式（独立进程，`asyncio.to_thread` 包装），回归工具串行驱动语义；
  产品客户端不使用本实现（客户端走主进程 EngineHost）。
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from engine.checkpoint.store import SQLiteCheckpointStore
from engine.orchestrator.graph import CHECKPOINT_EXECUTED, STAGES
from engine.trail.store import EventType, TrailStore

REPO_ROOT = Path(__file__).resolve().parents[2]
_STATUS_POLL_INTERVAL = 0.1
_STDERR_TAIL_LINES = 20


class RpcFlowError(RuntimeError):
    """RPC 任务流错误（传输/协议层）。"""


class _EngineTransport:
    """引擎子进程传输：首行注入 + NDJSON 读写（响应按 id 匹配，事件留存缓冲）。"""

    def __init__(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        first_line: str,
    ) -> None:
        self._proc = subprocess.Popen(  # noqa: S603 - 受控固定参数（回归工具）
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=str(cwd), env=env,
        )
        self._lines: list[str] = []
        self._cond = threading.Condition()
        self._eof = False
        self._stderr_tail: list[str] = []
        self._next_id = 0
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._drainer = threading.Thread(target=self._drain_stderr, daemon=True)
        self._reader.start()
        self._drainer.start()
        assert self._proc.stdin is not None
        self._proc.stdin.write(first_line + "\n")
        self._proc.stdin.flush()

    # ------------------------------------------------------------------
    def _read_stdout(self) -> None:
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            with self._cond:
                self._lines.append(raw.rstrip("\n"))
                self._cond.notify_all()
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        for raw in self._proc.stderr:
            self._stderr_tail.append(raw.rstrip("\n"))
            del self._stderr_tail[:-_STDERR_TAIL_LINES]

    def events(self) -> list[dict[str, Any]]:
        """已收到的事件流（event 字段；诊断/证据用，不影响响应匹配）。"""
        with self._cond:
            return [
                msg for msg in (self._parse(line) for line in self._lines)
                if isinstance(msg, dict) and "event" in msg
            ]

    @staticmethod
    def _parse(line: str) -> Any:
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------
    def rpc(self, method: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        """同步 JSON-RPC 调用；错误对象/EOF/超时均抛 RpcFlowError。"""
        self._next_id += 1
        rid = f"reg-{self._next_id}"
        request = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self._cond:
            scanned = 0
            assert self._proc.stdin is not None  # Popen stdin=PIPE 保证
            self._proc.stdin.write(request + "\n")
            self._proc.stdin.flush()
            while True:
                for line in self._lines[scanned:]:
                    scanned += 1
                    msg = self._parse(line)
                    if isinstance(msg, dict) and msg.get("id") == rid:
                        if "error" in msg:
                            raise RpcFlowError(f"{method} 失败：{msg['error']}")
                        return dict(msg.get("result") or {})
                if self._eof:
                    raise RpcFlowError(
                        f"{method} 等待响应时引擎退出（stderr 尾部：{' / '.join(self._stderr_tail[-3:]) or '无'}）"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RpcFlowError(f"{method} 响应超时（{timeout}s）")
                self._cond.wait(min(_STATUS_POLL_INTERVAL, remaining))

    def close(self, force: bool = False) -> None:
        """优雅退出（stdin EOF → 引擎 drain 后自退）；force 硬杀（崩溃演练）。"""
        proc = self._proc
        if proc.poll() is None:
            if force:
                proc.kill()
            else:
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._reader.join(timeout=5)
        self._drainer.join(timeout=5)


class RpcTaskFlow:
    """真实引擎进程任务流（TaskFlow 端口的 RPC 实现）。

    每个 flow 实例对应一个引擎进程；同 engine_home 重建实例 + 进程即生产级
    断点恢复路径（restart → task_create → start_stage → 检查点 restore）。
    """

    def __init__(
        self,
        task_id: str,
        title: str,
        problem_text: str,
        *,
        engine_home: Path | str,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        extra_env: dict[str, str] | None = None,
        stage_timeout: float = 900.0,
        engine_cmd: list[str] | None = None,
        work_cwd: Path | str | None = None,
    ) -> None:
        self._task_id = task_id
        self._stage_timeout = stage_timeout
        self._home = Path(engine_home)
        self._home.mkdir(parents=True, exist_ok=True)
        self._executed: list[str] = []
        self._cursor: str | None = None

        env = {**os.environ, "ERDOS_ENGINE_HOME": str(self._home)}
        if extra_env:
            env.update(extra_env)
        if api_key:
            if not base_url or not model:
                raise ValueError("Key 模式需要 base_url 与 model（引擎拒启红线：禁猜测端点）")
            env["ERDOS_MODEL_BASE_URL"] = base_url
            env["ERDOS_MODEL_NAME"] = model
            if provider:
                env["ERDOS_MODEL_PROVIDER"] = provider
            first_line = api_key  # 首行密钥约定：Key 仅经 stdin 一次性注入，不落盘
        else:
            first_line = "ERDOS_NO_KEY"

        cmd = list(engine_cmd) if engine_cmd else [sys.executable, "-m", "engine"]
        self._transport = _EngineTransport(
            cmd, Path(work_cwd) if work_cwd else REPO_ROOT, env, first_line,
        )
        self._transport.rpc("initialize", {"client_protocol_version": 2})
        self._transport.rpc(
            "task_create", {"task_id": task_id, "title": title, "problem_text": problem_text},
        )

    # ------------------------------------------------------------------
    # TaskFlow 端口
    # ------------------------------------------------------------------
    def current_stage(self) -> str:
        if self._cursor is None:
            self._cursor = self._resolve_stage()
        return self._cursor

    async def run_stage(self, stage: str) -> dict[str, Any]:
        if stage != self.current_stage():
            raise ValueError(f"阶段顺序约束：当前应执行 {self.current_stage()}，收到 {stage}")
        loop = asyncio.get_running_loop()
        # 重放检测：门禁未决恢复路径 start_stage 会重挂门禁而不重执行阶段，
        # 以留痕 model_call 计数是否增长为准判定「本流是否真实执行」（副作用观测）
        calls_before = await loop.run_in_executor(None, self._stage_call_count, stage)
        await loop.run_in_executor(
            None,
            lambda: self._transport.rpc(
                "start_stage", {"task_id": self._task_id, "stage": stage}, timeout=30.0,
            ),
        )
        await loop.run_in_executor(None, self._await_stage)
        data: dict[str, Any] = {"usage": await loop.run_in_executor(None, self._stage_usage, stage)}
        if stage == "writing":
            sha = await loop.run_in_executor(None, self._paper_sha256)
            if sha:
                data["paper_sha256"] = sha
        calls_after = await loop.run_in_executor(None, self._stage_call_count, stage)
        if calls_after > calls_before:
            self._executed.append(stage)
        return data

    async def answer_gate(self, decision: str) -> dict[str, Any]:
        gate = f"gate_{self.current_stage()}"
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._transport.rpc(
                "answer_gate",
                {"task_id": self._task_id, "gate": gate, "decision": decision},
                timeout=60.0,
            ),
        )
        if result.get("action") == "next_stage":
            index = min(STAGES.index(self.current_stage()) + 1, len(STAGES) - 1)
            self._cursor = STAGES[index]
        return result

    def executed_stages(self) -> tuple[str, ...]:
        return tuple(self._executed)

    def close(self, force: bool = False) -> None:
        self._transport.close(force=force)

    # ------------------------------------------------------------------
    # 内部：检查点定位 / 阶段等待 / 留痕回读
    # ------------------------------------------------------------------
    def _resolve_stage(self) -> str:
        """检查点 → 当前阶段（与 StageOrchestrator.restore 定位口径一致）。

        最后落库记录为「执行完成（门禁未决）」时定位到该阶段本身（恢复后重挂
        门禁）；为「门禁通过」时定位到下一阶段。
        """
        db = self._home / "checkpoints.db"
        if not db.exists():
            return STAGES[0]
        store = SQLiteCheckpointStore(str(db))
        try:
            completed = store.completed_stages(self._task_id)
        finally:
            store.close()
        if not completed:
            return STAGES[0]
        last = completed[-1]
        if last.status == CHECKPOINT_EXECUTED:
            return last.stage
        last_index = STAGES.index(last.stage)
        return STAGES[min(last_index + 1, len(STAGES) - 1)]

    def _await_stage(self) -> None:
        """轮询 get_status 至阶段终态；failed 按异常类型重映射（归因）。"""
        deadline = time.monotonic() + self._stage_timeout
        while time.monotonic() < deadline:
            snapshot = self._transport.rpc("get_status", {}, timeout=15.0)
            task = snapshot.get("task") or {}
            status = task.get("status")
            if status == "done":
                return
            if status == "failed":
                raise self._map_failure(str(snapshot.get("last_error") or "未知错误"))
            time.sleep(_STATUS_POLL_INTERVAL)
        raise TimeoutError(f"阶段执行超时（{self._stage_timeout}s）：{self._task_id}")

    @staticmethod
    def _map_failure(last_error: str) -> Exception:
        """last_error 形如 'TypeName: detail'——重映射异常类型，运行器据此归因。"""
        type_name = last_error.partition(":")[0]
        if any(key in type_name for key in ("Connection", "Http", "Network", "Adapter")):
            return ConnectionError(last_error)
        if "Timeout" in type_name or "Sandbox" in type_name:
            return TimeoutError(last_error)
        return RuntimeError(last_error)

    def _stage_call_count(self, stage: str) -> int:
        """该阶段 model_call 留痕条数（重放检测基准）。"""
        store = TrailStore(str(self._home / "audit.db"))
        try:
            return len([
                event for event in store.events(self._task_id)
                if event.event_type == EventType.MODEL_CALL and event.stage == stage
            ])
        finally:
            store.close()

    def _stage_usage(self, stage: str) -> dict[str, int]:
        """留痕库回读该阶段全部 model_call 的 usage 合计（含求解内循环多调用）。"""
        store = TrailStore(str(self._home / "audit.db"))
        try:
            calls = [
                event for event in store.events(self._task_id)
                if event.event_type == EventType.MODEL_CALL and event.stage == stage
            ]
        finally:
            store.close()
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        for call in calls:
            detail_usage = (call.detail or {}).get("usage") or {}
            usage["prompt_tokens"] += int(detail_usage.get("prompt_tokens", 0) or 0)
            usage["completion_tokens"] += int(detail_usage.get("completion_tokens", 0) or 0)
        return usage

    def _paper_sha256(self) -> str | None:
        store = TrailStore(str(self._home / "audit.db"))
        try:
            papers = [a for a in store.artifacts(self._task_id) if a["kind"] == "paper"]
        finally:
            store.close()
        return papers[-1]["sha256"] if papers else None
