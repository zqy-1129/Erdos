"""RPC 任务流系统测试（SP1-7）：真实 `python -m engine` 进程端到端 + 进程级 kill/恢复。

与 test_entry_wiring（协议握手/拒启红线）互补：本文件经 AcceptanceRunner +
RpcTaskFlow 驱动完整四阶段，验证生产断点恢复路径（restart → task_create →
start_stage → 检查点 restore）与留痕地面真值（audit.db 无重放）。
无 Key 模式走 ERDOS_NO_KEY（FakeLLM 确定性，链路与真实 Key 通道一致）。
"""

import http.server
import json as _json
import sys
import threading
from pathlib import Path

from engine.checkpoint.store import SQLiteCheckpointStore
from engine.orchestrator.graph import STAGES
from engine.regression.problems import REGRESSION_SET, RegressionProblem
from engine.regression.rpc_flow import RpcTaskFlow
from engine.regression.runner import CHANNEL_REAL, AcceptanceRunner
from engine.trail.store import EventType, TrailStore

_STAGE_TIMEOUT = 120.0


def _rpc_factory(root: Path):
    """RPC 任务流工厂：每题独立引擎 home（检查点/留痕/产物同源）。"""

    def build(problem: RegressionProblem) -> RpcTaskFlow:
        return RpcTaskFlow(
            task_id=f"reg-{problem.business_id}",
            title=problem.title,
            problem_text=problem.statement,
            engine_home=root / "homes" / problem.business_id,
            stage_timeout=_STAGE_TIMEOUT,
        )

    return build


def _model_call_count(audit_db: Path, task_id: str) -> int:
    store = TrailStore(str(audit_db))
    try:
        return len([
            e for e in store.events(task_id) if e.event_type == EventType.MODEL_CALL
        ])
    finally:
        store.close()


def _paper_path(home: Path, task_id: str) -> Path:
    return home / "tasks" / task_id / "paper.md"


async def test_rpc_flow_drives_real_engine_process_full_run(tmp_path) -> None:
    """真实引擎进程全程：四阶段 + 门禁 → 论文落盘 + 留痕 4 次 model_call。"""
    problem = REGRESSION_SET[0]
    task_id = f"reg-{problem.business_id}"
    runner = AcceptanceRunner(_rpc_factory(tmp_path))
    report = await runner.run_all((problem,))

    result = report.results[0]
    assert result.passed and result.failure_module is None
    assert result.executed_stages == STAGES
    assert result.prompt_tokens > 0 and result.completion_tokens > 0
    assert result.paper_sha256 and len(result.paper_sha256) == 64

    home = tmp_path / "homes" / problem.business_id
    paper = _paper_path(home, task_id)
    assert paper.exists()  # 论文真实落盘于引擎 home
    assert _model_call_count(home / "audit.db", task_id) == 4


async def test_rpc_flow_process_kill_resume_no_replay(tmp_path) -> None:
    """进程级 kill/恢复演练：modeling 后硬杀引擎进程，重启续跑且全程无阶段重放。"""
    problem = next(p for p in REGRESSION_SET if p.category == "prediction")
    task_id = f"reg-{problem.business_id}"
    runner = AcceptanceRunner(_rpc_factory(tmp_path))
    result = await runner.run_one(problem, kill_after_stage="modeling")

    assert result.passed and result.resumed
    assert result.kill_after_stage == "modeling"
    assert result.executed_stages == STAGES  # 驱动视角：每阶段恰执行一次
    assert result.paper_sha256 and len(result.paper_sha256) == 64

    home = tmp_path / "homes" / problem.business_id
    audit = home / "audit.db"
    assert _model_call_count(audit, task_id) == 4  # 地面真值：恢复后未重放任何阶段
    assert _paper_path(home, task_id).exists()

    # 检查点收尾状态：四阶段全部留档
    store = SQLiteCheckpointStore(str(home / "checkpoints.db"))
    try:
        assert len(store.completed_stages(task_id)) == 4
    finally:
        store.close()


def test_run_paper_e2e_script_no_key_smoke(tmp_path: Path) -> None:
    """重构后 run_paper_e2e.py（共享 RpcTaskFlow 驱动）无 Key 冒烟：四阶段出论文。

    同一协议实现跑通「脚本 → 引擎进程 → 论文落盘」全链，保证 SP1-7 验收方案
    引用的驱动脚本与回归验收工具不漂移。
    """
    import subprocess

    repo_root = Path(__file__).resolve().parents[2]
    out_dir = tmp_path / "run_out"
    proc = subprocess.run(  # noqa: S603 - 固定解释器与脚本，测试受控输入
        [
            sys.executable, str(repo_root / "scripts" / "run_paper_e2e.py"),
            "--no-key", "--title", "演示题", "--problem-text", "最小二乘拟合演示题面",
            "--out", str(out_dir),
        ],
        cwd=str(repo_root), capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout[-2000:]} stderr={proc.stderr[-2000:]}"
    assert "完成：论文已生成" in proc.stdout
    assert (out_dir / "paper.md").exists()
    # 事件展示链路工作（stage.progress 经 on_event 打印）
    assert "[事件]" in proc.stdout


