"""副作用幂等日志（EN-LOOP W11；DEC-005：恢复不重放已执行副作用）。

语义（《引擎开发详细方案》§7.3）：
- 键 (task_id, stage, attempt, exec_seq) 全局唯一；exec_seq 在一次求解尝试内自增；
- find：执行前查询——status="done" 且 result_ref 存在 → 跳过执行直接复用引用；
  引用产物带哈希时校验一致性（EC-T4），缺失/不一致按损坏重执行；
- record_done：执行成功后登记，**first-wins**（同键重放不覆盖，防半写覆盖终态）；
  哈希校验失败后的修复重执行经 force=True 刷新引用与哈希（终态保持 done）；
- 崩溃点矩阵（手册 W11 验收③）：执行前崩溃=无记录（重执行）/ 执行后未记=无记录
  （重执行，代价可接受）/ 记录后崩溃=find 命中（跳过）。record 与产物落盘的
  同事务性由调用方保证：先写产物文件，再 record_done，再更新 checkpoint。
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """一条已登记的副作用记录。"""

    task_id: str
    stage: str
    attempt: int
    exec_seq: int
    status: str  # done（终态）；running 为过程态，恢复时按未完成处理
    result_ref: str | None
    result_sha256: str | None = None  # 引用产物内容哈希（EC-T4；无产物引用时为 None）


class OperationLog:
    """SQLite 副作用日志：append + first-wins 覆盖语义，无删除通道。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operations (
                task_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                exec_seq INTEGER NOT NULL,
                status TEXT NOT NULL,
                result_ref TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (task_id, stage, attempt, exec_seq)
            )
            """
        )
        # 轻量列迁移（EC-T4）：旧库无 result_sha256 列时补齐
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(operations)")}
        if "result_sha256" not in columns:
            self._conn.execute("ALTER TABLE operations ADD COLUMN result_sha256 TEXT")
        self._conn.commit()

    def find(
        self, task_id: str, stage: str, attempt: int, exec_seq: int
    ) -> OperationRecord | None:
        """按幂等键查询；无记录/仅 running（半写）→ None（按需重执行）。"""
        cursor = self._conn.execute(
            "SELECT task_id, stage, attempt, exec_seq, status, result_ref, result_sha256 "
            "FROM operations WHERE task_id=? AND stage=? AND attempt=? AND exec_seq=?",
            (task_id, stage, attempt, exec_seq),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        record = OperationRecord(
            task_id=row[0], stage=row[1], attempt=row[2], exec_seq=row[3],
            status=row[4], result_ref=row[5], result_sha256=row[6],
        )
        if record.status != "done":
            return None  # running 半写记录按未完成处理（崩溃点矩阵）
        return record

    def record_done(
        self,
        task_id: str,
        stage: str,
        attempt: int,
        exec_seq: int,
        result_ref: str | None,
        result_sha256: str | None = None,
        force: bool = False,
    ) -> None:
        """登记终态（first-wins：同键已存在时不覆盖）。

        force=True 仅用于 EC-T4 哈希校验失败后的修复重执行：刷新引用与哈希，
        终态保持 done（正常重放路径仍 first-wins，不覆盖）。
        """
        now = datetime.now(UTC).isoformat()
        if force:
            self._conn.execute(
                """
                INSERT INTO operations (task_id, stage, attempt, exec_seq, status, result_ref, created_at, result_sha256)
                VALUES (?, ?, ?, ?, 'done', ?, ?, ?)
                ON CONFLICT(task_id, stage, attempt, exec_seq) DO UPDATE SET
                    result_ref = excluded.result_ref,
                    result_sha256 = excluded.result_sha256,
                    created_at = excluded.created_at
                """,
                (task_id, stage, attempt, exec_seq, result_ref, now, result_sha256),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO operations (task_id, stage, attempt, exec_seq, status, result_ref, created_at, result_sha256)
                VALUES (?, ?, ?, ?, 'done', ?, ?, ?)
                ON CONFLICT(task_id, stage, attempt, exec_seq) DO NOTHING
                """,
                (task_id, stage, attempt, exec_seq, result_ref, now, result_sha256),
            )
        self._conn.commit()

    def records(self, task_id: str, stage: str | None = None) -> list[OperationRecord]:
        """按任务（可选阶段）查询全部记录（对账/验收用）。"""
        if stage is None:
            cursor = self._conn.execute(
                "SELECT task_id, stage, attempt, exec_seq, status, result_ref, result_sha256 "
                "FROM operations WHERE task_id=? ORDER BY exec_seq",
                (task_id,),
            )
        else:
            cursor = self._conn.execute(
                "SELECT task_id, stage, attempt, exec_seq, status, result_ref, result_sha256 "
                "FROM operations WHERE task_id=? AND stage=? ORDER BY exec_seq",
                (task_id, stage),
            )
        return [
            OperationRecord(
                task_id=r[0], stage=r[1], attempt=r[2], exec_seq=r[3],
                status=r[4], result_ref=r[5], result_sha256=r[6],
            )
            for r in cursor.fetchall()
        ]
