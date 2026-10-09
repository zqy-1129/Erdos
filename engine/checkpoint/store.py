"""SQLite 检查点存储（SP1-2）：阶段粒度，断点续跑。

对齐《数据模型设计》stage_runs.checkpoint_data（jsonb）：
- 阶段粒度：每个阶段完成时落盘（task_id + stage + status + step + data）；
- 恢复时按最近完成的阶段定位，不重算已完成阶段；
- 任务题面（title/problem_text）同库持久化（R1：崩溃/重启后不丢题面，start_stage 水合恢复）。

红线：检查点不落 Key 明文（SP1-2 禁止项，此处数据仅编排状态与题面，无敏感字段）。
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class CheckpointRecord:
    """单条检查点记录。"""

    task_id: str
    stage: str
    status: str
    step: int
    data: dict[str, Any] = field(default_factory=dict)


class SQLiteCheckpointStore:
    """SQLite 检查点存储：表 checkpoints(task_id, stage, status, step, data, updated_at)
    + task_inputs(task_id, title, problem_text, updated_at)。

    同一 (task_id, stage) 更新覆盖（阶段粒度幂等）；题面按 task_id 更新覆盖。
    并发：busy_timeout=3000 等待短时写锁（联调/诊断进程只读并发读不阻塞引擎写入）。
    保留策略：题面随 home 长期保留（断点恢复依据），不做逐任务清理——清理随 home 生命周期。
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA busy_timeout = 3000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                task_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                step INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (task_id, stage)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_inputs (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                problem_text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def save_stage(self, task_id: str, stage: str, status: str, step: int, data: dict) -> None:
        """保存阶段检查点（覆盖更新）。"""
        updated_at = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT INTO checkpoints (task_id, stage, status, step, data, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, stage) DO UPDATE SET
                status = excluded.status,
                step = excluded.step,
                data = excluded.data,
                updated_at = excluded.updated_at
            """,
            (task_id, stage, status, step, json.dumps(data, ensure_ascii=False), updated_at),
        )
        self._conn.commit()

    def load_stage(self, task_id: str, stage: str) -> CheckpointRecord | None:
        """读取某阶段的检查点。"""
        row = self._conn.execute(
            "SELECT stage, status, step, data FROM checkpoints WHERE task_id = ? AND stage = ?",
            (task_id, stage),
        ).fetchone()
        if row is None:
            return None
        return CheckpointRecord(task_id=task_id, stage=row[0], status=row[1], step=row[2], data=json.loads(row[3]))

    def save_task_input(self, task_id: str, title: str, problem_text: str) -> None:
        """保存任务题面（按 task_id 覆盖更新）。

        与阶段检查点同库落盘：重启后 start_stage 据此水合 state.tasks，
        保证断点续跑使用原始题面（而非空题面兼容回退）。
        """
        updated_at = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT INTO task_inputs (task_id, title, problem_text, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                title = excluded.title,
                problem_text = excluded.problem_text,
                updated_at = excluded.updated_at
            """,
            (task_id, title, problem_text, updated_at),
        )
        self._conn.commit()

    def load_task_input(self, task_id: str) -> dict[str, str] | None:
        """读取任务题面；从未登记返回 None（调用方决定无题面兼容回退）。"""
        row = self._conn.execute(
            "SELECT title, problem_text FROM task_inputs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return {"title": row[0], "problem_text": row[1]}

    def delete_stage(self, task_id: str, stage: str) -> None:
        """删除某阶段检查点（门禁驳回重跑时调用）。"""
        self._conn.execute(
            "DELETE FROM checkpoints WHERE task_id = ? AND stage = ?",
            (task_id, stage),
        )
        self._conn.commit()

    def completed_stages(self, task_id: str) -> list[CheckpointRecord]:
        """按落库顺序返回该任务全部阶段检查点记录（恢复用）。

        status 含两态（SP1-7 门禁语义）：stage_done=执行完成门禁未决、
        done=门禁通过；消费方（orchestrator.restore / rpc_flow）按状态定位。
        """
        rows = self._conn.execute(
            "SELECT stage, status, step, data FROM checkpoints WHERE task_id = ? ORDER BY updated_at",
            (task_id,),
        ).fetchall()
        return [CheckpointRecord(task_id=task_id, stage=r[0], status=r[1], step=r[2], data=json.loads(r[3])) for r in rows]

    def close(self) -> None:
        self._conn.close()
