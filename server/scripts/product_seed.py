"""商品目录灌库脚本：把 `products` 表填上，`GET /v1/billing/products` 不再为空。

为什么要有这个脚本：`products` 表此前**既无种子也无管理端写接口**，裸起服务端后商品目录是空的，
购买→支付→权益这条链在最前面一步就断（现有的 live 测试是自己 INSERT 一行绕过去的）。
三端联调（B 档）缺的就是这一件事。

目录取值有出处，不是这里编的定价：
- PRD F-005「商品体系（建议初值）」：月订阅 ¥19、年订阅 ¥128、积分包 ¥18/400、¥45/1200、¥98/3000；
- 《服务端架构与模块设计》§计费订阅服务：同一组数字，并注明月订阅含"月赠 400 分 + 内容权益"。

三条红线（脚本自己校验，不看数据文件顺不顺眼）：
① **金额一律整数分**：拒浮点、拒 bool（`True` 是 `int` 的子类，`isinstance(x, int)` 单独用会放它过去）；
② **订阅商品的 `points` 必须等于配置项 `subscription_monthly_grant_points`**：实际月赠入账读的是配置，
   目录里的 `points` 只是展示——两处不一致时页面在对资金承诺说谎，属资金域而不是显示瑕疵。
   所以这里不是"警告"，是**拒灌**，并把两个位置都点名；
③ **幂等且不覆盖运营改动**：默认 create-only，已存在且不同就只报差异；要覆盖必须显式 `--update`。
   `Product` 的模型注释写着"定价配置化，运营可后台调价"——一个每次跑都把运营改回目录值的脚本是危险的。

用法：
    <venv>/python scripts/product_seed.py --data data/billing/products.json \\
        --db sqlite+aiosqlite:///./erdos.db [--update]

前置：先 `alembic upgrade head` 建表（表不存在时本脚本给出这条提示而不是抛栈）。
"""

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from app.core.config import Settings
from app.domain.billing.ports import ProductType
from app.infra.db import create_engine, create_session_factory
from app.repository.models import Product

SEEDABLE_FIELDS = ("type", "name", "price_cents", "points", "duration_days")


class SeedError(ValueError):
    """目录数据不合法：一条都不入库。"""


@dataclass(slots=True)
class SeedResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: dict[str, dict[str, tuple[object, object]]] = field(default_factory=dict)

    def describe(self) -> str:
        diff_lines = [
            f"  {code}: " + "；".join(
                f"{k} 库内={old!r} 目录={new!r}" for k, (old, new) in sorted(fields.items())
            )
            for code, fields in sorted(self.skipped.items())
        ]
        parts = [
            f"新增 {len(self.created)}",
            f"已是最新 {len(self.unchanged)}",
            f"更新 {len(self.updated)}",
            f"跳过（库内与目录不同） {len(self.skipped)}",
        ]
        return "、".join(parts) + ("\n" + "\n".join(diff_lines) if diff_lines else "")


def validate(rows: list[dict], grant_points: int) -> None:
    """灌库前的全量校验：宁可一条都不写，也不写进一个会说谎的价格。"""
    problems: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        code = str(row.get("code", ""))
        label = code or f"第 {index + 1} 条"

        if not code or code in seen:
            problems.append(f"{label}: code 为空或文件内重复（code 是幂等键）")
        seen.add(code)

        ptype = row.get("type")
        if ptype not in {ProductType.SUBSCRIPTION, ProductType.POINTS_PACK}:
            problems.append(f"{label}: type={ptype!r} 非法（只允许 subscription / points_pack）")
            continue

        for name in ("price_cents", "points", "duration_days"):
            value = row.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                problems.append(f"{label}: {name}={value!r} 必须是整数（金额一律整数分，禁浮点/字符串）")
            elif value < 0:
                problems.append(f"{label}: {name}={value} 不能为负")

        if not str(row.get("name", "")).strip():
            problems.append(f"{label}: name 为空")

        if problems and not isinstance(row.get("price_cents"), int):
            continue  # 数值面已经错了，后面的语义判断不再有意义

        if ptype == ProductType.POINTS_PACK:
            if row.get("duration_days") != 0:
                problems.append(f"{label}: 积分包的 duration_days 必须是 0（它不过期，PRD F-005）")
            if row.get("points", 0) < 1:
                problems.append(f"{label}: 积分包面额必须 ≥1 分，否则买它没有意义")
        elif ptype == ProductType.SUBSCRIPTION:
            if row.get("duration_days", 0) < 1:
                problems.append(f"{label}: 订阅必须有周期天数（30 / 365）")
            if row.get("points") != grant_points:
                problems.append(
                    f"{label}: 目录里的月赠 points={row.get('points')!r} 与实际入账额度"
                    f" subscription_monthly_grant_points={grant_points!r} 不一致——"
                    "入账读配置、页面读目录，两处必须一起改（否则资金承诺在说谎）"
                )

    if problems:
        raise SeedError("商品目录不合法，未写入任何数据：\n  " + "\n  ".join(problems))


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise SeedError(f"找不到商品目录文件：{path}")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise SeedError(f"{path} 必须是非空的 JSON 数组")
    return rows


async def seed_products(
    session_factory, rows: list[dict], *, update: bool, grant_points: int
) -> SeedResult:
    validate(rows, grant_points)
    result = SeedResult()
    async with session_factory() as session:
        for row in rows:
            # 一律取 ORM 行而不是 ProductRecord：仓储返回的是 frozen dataclass（视图），
            # 在它上面 setattr 会抛 FrozenInstanceError——第一版就死在这里。
            existing = (
                await session.execute(select(Product).where(Product.code == row["code"]))
            ).scalar_one_or_none()
            if existing is None:
                session.add(Product(**row, active=True))
                result.created.append(row["code"])
                continue
            diffs = {
                k: (getattr(existing, k), row[k])
                for k in SEEDABLE_FIELDS
                if getattr(existing, k) != row[k]
            }
            if not diffs:
                result.unchanged.append(row["code"])
            elif update:
                for key, (_old, new) in diffs.items():
                    setattr(existing, key, new)
                result.updated.append(row["code"])
            else:
                result.skipped[row["code"]] = diffs
        await session.commit()
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description="商品目录灌库（幂等，默认不覆盖运营改动）")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--db", default="sqlite+aiosqlite:///./erdos.db")
    parser.add_argument("--update", action="store_true", help="目录覆盖库内差异（默认只报不写）")
    args = parser.parse_args()

    rows = load_rows(args.data)
    grant_points = Settings().subscription_monthly_grant_points
    engine = create_engine(args.db)
    try:
        result = await seed_products(
            create_session_factory(engine), rows, update=args.update, grant_points=grant_points
        )
    except Exception as exc:  # noqa: BLE001 - 表不存在是此处最常见的可操作失败，要给人话
        if "no such table" in str(exc).lower() or "doesn't exist" in str(exc).lower():
            print("products 表不存在：请先执行 `python -m alembic upgrade head`")
            return 2
        raise
    finally:
        await engine.dispose()

    print(f"商品目录：{result.describe()}")
    if result.skipped:
        print("（库内与目录不同且未加 --update：这些条目保持库内值不变，运营改过价就不会被覆盖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
