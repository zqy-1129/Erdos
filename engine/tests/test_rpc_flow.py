"""RPC 任务流系统测试（SP1-7）：真实 `python -m engine` 进程端到端 + 进程级 kill/恢复。

与 test_entry_wiring（协议握手/拒启红线）互补：本文件经 AcceptanceRunner +
RpcTaskFlow 驱动完整四阶段，验证生产断点恢复路径（restart → task_create →
start_stage → 检查点 restore）与留痕地面真值（audit.db 无重放）。
无 Key 模式走 ERDOS_NO_KEY（FakeLLM 确定性，链路与真实 Key 通道一致）。
"""

import http.server
import json as _json
import re
import sys
import threading
from pathlib import Path

from engine.checkpoint.store import SQLiteCheckpointStore
from engine.gates.llm_evaluator import LlmRubricEvaluator
from engine.gates.schema import load_rubric_for_stage
from engine.orchestrator.graph import STAGES
from engine.regression.problems import REGRESSION_SET, RegressionProblem
from engine.regression.rpc_flow import RpcTaskFlow
from engine.regression.runner import (
    CHANNEL_REAL,
    AcceptanceRunner,
    FailureModule,
    RubricGatePolicy,
)
from engine.trail.store import EventType, TrailStore

_STAGE_TIMEOUT = 120.0
_RUBRICS_DIR = Path(__file__).resolve().parent.parent / "gates" / "rubrics"


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

# 评委请求识别：llm_evaluator 的 system prompt 首句（同一常量单源，避免测试与实现漂移）
_JUDGE_MARKER = "质量门禁评委"
_DIMENSION_LINE = re.compile(r"^- (.*?)（权重", re.MULTILINE)


def _judge_output(dimension_names: list[str], score: float) -> str:
    """厂商替身：对 prompt 里列出的每个维度给同一分数（模拟"全维度 0.9/0.2"的评委）。"""
    return _json.dumps(
        {"scores": [{"dimension": n, "score": score, "reason": "mock"} for n in dimension_names]},
        ensure_ascii=False,
    )


