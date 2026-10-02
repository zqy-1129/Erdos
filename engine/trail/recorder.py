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


class TrailRecorder:
    """四类事件记录器：model_call / tool_call / artifact / manual_edit。"""

    def __init__(self, store: TrailStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # 四类事件采集
    # ------------------------------------------------------------------
    def record_model_call(
        self, task_id: str, stage: str, model: str, usage: dict[str, Any], duration_ms: float
    ) -> int:
        """模型调用留痕：模型名/token 用量/耗时。"""
        return self._store.append_event(
            task_id, stage, EventType.MODEL_CALL,
            detail={"model": model, "usage": usage, "duration_ms": duration_ms},
        )

    def record_tool_call(self, task_id: str, stage: str, tool: str, detail: dict[str, Any]) -> int:
        """工具调用留痕：工具名 + 明细。"""
        return self._store.append_event(
            task_id, stage, EventType.TOOL_CALL,
            detail={"tool": tool, **detail},
        )

    def record_artifact(self, task_id: str, stage: str, kind: str, file_path: str | Path) -> int:
        """产物留痕：计算 sha256 并登记 artifact_index。"""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"产物不存在：{path}")
        data = path.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        size = len(data)
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


def sha256_of(path: str | Path) -> str:
    """计算文件 sha256（合规声明导出用）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
