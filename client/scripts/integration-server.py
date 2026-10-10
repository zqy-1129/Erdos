"""客户端真实 HTTP 联调夹具：仅限回环地址与新建临时数据库。"""

import asyncio
import socket
import sys
from pathlib import Path

repo_root, evidence_root = (Path(value).resolve() for value in sys.argv[1:3])
sys.path.insert(0, str(repo_root / "server"))

import uvicorn  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repository.models import Base, Product  # noqa: E402

settings = Settings(
    _env_file=None,
    env="dev",
    database_url=f"sqlite+aiosqlite:///{evidence_root / 'test.db'}",
    auth_enforce=True,
    scheduler_enabled=False,
    order_reconcile_interval_seconds=0,
    rate_limit_requests=2000,
    audit_rate_limit_requests=2000,
)
app = create_app(settings)


async def prepare():
    async with app.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with app.state.session_factory() as session:
        session.add(
            Product(
                code="unit_pack",
                type="points_pack",
                name="Unit Pack",
                price_cents=100,
                points=50,
                duration_days=0,
                active=True,
            )
        )
        await session.commit()


asyncio.run(prepare())
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen(128)
print(f"ERDOS_TEST_SERVER_URL=http://127.0.0.1:{listener.getsockname()[1]}", flush=True)
uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[listener])