class _MockVendorHandler(http.server.BaseHTTPRequestHandler):
    """最小 OpenAI 兼容厂商替身：POST /v1/chat/completions；可编程首 N 次 429。

    评委请求（system 含 _JUDGE_MARKER）按 judge_score 回结构化评分，其余按 content
    回阶段产物文本——真实 Key 通道下引擎与回归驱动打的是同一个端点。
    """

    def do_POST(self) -> None:  # noqa: N802 - http.server 命名约定
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = _json.loads(body) if body else {}
        except _json.JSONDecodeError:
            payload = {}
        messages = payload.get("messages") or []
        text = " ".join(str(m.get("content", "")) for m in messages)
        is_judge = _JUDGE_MARKER in text
        self.server.requests.append({  # type: ignore[attr-defined]
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "model": payload.get("model", ""),
            "judge": is_judge,
        })

        if self.server.fail_first > 0:  # type: ignore[attr-defined]
            self.server.fail_first -= 1  # type: ignore[attr-defined]
            self._respond(429, {"error": {"message": "slow down"}},
                          headers={"Retry-After": str(self.server.retry_after)})  # type: ignore[attr-defined]
            return
        if is_judge and self.server.judge_fail_status:  # type: ignore[attr-defined]
            self._respond(
                self.server.judge_fail_status, {"error": {"message": "judge unavailable"}}  # type: ignore[attr-defined]
            )
            return
        if is_judge and self.server.judge_score is not None:  # type: ignore[attr-defined]
            content = _judge_output(
                _DIMENSION_LINE.findall(text), self.server.judge_score  # type: ignore[attr-defined]
            )
        else:
            content = self.server.content  # type: ignore[attr-defined]
        self._respond(200, {
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
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

    def __init__(
        self, fail_first: int, retry_after: int,
        *, content: str = _MOCK_CONTENT, judge_score: float | None = None,
        judge_fail_status: int | None = None,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _MockVendorHandler)
        self.requests: list[dict] = []
        self.fail_first = fail_first
        self.retry_after = retry_after
        self.content = content
        self.judge_score = judge_score
        self.judge_fail_status = judge_fail_status


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


# ----------------------------------------------------------------------
# SP1-3 rubric 评委进判定通道（S6-2）：真实引擎进程产物 + 真实 HTTP 评委调用。
# 厂商端点为本地替身（DEC-024 口径：fake 与真实分开统计）；11-05 换真实凭据即真实回归。
# ----------------------------------------------------------------------

def _rubrics() -> dict:
    return {stage: load_rubric_for_stage(stage, _RUBRICS_DIR) for stage in STAGES}


def _judge_llm(base_url: str):
    """回归驱动侧的评委模型端口：真实适配器 + 真实 HTTP（Key 仅内存持有，DEC-011）。"""
    from engine.adapters.key_store import KeyStore
    from engine.adapters.openai_compat import ChatMessage, ModelConfig, OpenAIChatAdapter

    keys = KeyStore()
    keys.inject("sk-test-vendor-key")
    config = ModelConfig(provider="openai-compat", base_url=base_url, model="mock-model")
    adapter = OpenAIChatAdapter(config, keys)

    async def judge(messages: list[dict[str, str]], stage: str) -> dict:
        reply = await adapter.chat(
            [ChatMessage(role=m["role"], content=m["content"]) for m in messages]
        )
        return {
            "content": reply.content,
            "usage": {
                "prompt_tokens": reply.usage.prompt_tokens,
                "completion_tokens": reply.usage.completion_tokens,
            },
            "model": config.model,
            "stage": stage,
        }

    return judge


async def _run_rubric_channel(tmp_path, server, *, problem=None):
    """真实 Key 通道 + RubricGatePolicy 跑一题 → (单题结果, 评委用量, 厂商请求记录)。"""
    threading.Thread(target=server.serve_forever, daemon=True).start()
    usage: list[dict] = []
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        policy = RubricGatePolicy(
            LlmRubricEvaluator(_judge_llm(base_url), usage_sink=usage.append), _rubrics()
        )
        runner = AcceptanceRunner(
            _vendor_factory(tmp_path, base_url), gate_policy=policy, channel=CHANNEL_REAL
        )
        report = await runner.run_all(((problem or REGRESSION_SET[0]),))
    finally:
        judge_requests = list(server.requests)
        server.shutdown()
    return report.results[0], usage, judge_requests


async def test_rubric_gate_scores_engine_artifacts_through_http(tmp_path) -> None:
    """判定通道的「门禁通过为硬条件」自此有机器判定：评委读引擎真实产物、走真实 HTTP。"""
    problem = REGRESSION_SET[0]
    result, usage, requests = await _run_rubric_channel(
        tmp_path, _MockVendorServer(0, 0, judge_score=0.9), problem=problem
    )

    assert result.passed, result.failure_detail
    assert [g.stage for g in result.gate_scores] == list(STAGES)
    assert all(g.passed and g.attempts == 1 for g in result.gate_scores)
    assert result.gate_scores[0].total_score == 0.9

    # 评委调用确实打到厂商端点，且用量如实计数（自动评审成本不隐藏）
    assert len([r for r in requests if r["judge"]]) == 4
    assert len(usage) == 4
    assert usage[0]["usage"]["prompt_tokens"] == _MOCK_USAGE["prompt_tokens"]
    assert usage[0]["stage"] == "analysis"


async def test_rubric_gate_reject_exhausts_retries_and_attributes_gate(tmp_path) -> None:
    """低分产物：重试 ≤3（SP1-3 红线）后归因 gate，且不计入成功分母。"""
    result, usage, _ = await _run_rubric_channel(
        tmp_path, _MockVendorServer(0, 0, judge_score=0.2)
    )
    assert result.passed is False
    assert result.failure_module == FailureModule.GATE
    assert "门禁评审未通过" in result.failure_detail
    assert result.stages_passed == 0
    assert result.gate_scores[0].passed is False
    assert result.gate_scores[0].attempts == 3
    assert len(usage) == 3  # 评委被叫 3 次即停（不无限重试刷 token）


async def test_judge_infra_failure_attributes_to_adapter_not_gate(tmp_path) -> None:
    """评委端点故障归 adapter：伪装成"评审不通过"会把 infra 故障计成质量问题。"""
    result, usage, _ = await _run_rubric_channel(
        tmp_path, _MockVendorServer(0, 0, judge_score=0.9, judge_fail_status=500)
    )
    assert result.passed is False
    assert result.failure_module == FailureModule.ADAPTER
    assert "评审未通过" not in result.failure_detail
    assert usage == []  # 5xx 不产生用量记录（失败调用不计数）


async def test_hard_check_pause_fails_fast_instead_of_until_timeout(tmp_path) -> None:
    """引擎硬检查驳回把任务置 paused：驱动必须当轮判出，不得空转到阶段超时（原会等 120s）。"""
    problem = REGRESSION_SET[0]
    server = _MockVendorServer(0, 0, content="")  # 空产出 → analysis insights 全空白
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        runner = AcceptanceRunner(
            _vendor_factory(tmp_path, base_url), channel=CHANNEL_REAL
        )
        result = await runner.run_one(problem)
    finally:
        server.shutdown()

    assert not result.passed
    assert result.failure_module == FailureModule.GATE
    assert "硬检查不通过" in result.failure_detail
    assert result.stages_passed == 0


async def test_rpc_flow_returns_stage_artifact_data_for_review(tmp_path) -> None:
    """RPC 通道回读检查点里的阶段产物（原 run_stage 只回 usage，评委无从评分）。"""
    problem = REGRESSION_SET[1]
    flow = RpcTaskFlow(
        task_id="reg-readback",
        title=problem.title,
        problem_text=problem.statement,
        engine_home=tmp_path / "home",
        stage_timeout=_STAGE_TIMEOUT,
    )
    try:
        data = await flow.run_stage("analysis")
        assert data.get("insights")
        assert flow.hard_reject_reason() is None
        await flow.answer_gate("pass")
        for stage in STAGES[1:-1]:
            await flow.run_stage(stage)
            await flow.answer_gate("pass")
        writing = await flow.run_stage("writing")
        assert "## 摘要" in writing["paper_md"]
        assert writing["paper_sha256"]
    finally:
        flow.close()


async def test_paused_reason_falls_back_to_events_replay(tmp_path) -> None:
    """事件未及时到达时经 events_replay 兜底取回驳回原因（否则失败会被错记成编排故障）。"""
    problem = REGRESSION_SET[0]
    server = _MockVendorServer(0, 0, content="")  # 空产出 → analysis 硬检查违规 → paused
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        flow = RpcTaskFlow(
            task_id="reg-paused", title=problem.title, problem_text=problem.statement,
            engine_home=tmp_path / "home", api_key="sk-test-vendor-key",
            base_url=base_url, model="mock-model", stage_timeout=_STAGE_TIMEOUT,
        )
        try:
            await flow.run_stage("analysis")
            assert "硬检查不通过" in (flow.hard_reject_reason() or "")
            flow._hard_reject = None  # 模拟事件行晚于 get_status 到达的竞态
            assert "硬检查不通过" in (flow._recover_gate_failed_reason() or "")
        finally:
            flow.close()
    finally:
        server.shutdown()


async def test_run_regression_cli_rubric_channel_end_to_end(tmp_path) -> None:
    """验收命令本身可跑通：`run_regression.py --gate rubric` 驱动引擎进程 + rubric 评审。

    库层 rubric 全链由上一个用例覆盖，本用例把「脚本 → AcceptanceRunner → RpcTaskFlow →
    引擎子进程 → mock 厂商 → 评委 HTTP → 证据落盘」整条链钉住（QA 实跑的正是这条）。
    """
    import subprocess

    server = _MockVendorServer(0, 0, judge_score=0.9)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    repo_root = Path(__file__).resolve().parents[2]
    evidence = tmp_path / "evidence"
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        proc = subprocess.run(  # noqa: S603 - 固定解释器与脚本，测试受控输入
            [
                sys.executable, str(repo_root / "scripts" / "run_regression.py"),
                "--driver", "rpc", "--api-key", "sk-test-vendor-key", "--base-url", base_url,
                "--model", "mock-model", "--gate", "rubric", "--per-category", "1",
                "--no-kill-resume", "--stage-timeout", "120",
                "--work-root", str(tmp_path / "work"), "--evidence", str(evidence),
            ],
            cwd=str(repo_root), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600,
        )
    finally:
        server.shutdown()

    assert proc.returncode == 0, f"stdout={proc.stdout[-3000:]} stderr={proc.stderr[-3000:]}"
    assert "门禁=rubric" in proc.stdout
    assert "rubric 评审" in proc.stdout  # 评委消耗计数行（成本不隐藏）
    assert "[决策门] GO" in proc.stdout

    import json as _json_local

    summary = _json_local.loads((evidence / "summary.json").read_text(encoding="utf-8"))
    assert summary["gate_mode"] == "rubric"
    assert summary["success_rate_real_key"] == 1.0
    # 4 题 × 4 阶段各一次评委调用；评委 token 如实入账（自动评审成本不隐藏）
    assert summary["judge_usage"]["calls"] == 16
    assert summary["judge_usage"]["prompt_tokens"] > 0

    markdown = (evidence / "report.md").read_text(encoding="utf-8")
    assert "真实 Key 判定通道" in markdown  # 标题按通道落笔（DEC-024 分通道统计）
    assert "rubric 评审明细" in markdown
    assert "0.90/0.70" in markdown


async def test_run_regression_gate_policy_builds_real_judge(tmp_path) -> None:
    """驱动脚本的 --gate rubric 装配：真实适配器 + 真实 HTTP + 4 份阶段 rubric。

    只测策略装配与裁决，不拉起引擎（引擎侧与脚本级全链见前两个用例）。
    """
    import argparse
    import importlib.util

    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "run_regression", repo_root / "scripts" / "run_regression.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _build_gate_policy = module._build_gate_policy  # 脚本与验收口径共用同一装配

    server = _MockVendorServer(0, 0, judge_score=0.95)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        args = argparse.Namespace(
            gate="rubric", api_key="sk-test-vendor-key", base_url=base_url,
            model="mock-model", provider="openai-compat",
        )
        policy = _build_gate_policy(args, usage)
        outcome = await policy.decide(
            REGRESSION_SET[0], "analysis", {"stage": "analysis", "insights": ["要点"]}
        )
        empty = _build_gate_policy(argparse.Namespace(gate="autopass"), usage) is None
    finally:
        server.shutdown()

    assert outcome.decision == "pass"
    assert outcome.score is not None and outcome.score.total_score == 0.95
    assert usage["calls"] == 1 and usage["prompt_tokens"] == _MOCK_USAGE["prompt_tokens"]
    assert empty  # autopass 口径不装配评委


