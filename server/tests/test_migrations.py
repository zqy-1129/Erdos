"""Alembic 迁移测试：升级/回滚双驱动 + 模型-迁移一致性断言。"""

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.repository.models import (
    Account,
    AuditLog,
    AuthRefreshToken,
    Case,
    ContentManifest,
    Device,
    EntitlementSnapshot,
    MonitoringMinuteSnapshot,
    NotificationSendLog,
    Order,
    PaperTemplate,
    PaymentCallback,
    PointAccount,
    PointLedger,
    Problem,
    Product,
    SchedulerRun,
    StageGrant,
    Subscription,
    TelemetryEventRecord,
)

SERVER_ROOT = Path(__file__).resolve().parents[1]


def _config(db_url: str) -> Config:
    cfg = Config(str(SERVER_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVER_ROOT / "app" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_upgrade_head_creates_schema_consistent_with_model(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    command.upgrade(_config(f"sqlite+aiosqlite:///{db_file}"), "head")

    conn = sqlite3.connect(db_file)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_logs)")}
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(audit_logs)")
        }
    finally:
        conn.close()

    # 迁移产物与 ORM 模型列 100% 一致（表结构为契约冻结项）
    assert columns == set(AuditLog.__table__.columns.keys())
    # 模型声明的索引必须存在于迁移产物
    assert {"ix_audit_logs_actor_id", "ix_audit_logs_action", "ix_audit_logs_actor_type"} <= indexes


def test_monitoring_table_matches_model(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    command.upgrade(_config(f"sqlite+aiosqlite:///{db_file}"), "head")

    conn = sqlite3.connect(db_file)
    try:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(monitoring_minute_snapshots)")
        }
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(monitoring_minute_snapshots)")
        }
    finally:
        conn.close()

    assert columns == set(MonitoringMinuteSnapshot.__table__.columns.keys())
    assert "ix_monitoring_minute_snapshots_minute_ts" in indexes


def test_auth_refresh_tokens_table_matches_model(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    command.upgrade(_config(f"sqlite+aiosqlite:///{db_file}"), "head")

    conn = sqlite3.connect(db_file)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(auth_refresh_tokens)")
        }
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(auth_refresh_tokens)")
        }
    finally:
        conn.close()

    assert columns == set(AuthRefreshToken.__table__.columns.keys())
    assert {
        "ix_auth_refresh_tokens_token_hash",
        "ix_auth_refresh_tokens_user_id",
        "ix_auth_refresh_tokens_device_id",
        "ix_auth_refresh_tokens_expires_at",
        "ix_auth_refresh_tokens_revoked_at",
    } <= indexes


def test_account_domain_tables_match_models(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    command.upgrade(_config(f"sqlite+aiosqlite:///{db_file}"), "head")

    conn = sqlite3.connect(db_file)
    try:
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        device_columns = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
        device_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(devices)")
        }
    finally:
        conn.close()

    assert user_columns == set(Account.__table__.columns.keys())
    assert device_columns == set(Device.__table__.columns.keys())
    assert {"ix_devices_user_id", "ix_devices_fingerprint"} <= device_indexes


def test_points_domain_tables_match_models(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    command.upgrade(_config(f"sqlite+aiosqlite:///{db_file}"), "head")

    conn = sqlite3.connect(db_file)
    try:
        account_cols = {row[1] for row in conn.execute("PRAGMA table_info(point_accounts)")}
        ledger_cols = {row[1] for row in conn.execute("PRAGMA table_info(point_ledgers)")}
        grant_cols = {row[1] for row in conn.execute("PRAGMA table_info(stage_grants)")}
        ledger_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(point_ledgers)")
        }
    finally:
        conn.close()

    assert account_cols == set(PointAccount.__table__.columns.keys())
    assert ledger_cols == set(PointLedger.__table__.columns.keys())
    assert grant_cols == set(StageGrant.__table__.columns.keys())
    assert {"ix_point_ledgers_user_id", "ix_point_ledgers_exec_id"} <= ledger_indexes


