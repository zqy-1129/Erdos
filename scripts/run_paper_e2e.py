"""Erdos 引擎真实端到端驱动：填入你的 API Key，按题目生成论文。

用法（仓库根目录）：
    python scripts/run_paper_e2e.py \
        --api-key sk-xxxx --base-url https://api.deepseek.com/v1 \
        --model deepseek-chat --provider deepseek \
        --title "生产计划优化" --problem-file problem.txt \
        [--tool-loop] [--out run_out]

流程：拉起引擎（stdin 首行注入 Key）→ task_create 登记题面 →
四阶段（analysis/modeling/solving/writing，每阶段门禁自动通过）→ 论文 paper.md 落盘。

说明：
- 模型请求 BYOK 直连你填的厂商；平台不经手。
- --tool-loop 开启模型驱动求解循环（execute_code/plot_figure 真沙箱执行）；
  默认 stage_level 仅演示脚本求解。
- 产物：Markdown 论文草稿 + 求解产物，位于 --out 目录；LaTeX/Word 导出属后续增量。
- 需要设置 ERDOS_SANDBOX_REQUIRE_DOCKER=1 时自行传环境变量（本脚本透传环境）。
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STAGES = ("analysis", "modeling", "solving", "writing")


def main() -> None:
    parser = argparse.ArgumentParser(description="Erdos 引擎题目→论文端到端驱动")
    parser.add_argument("--api-key", required=True, help="你的厂商 API Key（仅经 stdin 注入引擎，不落盘）")
    parser.add_argument("--base-url", required=True, help="OpenAI 兼容 Base URL，如 https://api.deepseek.com/v1")
    parser.add_argument("--model", required=True, help="模型名，如 deepseek-chat")
    parser.add_argument("--provider", default="openai-compat", help="厂商族（能力矩阵键，如 deepseek/vllm/openai）")
    parser.add_argument("--title", required=True, help="题目标题")
    parser.add_argument("--problem-file", help="题面文件（纯文本）；与 --problem-text 二选一")
    parser.add_argument("--problem-text", help="题面文本（直接传参）")
    parser.add_argument("--tool-loop", action="store_true", help="启用模型驱动求解循环（真沙箱执行代码）")
    parser.add_argument("--out", default="run_out", help="输出目录（默认 ./run_out）")
    args = parser.parse_args()

    problem = args.problem_text or (Path(args.problem_file).read_text(encoding="utf-8") if args.problem_file else "")
    if not problem.strip():
        sys.exit("必须提供 --problem-file 或 --problem-text")

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    home = out_dir / "engine_home"

    env = {**os.environ, "ERDOS_ENGINE_HOME": str(home)}
    if args.tool_loop:
        env["ERDOS_TOOL_MODE"] = "tool_loop"
    proc = subprocess.Popen(
        [sys.executable, "-m", "engine"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", cwd=str(REPO_ROOT), env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None

    lines: list[str] = []

    def reader() -> None:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "event" in msg:  # 事件流实时打印
                if msg["event"] == "model.delta":
                    print(f"  [流式] {msg['delta']}", end="", flush=True)
                else:
                    print(f"  [事件] {msg['event']} {msg.get('stage', msg.get('artifact', ''))}")
            elif "error" in msg:
                print(f"  [错误] {msg['error']}")

    threading.Thread(target=reader, daemon=True).start()

    def rpc(req_id: str, method: str, params: dict, timeout: float = 600.0) -> dict:
        before = len(lines)
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in lines[before:]:
                msg = json.loads(line)
                if msg.get("id") == req_id:
                    if "error" in msg:
                        sys.exit(f"{method} 失败：{msg['error']}")
                    print(f"[{method}] ok")
                    return msg["result"]
            time.sleep(0.1)
        sys.exit(f"{method} 超时")

    # 1. 注入 Key（首行）→ 握手
    proc.stdin.write(args.api_key + "\n")
    proc.stdin.flush()
    rpc("i1", "initialize", {"client_protocol_version": 2})

    # 2. 登记题面 → 四阶段推进（门禁自动通过）
    task_id = "paper-run"
    rpc("t1", "task_create", {"task_id": task_id, "title": args.title, "problem_text": problem})
    for index, stage in enumerate(STAGES):
        print(f"== 阶段 {index + 1}/4：{stage} ==")
        rpc(f"s{index}", "start_stage", {"task_id": task_id, "stage": stage})
        rpc(f"g{index}", "answer_gate",
            {"task_id": task_id, "gate": f"gate_{stage}", "decision": "pass"})

    paper = home / "tasks" / task_id / "paper.md"
    if not paper.exists():
        sys.exit("论文未生成（见上方错误输出）")
    final = out_dir / "paper.md"
    final.write_text(paper.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"\n完成：论文已生成 → {final}")
    print(f"引擎数据（留痕/检查点/产物）目录：{home}")

    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


if __name__ == "__main__":
    main()
