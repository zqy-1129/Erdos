"""留痕记录器（SP1-6）：四类事件采集 + 产物 sha256 计算。

红线（SP1-6 提示词）：
- 不可关闭（无开关）；
- 留痕写入失败让任务失败（抛异常，非静默丢弃）；
- 留痕内容不上传云端或遥测（本地 SQLite）。
"""

import hashlib
from pathlib import Path
from typing import Any

from engine.trail.store import EventType, TrailStore

# 沙箱产物扩展名 → 契约 artifact enum（figure/data/table/code/paper）；认不出的一律不登记
_ARTIFACT_KIND_BY_SUFFIX = {
    ".png": "figure", ".jpg": "figure", ".jpeg": "figure", ".svg": "figure",
    ".csv": "data", ".tsv": "data", ".json": "data", ".xlsx": "data", ".npy": "data",
    ".md": "paper",
}


class TrailRecorder:
    """四类事件记录器：model_call / tool_call / artifact / manual_edit。"""

    def __init__(self, store: TrailStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # 四类事件采集
    # ------------------------------------------------------------------
    def record_model_call(
        self,
        task_id: str,
        stage: str,
        model: str,
        usage: dict[str, Any],
        duration_ms: float | None = None,
    ) -> int:
        """模型调用留痕：模型名/token 用量/耗时。

        耗时不可得时不写该字段——写 0 与「真的很快」在留痕里不可区分（F-007 精度）。
        """
        detail: dict[str, Any] = {"model": model, "usage": usage}
        if duration_ms is not None:
            detail["duration_ms"] = duration_ms
        return self._store.append_event(task_id, stage, EventType.MODEL_CALL, detail=detail)

    def record_tool_call(self, task_id: str, stage: str, tool: str, detail: dict[str, Any]) -> int:
        """工具调用留痕：工具名 + 明细。"""
        return self._store.append_event(
            task_id, stage, EventType.TOOL_CALL,
            detail={"tool": tool, **detail},
        )

    def record_artifact(self, task_id: str, stage: str, kind: str, file_path: str | Path) -> int:
        """产物留痕：算 sha256，并把"索引行 + 事件行"**在一个事务里**落库。

        这两条是一件事的两半（合规声明引用 `artifact_index` 的哈希，审计流水看 `audit_trail`
        的事件），分开提交就留了一个窗口：崩溃或第二条失败会得到"有支撑材料却查无流水"。
        批内失败会整体回滚，任务照样按红线失败——**要么两条都在，要么两条都不在**。
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"产物不存在：{path}")
        data = path.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        size = len(data)
        with self._store.batch():
            # 1. artifact_index 登记（sha256 支撑合规声明导出）
            self._store.append_artifact(task_id, stage, kind, str(path), sha256, size)
            # 2. audit_trail 记录 artifact 事件
            return self._store.append_event(
                task_id, stage, EventType.ARTIFACT,
                detail={"kind": kind, "file_path": str(path), "sha256": sha256, "size_bytes": size},
            )

    def record_manual_edit(self, task_id: str, stage: str, note: str) -> int:
        """人工修改留痕：人工标记说明。"""
        return self._store.append_event(
            task_id, stage, EventType.MANUAL_EDIT,
            detail={"note": note},
        )


def kind_for_file(path: str | Path) -> str | None:
    """按扩展名给沙箱产物归类（契约 `artifact.ready.artifact` enum 同源）。

    认不出来的**返回 None 而不是兜底成 "data"**：登记一个错类别等于编造证据，
    而少登记一条只是少一条支撑材料（合规声明按实际登记的哈希出）。
    """
    suffix = Path(path).suffix.lower()
    return _ARTIFACT_KIND_BY_SUFFIX.get(suffix)


def sha256_of(path: str | Path) -> str:
    """计算文件 sha256（合规声明导出用）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
