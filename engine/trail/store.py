"""留痕存储（SP1-6）：SQLite 只追加，无删除通道。

对齐《数据模型设计》第 6 章：
- audit_trail（只追加）：id 自增、task_id/stage、event_type、detail(jsonb)、ts；
- artifact_index：task_id/stage/kind/file_path/sha256/size_bytes/created_at。

红线（SP1-6 提示词）：
- 只追加，无删除/清空 API；
- 留痕写入失败让任务失败（抛异常，非静默丢弃）。

一致性判据：`artifact_index` 与它对应的 `artifact` 事件必须**一起出现**（合规声明引用前者、
审计查流水看后者），所以这类成对写走 `batch()` 一个事务，不留"索引有行、流水无事件"的窗口。
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from engine.trail.redact import TRAIL_SENSITIVE_FRAGMENTS, redact_sensitive


# 四类事件类型（对齐《数据模型设计》audit_trail.event_type）
class EventType:
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    ARTIFACT = "artifact"
    MANUAL_EDIT = "manual_edit"


@dataclass(frozen=True, slots=True)
class TrailRecord:
    """一条留痕记录。"""

    id: int
    task_id: str
    stage: str
    event_type: str
    detail: dict[str, Any]
    ts: str


class TrailStore:
    """SQLite 留痕存储：audit_trail + artifact_index，只追加。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        # `batch()` 期间推迟每条写各自的 commit，改由批末一次提交（见该方法的说明）。
        self._defer_commits = False
        # 与 checkpoints.db 对称：留痕写失败按红线要让任务失败，
        # 不该由主进程跨进程只读并发把写入堵成 "database is locked"。
        self._conn.execute("PRAGMA busy_timeout = 3000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_trail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '{}',
                ts TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS artifact_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                kind TEXT NOT NULL,
                file_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # 只追加写入（无删除/清空 API）
    # ------------------------------------------------------------------
    def _commit(self) -> None:
        """单条写默认立即提交；在 `batch()` 内推迟到批末，由批统一提交或回滚。"""
        if not self._defer_commits:
            self._conn.commit()

    @contextmanager
    def batch(self) -> Iterator[None]:
        """把批内多条只追加写并进**一个事务**：要么全都落，要么全都不落。

        存在的理由是一致性判据，不是性能：`artifact_index` 是合规声明的引用来源，
        `audit_trail` 是流水。两条各提交一次就有一个崩溃/失败窗口会留下
        "索引有行、流水无事件"（G47）——那种状态比"两条都没有"更难回答：前者要解释
        这条哈希从哪来，后者只要重跑一次。批内出错必须显式 `rollback()`：连接默认开启隐式
        事务，"不提交"不等于"撤销"——留在事务里的行会被这个连接上的下一次提交带进库。
        """
        outer, self._defer_commits = self._defer_commits, True
        try:
            yield
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            self._defer_commits = outer

    def append_event(
        self, task_id: str, stage: str, event_type: str, detail: dict[str, Any], ts: str | None = None
    ) -> int:
        """追加一条留痕事件，返回自增 id。写入失败抛异常（不静默丢弃）。

        detail 落库前经 redact_sensitive 脱敏（密钥域红线：留痕不含凭据/授权片段）。
        """
        ts = ts or datetime.now(UTC).isoformat()
        safe_detail = redact_sensitive(detail, TRAIL_SENSITIVE_FRAGMENTS)
        cursor = self._conn.execute(
            "INSERT INTO audit_trail (task_id, stage, event_type, detail, ts) VALUES (?, ?, ?, ?, ?)",
            (task_id, stage, event_type, json.dumps(safe_detail, ensure_ascii=False), ts),
        )
        self._commit()
        return int(cursor.lastrowid or 0)

    def append_artifact(
        self, task_id: str, stage: str, kind: str, file_path: str, sha256: str, size_bytes: int
    ) -> int:
        """登记一条产物索引（合规声明导出以 sha256 作支撑材料引用）。"""
        created_at = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            "INSERT INTO artifact_index (task_id, stage, kind, file_path, sha256, size_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, stage, kind, file_path, sha256, size_bytes, created_at),
        )
        self._commit()
        return int(cursor.lastrowid or 0)

    # ------------------------------------------------------------------
    # 只读查询
    # ------------------------------------------------------------------
    def events(self, task_id: str) -> list[TrailRecord]:
        """按 task_id 查询留痕事件（追加序）。"""
        rows = self._conn.execute(
            "SELECT id, task_id, stage, event_type, detail, ts FROM audit_trail WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [
            TrailRecord(id=r[0], task_id=r[1], stage=r[2], event_type=r[3], detail=json.loads(r[4]), ts=r[5])
            for r in rows
        ]

    def artifacts(self, task_id: str) -> list[dict[str, Any]]:
        """按 task_id 查询产物索引。"""
        rows = self._conn.execute(
            "SELECT task_id, stage, kind, file_path, sha256, size_bytes FROM artifact_index WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        return [
            {"task_id": r[0], "stage": r[1], "kind": r[2], "file_path": r[3], "sha256": r[4], "size_bytes": r[5]}
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()