def test_billing_domain_tables_match_models(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    command.upgrade(_config(f"sqlite+aiosqlite:///{db_file}"), "head")

    conn = sqlite3.connect(db_file)
    try:
        product_cols = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
        order_cols = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        sub_cols = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)")}
        cb_cols = {row[1] for row in conn.execute("PRAGMA table_info(payment_callbacks)")}
        order_indexes = {row[1] for row in conn.execute("PRAGMA index_list(orders)")}
    finally:
        conn.close()

    assert product_cols == set(Product.__table__.columns.keys())
    assert order_cols == set(Order.__table__.columns.keys())
    assert sub_cols == set(Subscription.__table__.columns.keys())
    assert cb_cols == set(PaymentCallback.__table__.columns.keys())
    assert {"ix_orders_user_id", "ix_orders_status"} <= order_indexes


def test_entitlement_content_tables_match_models(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    command.upgrade(_config(f"sqlite+aiosqlite:///{db_file}"), "head")

    conn = sqlite3.connect(db_file)
    try:
        snap_cols = {row[1] for row in conn.execute("PRAGMA table_info(entitlement_snapshots)")}
        problem_cols = {row[1] for row in conn.execute("PRAGMA table_info(problems)")}
        tpl_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_templates)")}
        case_cols = {row[1] for row in conn.execute("PRAGMA table_info(cases)")}
        manifest_cols = {row[1] for row in conn.execute("PRAGMA table_info(content_manifests)")}
    finally:
        conn.close()

    assert snap_cols == set(EntitlementSnapshot.__table__.columns.keys())
    assert problem_cols == set(Problem.__table__.columns.keys())
    assert tpl_cols == set(PaperTemplate.__table__.columns.keys())
    assert case_cols == set(Case.__table__.columns.keys())
    assert manifest_cols == set(ContentManifest.__table__.columns.keys())


def test_notification_scheduler_tables_match_models(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    command.upgrade(_config(f"sqlite+aiosqlite:///{db_file}"), "head")

    conn = sqlite3.connect(db_file)
    try:
        log_cols = {row[1] for row in conn.execute("PRAGMA table_info(notification_send_logs)")}
        run_cols = {row[1] for row in conn.execute("PRAGMA table_info(scheduler_runs)")}
    finally:
        conn.close()

    assert log_cols == set(NotificationSendLog.__table__.columns.keys())
    assert run_cols == set(SchedulerRun.__table__.columns.keys())


def test_telemetry_table_matches_model(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    command.upgrade(_config(f"sqlite+aiosqlite:///{db_file}"), "head")

    conn = sqlite3.connect(db_file)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(telemetry_events)")}
    finally:
        conn.close()

    assert cols == set(TelemetryEventRecord.__table__.columns.keys())


def test_downgrade_base_removes_table(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    cfg = _config(f"sqlite+aiosqlite:///{db_file}")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    conn = sqlite3.connect(db_file)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "audit_logs" not in tables
    assert "monitoring_minute_snapshots" not in tables
    assert "auth_refresh_tokens" not in tables
    assert "users" not in tables
    assert "devices" not in tables
    assert "point_accounts" not in tables
    assert "point_ledgers" not in tables
    assert "stage_grants" not in tables
    assert "products" not in tables
    assert "orders" not in tables
    assert "subscriptions" not in tables
    assert "payment_callbacks" not in tables
    assert "problems" not in tables
    assert "paper_templates" not in tables
    assert "cases" not in tables
    assert "content_manifests" not in tables
    assert "entitlement_snapshots" not in tables
    assert "notification_send_logs" not in tables
    assert "scheduler_runs" not in tables
    assert "telemetry_events" not in tables


def test_offline_sql_generation(capsys) -> None:
    """离线模式：PostgreSQL 方言下生成 SQL 脚本（验证 PG 驱动解析无碍）。"""
    cfg = _config("postgresql+asyncpg://u:p@localhost:5432/erdos")
    command.upgrade(cfg, "head", sql=True)
    script = capsys.readouterr().out.replace("\n", " ")
    assert "CREATE TABLE audit_logs" in script
    assert "postgresql" in cfg.get_main_option("sqlalchemy.url")