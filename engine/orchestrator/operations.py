"""副作用幂等日志（EN-LOOP W11；DEC-005：恢复不重放已执行副作用）。

语义（《引擎开发详细方案》§7.3）：
- 键 (task_id, stage, attempt, exec_seq) 全局唯一；exec_seq 在一次求解尝试内自增；
- **键定位、内容定性**（G53）：键只说明"这是本次 attempt 的第 N 次派发"，不说明"这次派发要跑
  哪段代码"。复用前还必须比对 `request_digest`（工具名 + 参数原文的摘要）——恢复重跑时模型
  在同一个位置上给出的往往是**另一段代码**，跳过执行就等于把上一个程序的输出当成这次的结果；
- find：执行前查询——status="done" **且请求内容一致** 且 result_ref 存在 → 跳过执行直接复用引用；
  引用产物带哈希时校验一致性（EC-T4），缺失/不一致按损坏重执行；
  摘要不一致或旧库无摘要（无法证明是同一请求）→ 按未执行处理，宁可重跑一次沙箱，
  不可把没跑成的当跑成了；
- record_done：**仅执行成功后登记**（失败不入账——status 只有 done 一个写入方，登记即宣称
  副作用已完成），**first-wins**（同键重放不覆盖，防半写覆盖终态）；
  哈希校验失败后的修复重执行、以及同键换了请求的重执行，经 force=True 刷新摘要/引用与哈希
  （终态保持 done）；
- 崩溃点矩阵（手册 W11 验收③）：执行前崩溃=无记录（重执行）/ 执行后未记=无记录
  （重执行，代价可接受）/ 记录后崩溃=find 命中且摘要一致（跳过）。record 与产物落盘的
  同事务性由调用方保证：先写产物文件，再 record_done，再更新 checkpoint。

账本不是留痕：`operations.db` 服务的是崩溃恢复去重，同键被 force 刷新不影响合规可见性——
每次真实派发都在 `audit.db` 的 tool_call 里逐条追加（W23），审计数的是那条，不是这张表。
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


def request_digest(name: str, arguments_json: str) -> str:
    """一次工具请求的内容摘要（复用判据的单源）。

    取**参数原文**而不是规范化后的 JSON：真实厂商两次给出的键序/空白可能不同，摘要因此可能
    不同——多跑一次沙箱是可以承受的代价，把"没跑过"说成"跑过"不可以。判据要朝可承受的那一侧坏。
    """
    # str.encode() 默认 UTF-8（语言规范保证），跨平台跨版本同输入必得同摘要
    return hashlib.sha256(f"{name}\n{arguments_json}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """一条已登记的副作用记录。"""

    task_id: str
    stage: str
    attempt: int
    exec_seq: int
    status: str  # 目前只有 done 一个写入方；非 done 一律按未完成处理（防御：将来若引入过程态不会误判为已执行）
    result_ref: str | None
    result_sha256: str | None = None  # 引用产物内容哈希（EC-T4；无产物引用时为 None）
    request_digest: str | None = None  # 本次副作用对应的请求内容摘要（G53；旧库行为 None）

    def same_request(self, digest: str) -> bool:
        """账上这条记录是否就是"这一次"请求的副作用。

        None（迁移前的旧行）返回 False：无法证明是同一副作用时按未执行处理，与"非 done 按
        未完成处理"同口径。
        """
        return self.request_digest is not None and self.request_digest == digest


class OperationLog:
    """SQLite 副作用日志：append + first-wins 覆盖语义，无删除通道。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        # 与 checkpoints.db / audit.db 对称：幂等账本写失败会让恢复判定退回"未执行"，
        # 不该因瞬时锁竞争把它变成随机红灯。
        self._conn.execute("PRAGMA busy_timeout = 3000")
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
        # 轻量列迁移：旧库无 result_sha256（EC-T4）/ request_digest（G53）时补齐
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(operations)")}
        if "result_sha256" not in columns:
            self._conn.execute("ALTER TABLE operations ADD COLUMN result_sha256 TEXT")
        if "request_digest" not in columns:
            self._conn.execute("ALTER TABLE operations ADD COLUMN request_digest TEXT")
        self._conn.commit()

    def find(
        self, task_id: str, stage: str, attempt: int, exec_seq: int
    ) -> OperationRecord | None:
        """按幂等键查询；无记录、或状态非 done（半写/将来引入的过程态）→ None（按需重执行）。

        返回的记录**尚未判定请求内容**——复用前调用方必须再过 `same_request()`。
        """
        cursor = self._conn.execute(
            "SELECT task_id, stage, attempt, exec_seq, status, result_ref, result_sha256, request_digest "
            "FROM operations WHERE task_id=? AND stage=? AND attempt=? AND exec_seq=?",
            (task_id, stage, attempt, exec_seq),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        record = OperationRecord(
            task_id=row[0], stage=row[1], attempt=row[2], exec_seq=row[3],
            status=row[4], result_ref=row[5], result_sha256=row[6], request_digest=row[7],
        )
        if record.status != "done":
            return None  # 非 done 按未完成处理（崩溃点矩阵：宁可重跑，不可把没做完的当做完）
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
        request_digest: str | None = None,
    ) -> None:
        """登记终态（first-wins：同键已存在时不覆盖）；**只在执行成功后调用**。

        force=True 用于"同键要换新内容"的两种情形：EC-T4 引用产物哈希校验失败后的修复重执行、
        G53 同键但请求内容不同后的重执行——刷新摘要/引用与哈希，终态保持 done（正常重放路径仍
        first-wins，不覆盖）。
        """
        now = datetime.now(UTC).isoformat()
        if force:
            self._conn.execute(
                """
                INSERT INTO operations (task_id, stage, attempt, exec_seq, status, result_ref, created_at, result_sha256, request_digest)
                VALUES (?, ?, ?, ?, 'done', ?, ?, ?, ?)
                ON CONFLICT(task_id, stage, attempt, exec_seq) DO UPDATE SET
                    result_ref = excluded.result_ref,
                    result_sha256 = excluded.result_sha256,
                    request_digest = excluded.request_digest,
                    created_at = excluded.created_at
                """,
                (task_id, stage, attempt, exec_seq, result_ref, now, result_sha256, request_digest),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO operations (task_id, stage, attempt, exec_seq, status, result_ref, created_at, result_sha256, request_digest)
                VALUES (?, ?, ?, ?, 'done', ?, ?, ?, ?)
                ON CONFLICT(task_id, stage, attempt, exec_seq) DO NOTHING
                """,
                (task_id, stage, attempt, exec_seq, result_ref, now, result_sha256, request_digest),
            )
        self._conn.commit()

    def records(self, task_id: str, stage: str | None = None) -> list[OperationRecord]:
        """按任务（可选阶段）查询全部记录（对账/验收用）。

        排序必须带上 `attempt`：主键是四元组，同一 `exec_seq` 会在不同尝试里重复出现，
        只按 exec_seq 排会把两次尝试的流水交错成一条假时间线（"第 3 次派发"到底属于哪次
        尝试，正是恢复对账要问的问题）。
        """
        order = " ORDER BY attempt, exec_seq"
        select = (
            "SELECT task_id, stage, attempt, exec_seq, status, result_ref, result_sha256, request_digest "
            "FROM operations"
        )
        if stage is None:
            rows = self._conn.execute(select + " WHERE task_id=?" + order, (task_id,)).fetchall()
        else:
            rows = self._conn.execute(select + " WHERE task_id=? AND stage=?" + order, (task_id, stage)).fetchall()
        return [
            OperationRecord(
                task_id=r[0], stage=r[1], attempt=r[2], exec_seq=r[3],
                status=r[4], result_ref=r[5], result_sha256=r[6], request_digest=r[7],
            )
            for r in rows
        ]

