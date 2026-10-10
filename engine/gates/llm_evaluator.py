"""rubric LLM 评委生产实现（SP1-3 评审执行器 + SP1-5 BYOK 适配器端口）。

分工：本模块只负责「把 rubric 与阶段产物组织成评委请求、把模型应答原样交回」；
评分解析、重试计数与转人工仍归 GateRunner（evaluator.py），避免两处口径分叉。

红线：
- 评分规则单源来自 rubric YAML——维度、权重、阈值、描述全部取自配置，
  本模块不硬编码任何维度名或关键词打分规则（SP1-3 禁止项）；
- 模型调用失败一律上抛：伪装成「评分不通过」会把 infra 故障归因成质量问题，
  破坏 SP1-7 的分层失败归因；
- 评委自身 token 消耗经 usage_sink 如实上报，不隐藏自动评审成本；
- 投喂有界：题面与产物按上限截断，并在 prompt 内显式声明已截断，
  不让评委假装看过全文。
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from engine.gates.schema import Rubric

# 评委模型端口：与 StagePipeline 的文本模型端口同型（(messages, stage) → 应答），
# 生产实现直接复用 __main__._build_llm 的 text_llm，测试注入替身。
JudgeLLM = Callable[[list[dict[str, str]], str], Awaitable[dict[str, Any]]]

MAX_ARTIFACT_CHARS = 8000  # 阶段产物投喂上限（求解 results / 论文正文按此截断）
MAX_PROBLEM_CHARS = 4000  # 题面投喂上限（评委需题面才能评「问题理解」，但非全文必投）
MAX_REASON_HINT = 60  # 评分理由长度提示（进 prompt，避免评委写小作文）

_FORMAT_EXAMPLE = '{"scores":[{"dimension":"<维度名>","score":0.0,"reason":"<简短理由>"}]}'

_NOISE_FIELDS = frozenset(
    {"usage", "model", "duration_ms", "paper_path", "trace_id", "timestamp", "event_seq"}
)

# 各阶段供评审的字段（只投语义产物，运行元数据不进 prompt）
_REVIEW_FIELDS: dict[str, tuple[str, ...]] = {
    "analysis": ("insights", "question_focused"),
    "modeling": ("assumptions", "objective", "modeling_detail", "variables"),
    "solving": (
        "mode", "status", "results", "stdout", "exit_code", "timed_out",
        "repair_count", "dispatch_count", "limitations", "artifacts",
    ),
    "writing": ("paper_md",),
}

# 跨阶段上下文：题面与标题（回归驱动与生产装配都可注入到 artifact）
_CONTEXT_FIELDS = ("title", "problem_text")

_SYSTEM_PROMPT = (
    "你是数学建模竞赛流程中的质量门禁评委。"
    "只输出一个 JSON 对象，不要解释文字、不要 Markdown 代码块。"
    "评分只依据给定的题面与阶段产物，不得假设未提供的内容；"
    "产物被截断时只评价已给出的部分，不因截断本身扣分。"
)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（已截断，原文 {len(text)} 字符）"


def _rubric_block(rubric: Rubric) -> str:
    lines = [
        f"【评审阶段】{rubric.stage}（rubric v{rubric.version}）",
        f"【通过阈值】{rubric.threshold:.2f}（加权总分 = Σ 维度分 × 权重）",
        "【评分维度】每维度给 0~1 的分；维度名必须逐字一致；下列维度都要评分：",
    ]
    for dimension in rubric.dimensions:
        lines.append(
            f"- {dimension.name}（权重 {dimension.weight:.2f}）：{dimension.description}"
        )
    return "\n".join(lines)


def _review_payload(artifact: dict[str, Any], max_artifact_chars: int) -> str:
    """产物投喂体：阶段字段白名单优先，白名单全缺则退化为「非噪声字段」全量。"""
    stage = str(artifact.get("stage", ""))
    whitelist = _REVIEW_FIELDS.get(stage, ())
    selected = {key: artifact[key] for key in whitelist if key in artifact}
    if not selected:
        selected = {
            key: value
            for key, value in artifact.items()
            if key not in _NOISE_FIELDS and key not in _CONTEXT_FIELDS
        }
    text = json.dumps(selected, ensure_ascii=False, indent=2, default=str)
    return _clip(text, max_artifact_chars)


def build_judge_prompt(
    artifact: dict[str, Any], rubric: Rubric, *, max_artifact_chars: int = MAX_ARTIFACT_CHARS
) -> list[dict[str, str]]:
    """构造评委请求消息（system 定纪律，user 携 rubric + 题面 + 产物 + 输出格式）。"""
    parts = [_rubric_block(rubric)]

    title = str(artifact.get("title", "")).strip()
    problem = str(artifact.get("problem_text", "")).strip()
    if title or problem:
        heading = f"【题面】{title}" if title else "【题面】"
        parts.append(f"{heading}\n{_clip(problem, MAX_PROBLEM_CHARS) if problem else '（未提供题面）'}")

    parts.append(f"【阶段产物】\n{_review_payload(artifact, max_artifact_chars)}")
    parts.append(
        "【输出格式】\n"
        f"{_FORMAT_EXAMPLE}\n"
        "score 必须是 0~1 的数值；维度名必须逐字一致且不得遗漏；"
        f"reason 每条不超过 {MAX_REASON_HINT} 字，指出具体缺陷。"
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def extract_json_object(text: str) -> str:
    """剥掉 JSON 对象本体外层的包裹文本（围栏/前后缀说明）。

    外层包裹是厂商输出习惯而非质量问题；找不到对象时原样返回，交由 GateRunner
    按红线判不通过——评委层不替模型编造评分。
    """
    stripped = text.strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        return text
    return stripped[start : end + 1]


@dataclass(slots=True)
class LlmRubricEvaluator:
    """Evaluator 端口的生产实现：把阶段产物交 BYOK 模型按 rubric 打分。"""

    llm: JudgeLLM
    max_artifact_chars: int = MAX_ARTIFACT_CHARS
    usage_sink: Callable[[dict[str, Any]], None] | None = None

    async def evaluate(self, stage: str, artifact: dict[str, Any], rubric: Rubric) -> str:
        """返回评委的评分 JSON 文本（解析与重试归 GateRunner）。"""
        if rubric.stage != stage:
            raise ValueError(f"rubric 阶段错配：rubric={rubric.stage}，待评阶段={stage}")
        messages = build_judge_prompt(
            artifact, rubric, max_artifact_chars=self.max_artifact_chars
        )
        reply = await self.llm(messages, stage)
        model = str(reply.get("model", "unknown"))
        if self.usage_sink is not None:
            self.usage_sink(
                {"stage": stage, "model": model, "usage": dict(reply.get("usage") or {})}
            )
        content = reply.get("content")
        if not isinstance(content, str):
            raise ValueError(f"评委应答缺少文本 content（model={model}）")
        return extract_json_object(content)
