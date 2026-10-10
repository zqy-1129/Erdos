"""Erdos 引擎真实端到端驱动：填入你的 API Key，按题目生成论文。

用法（仓库根目录）：
    python scripts/run_paper_e2e.py \
        --api-key sk-xxxx --base-url https://api.deepseek.com/v1 \
        --model deepseek-chat --provider deepseek \
        --title "生产计划优化" --problem-file problem.txt \
        [--tool-loop] [--out run_out]
    python scripts/run_paper_e2e.py --no-key --title "演示" \
        --problem-text "最小二乘拟合演示题面" [--out run_out]   # FakeLLM 冒烟（演示专用）

流程：拉起引擎（stdin 首行注入 Key）→ task_create 登记题面 →
四阶段（analysis/modeling/solving/writing，每阶段门禁自动通过）→ 论文 paper.md 落盘。

说明：
- 协议驱动复用 engine.regression.rpc_flow.RpcTaskFlow（与 SP1-7 回归验收同一实现；
  重试/退避/错误分类同 EC-N1/N2/N3），本脚本只做参数解析、事件展示与产物拷贝。
- 模型请求 BYOK 直连你填的厂商；平台不经手；Key 仅经 stdin 首行注入引擎，不落盘。
- --tool-loop 开启模型驱动求解循环（execute_code/plot_figure 真沙箱执行）；
  默认 stage_level 仅演示脚本求解。
- 产物：Markdown 论文草稿 + 求解产物，位于 --out 目录；重跑请换 --out（或删除
  engine_home），否则按检查点恢复语义续跑而非重新开始。
- ERDOS_SANDBOX_REQUIRE_DOCKER=1 等环境变量原样透传给引擎进程。
"""

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from engine.ipc.stdio import configure_stdio
from engine.orchestrator.graph import STAGES
from engine.regression.rpc_flow import RpcTaskFlow

TASK_ID = "paper-run"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Erdos 引擎题目→论文端到端驱动")
    parser.add_argument("--api-key", default=None, help="你的厂商 API Key（仅经 stdin 注入引擎，不落盘）")
    parser.add_argument("--base-url", default=None, help="OpenAI 兼容 Base URL，如 https://api.deepseek.com/v1")
    parser.add_argument("--model", default=None, help="模型名，如 deepseek-chat")
    parser.add_argument("--provider", default="openai-compat", help="厂商族（能力矩阵键，如 deepseek/vllm/openai）")
    parser.add_argument("--no-key", action="store_true", help="无 Key 模式（FakeLLM 确定性假模型，仅供演示/冒烟）")
    parser.add_argument("--title", required=True, help="题目标题")
    parser.add_argument("--problem-file", help="题面文件（纯文本）；与 --problem-text 二选一")
    parser.add_argument("--problem-text", help="题面文本（直接传参）")
    parser.add_argument("--tool-loop", action="store_true", help="启用模型驱动求解循环（真沙箱执行代码）")
    parser.add_argument("--out", default="run_out", help="输出目录（默认 ./run_out）")
    return parser.parse_args()


def _print_event(event: dict) -> None:
    """事件流实时展示（与引擎 stdout NDJSON 同步）。"""
    if event.get("event") == "model.delta":
        print(f"  [流式] {event.get('delta', '')}", end="", flush=True)
    else:
        label = event.get("stage", event.get("artifact", ""))
        print(f"  [事件] {event.get('event')} {label}")


async def _run(args: argparse.Namespace) -> None:
    problem = args.problem_text or (
        Path(args.problem_file).read_text(encoding="utf-8") if args.problem_file else ""
    )
    if not problem.strip():
        sys.exit("必须提供 --problem-file 或 --problem-text")

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    home = out_dir / "engine_home"

    flow = RpcTaskFlow(
        task_id=TASK_ID,
        title=args.title,
        problem_text=problem,
        engine_home=home,
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        provider=args.provider,
        extra_env={"ERDOS_TOOL_MODE": "tool_loop"} if args.tool_loop else None,
        stage_timeout=600.0,
        on_event=_print_event,
    )
    for index, stage in enumerate(STAGES):
        print(f"== 阶段 {index + 1}/4：{stage} ==")
        await flow.run_stage(stage)
        await flow.answer_gate("pass")

    paper = home / "tasks" / TASK_ID / "paper.md"
    if not paper.exists():
        sys.exit("论文未生成（见上方事件/错误输出）")
    final = out_dir / "paper.md"
    shutil.copyfile(paper, final)
    print(f"\n完成：论文已生成 → {final}")
    print(f"引擎数据（留痕/检查点/产物）目录：{home}")
    flow.close()


def main() -> None:
    configure_stdio()  # 驱动脚本同样按 UTF-8 输出中文进度，与 Windows 控制台码页解耦
    args = _parse_args()
    if args.api_key and not (args.base_url and args.model):
        sys.exit("Key 模式需要 --base-url 与 --model（引擎拒启红线：禁猜测端点）")
    if not args.api_key and not args.no_key:
        sys.exit("必须提供 --api-key（或 --no-key 进入 FakeLLM 演示冒烟模式）")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
