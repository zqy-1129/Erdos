"""rubric schema（SP1-3）：维度/权重/阈值，YAML 配置加载。

rubric 以 YAML 文件维护、版本化、随包分发，非开发人员可打开阅读。
"""

from dataclasses import dataclass
from pathlib import Path

import yaml  # pyyaml 无类型 stub（mypy override 已忽略缺失导入）


@dataclass(frozen=True, slots=True)
class Dimension:
    """单个评审维度。"""

    name: str
    weight: float
    description: str


@dataclass(frozen=True, slots=True)
class Rubric:
    """一份阶段评审 rubric。"""

    version: int
    stage: str
    threshold: float
    dimensions: tuple[Dimension, ...]

    def validate(self) -> None:
        """校验权重和为 1、维度非空、阈值在 (0,1]。"""
        if not self.dimensions:
            raise ValueError("rubric 至少需要一个维度")
        total = sum(d.weight for d in self.dimensions)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"rubric 权重之和必须为 1，实际 {total}")
        if not 0.0 < self.threshold <= 1.0:
            raise ValueError(f"threshold 必须在 (0,1]，实际 {self.threshold}")


def load_rubric(path: str | Path) -> Rubric:
    """从 YAML 文件加载 rubric 并校验。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    dimensions = tuple(
        Dimension(name=d["name"], weight=float(d["weight"]), description=d.get("description", ""))
        for d in raw["dimensions"]
    )
    rubric = Rubric(
        version=int(raw["version"]),
        stage=str(raw["stage"]),
        threshold=float(raw["threshold"]),
        dimensions=dimensions,
    )
    rubric.validate()
    return rubric


def load_rubric_for_stage(stage: str, rubrics_dir: str | Path) -> Rubric:
    """按阶段加载对应 rubric（analysis/modeling/solving/writing）。"""
    path = Path(rubrics_dir) / f"{stage}.yaml"
    if not path.exists():
        raise ValueError(f"未找到阶段 rubric：{path}")
    return load_rubric(path)