# ----------------------------------------------------------------------
# 真实 Key 通道全链演练（mock 厂商端点）：引擎进程 + stdin 注入 + 适配器 + 真实 HTTP
# 覆盖 SP1-7 步骤 3 的全部接缝——唯一差异是厂商端点为本地替身（11-05 换真实凭据即真实回归）。
# ----------------------------------------------------------------------

_MOCK_CONTENT = "【摘要】mock 摘要，覆盖问题与方法。【结论】mock 结论，含局限与改进。"
_MOCK_USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


class _MockVendorHandler(http.server.BaseHTTPRequestHandler):
    """最小 OpenAI 兼容厂商替身：POST /v1/chat/completions；可编程首 N 次 429。"""

    def do_POST(self) -> None:  # noqa: N802 - http.server 命名约定
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            model = (_json.loads(body) if body else {}).get("model", "")
        except _json.JSONDecodeError:
            model = ""
        self.server.requests.append({  # type: ignore[attr-defined]
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "model": model,
        })

        if self.server.fail_first > 0:  # type: ignore[attr-defined]
            self.server.fail_first -= 1  # type: ignore[attr-defined]
            self._respond(429, {"error": {"message": "slow down"}},
                          headers={"Retry-After": str(self.server.retry_after)})  # type: ignore[attr-defined]
            return
        self._respond(200, {
            "choices": [{
                "message": {"role": "assistant", "content": _MOCK_CONTENT},
                "finish_reason": "stop",
            }],
            "usage": dict(_MOCK_USAGE),
        })

    def _respond(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        body = _json.dumps(payload).encode("utf-8")
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: ANN002 - 静默访问日志
        pass


class _MockVendorServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, fail_first: int, retry_after: int) -> None:
        super().__init__(("127.0.0.1", 0), _MockVendorHandler)
        self.requests: list[dict] = []
        self.fail_first = fail_first
        self.retry_after = retry_after


def _vendor_factory(root: Path, base_url: str):
    """真实 Key 通道任务流工厂：注入 mock 端点与测试 Key。"""

    def build(problem: RegressionProblem) -> RpcTaskFlow:
        return RpcTaskFlow(
            task_id=f"reg-{problem.business_id}",
            title=problem.title,
            problem_text=problem.statement,
            engine_home=root / "homes" / problem.business_id,
            api_key="sk-test-vendor-key",
            base_url=base_url,
            model="mock-model",
            provider="openai-compat",
            stage_timeout=_STAGE_TIMEOUT,
        )

    return build


async def test_real_key_channel_full_chain_with_mock_vendor(tmp_path) -> None:
    """真实 Key 通道全链：stdin 注入 → 适配器 → HTTP → usage 留痕 → 论文。

    验证引擎进程在 Key 模式下四阶段全通，Authorization 头携带注入 Key
    （不出现在事件/日志面），usage 来自厂商应答（非估算），决策门对
    real_key 通道给出 GO。
    """
    problem = REGRESSION_SET[0]
    server = _MockVendorServer(fail_first=0, retry_after=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        runner = AcceptanceRunner(_vendor_factory(tmp_path, base_url), channel=CHANNEL_REAL)
        report = await runner.run_all((problem,))
    finally:
        server.shutdown()

    result = report.results[0]
    assert result.passed and result.failure_module is None
    assert result.executed_stages == STAGES
    assert result.prompt_tokens == 4 * _MOCK_USAGE["prompt_tokens"]  # 四阶段各一次调用
    assert result.paper_sha256

    home = tmp_path / "homes" / problem.business_id
    assert (home / "tasks" / f"reg-{problem.business_id}" / "paper.md").exists()

    # Key 注入链路：stdin 首行 → 适配器 Authorization 头（仅此处出现，不出现在留痕）
    assert {req["authorization"] for req in server.requests} == {"Bearer sk-test-vendor-key"}
    assert all(req["path"] == "/v1/chat/completions" for req in server.requests)
    assert {req["model"] for req in server.requests} == {"mock-model"}

    # 真实通道决策门：1/1 通过 + 题型防偏科满足 → GO
    assert report.decision().verdict == "GO"


async def test_real_key_channel_429_retry_after_through_process(tmp_path) -> None:
    """EC-N2 进程级验证：厂商 429 + Retry-After:0 → 引擎内重试后恢复，全程成功。"""
    problem = REGRESSION_SET[0]
    server = _MockVendorServer(fail_first=1, retry_after=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        runner = AcceptanceRunner(_vendor_factory(tmp_path, base_url), channel=CHANNEL_REAL)
        report = await runner.run_all((problem,))
    finally:
        server.shutdown()

    result = report.results[0]
    assert result.passed  # 重试恢复，阶段不失败
    assert len(server.requests) == 4 * 1 + 1  # 四阶段各一次成功 + 首次被 429
    assert result.prompt_tokens == 4 * _MOCK_USAGE["prompt_tokens"]  # 失败调用不计 usage
