"""配置加载测试（环境变量覆盖与默认值）。"""

from app.core.config import Settings


def test_defaults() -> None:
    cfg = Settings()
    assert cfg.database_url.startswith("sqlite")
    assert cfg.request_id_header == "X-Request-Id"
    assert cfg.auth_enforce is False
    assert cfg.metrics_enabled is True


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("ERDOS_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/erdos")
    monkeypatch.setenv("ERDOS_AUTH_ENFORCE", "true")
    monkeypatch.setenv("ERDOS_RATE_LIMIT_REQUESTS", "42")
    cfg = Settings()
    assert cfg.database_url == "postgresql+asyncpg://u:p@db:5432/erdos"
    assert cfg.auth_enforce is True
    assert cfg.rate_limit_requests == 42


def test_extra_env_ignored(monkeypatch) -> None:
    monkeypatch.setenv("ERDOS_UNKNOWN_FIELD", "x")
    cfg = Settings()  # extra="ignore"：未知项不得报错
    assert "unknown_field" not in cfg.model_dump()