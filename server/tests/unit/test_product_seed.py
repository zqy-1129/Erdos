"""商品目录灌库脚本的测试（打通三端联调 B 档：目录不为空 + 幂等 + 不覆盖运营改动）。

判据分三层，缺一层都会在以后咬人：
① **资金面**：金额必须是整数分（浮点/bool/负数一律拒），订阅的展示额度必须等于实际入账配置额度；
② **幂等面**：跑两次不多一行；库内被运营改过的价格**默认不被目录覆盖**（`--update` 才覆盖）；
③ **消费面**：灌完之后 `GET /v1/billing/products` 真的能列出商品——这条链此前断在"表是空的"。
"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.domain.billing.ports import ProductType
from app.repository.billing import SQLAlchemyProductRepository
from app.repository.models import Product

SERVER_ROOT = Path(__file__).resolve().parent.parent.parent
CATALOG = SERVER_ROOT / "data" / "billing" / "products.json"
GRANT_POINTS = 400  # 与 Settings 默认值一致（PRD：订阅月赠 400 分）


@pytest.fixture(scope="module")
def product_seed():
    spec = importlib.util.spec_from_file_location(
        "product_seed", SERVER_ROOT / "scripts" / "product_seed.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows() -> list[dict]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


async def _codes(session_factory) -> dict[str, int]:
    async with session_factory() as session:
        rows = (await session.execute(select(Product.code, Product.price_cents))).all()
    return {code: price for code, price in rows}


# ----------------------------------------------------------------------
# ③ 消费面 + 基本形状
# ----------------------------------------------------------------------
async def test_shipped_catalog_seeds_five_products(product_seed, session_factory) -> None:
    result = await product_seed.seed_products(
        session_factory, _rows(), update=False, grant_points=GRANT_POINTS
    )
    assert len(result.created) == 5 and not result.skipped

    async with session_factory() as session:
        items = await SQLAlchemyProductRepository(session).list_active()
    # list_active 按价格升序：¥18 的积分包排在 ¥19 的月订阅之前，最贵的是年订阅
    assert [p.code for p in items] == ["pack_400", "sub_monthly", "pack_1200", "pack_3000", "sub_annual"]
    assert [p.price_cents for p in items] == [1800, 1900, 4500, 9800, 12800]
    assert {p.type for p in items} == {ProductType.SUBSCRIPTION, ProductType.POINTS_PACK}


async def test_api_lists_products_after_seed(product_seed, client, app) -> None:
    """这条是"打通 B 档"的验收面：灌完目录，客户端拿得到的商品列表不再为空。"""
    await product_seed.seed_products(
        app.state.session_factory, _rows(), update=False, grant_points=GRANT_POINTS
    )
    resp = await client.get("/v1/billing/products")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 5
    assert {"id", "code", "type", "name", "price_cents", "points", "duration_days"} <= set(data[0])


# ----------------------------------------------------------------------
# ② 幂等面
# ----------------------------------------------------------------------
async def test_second_run_creates_nothing(product_seed, session_factory) -> None:
    await product_seed.seed_products(session_factory, _rows(), update=False, grant_points=GRANT_POINTS)
    again = await product_seed.seed_products(
        session_factory, _rows(), update=False, grant_points=GRANT_POINTS
    )
    assert again.created == [] and len(again.unchanged) == 5
    assert len(await _codes(session_factory)) == 5, "重复跑不能多出行（code 是幂等键）"


async def test_operators_price_edit_survives_a_reseed(product_seed, session_factory) -> None:
    """运营改过价之后再跑 seed：默认**只报差异不覆盖**——否则调价会被脚本悄悄撤回。"""
    await product_seed.seed_products(session_factory, _rows(), update=False, grant_points=GRANT_POINTS)
    async with session_factory() as session:
        row = (await session.execute(select(Product).where(Product.code == "sub_monthly"))).scalar_one()
        row.price_cents = 990  # 运营做了一次促销调价
        await session.commit()

    result = await product_seed.seed_products(
        session_factory, _rows(), update=False, grant_points=GRANT_POINTS
    )
    assert set(result.skipped) == {"sub_monthly"}
    old, new = result.skipped["sub_monthly"]["price_cents"]
    assert (old, new) == (990, 1900), "差异要报出来，否则没人知道目录和库内不一致"
    assert (await _codes(session_factory))["sub_monthly"] == 990


async def test_update_flag_overwrites_the_diff(product_seed, session_factory) -> None:
    await product_seed.seed_products(session_factory, _rows(), update=False, grant_points=GRANT_POINTS)
    async with session_factory() as session:
        row = (await session.execute(select(Product).where(Product.code == "sub_monthly"))).scalar_one()
        row.price_cents = 990
        await session.commit()

    rows = _rows()
    rows[0]["name"] = "月度订阅（标准价）"
    result = await product_seed.seed_products(session_factory, rows, update=True, grant_points=GRANT_POINTS)
    assert result.updated == ["sub_monthly"]
    assert (await _codes(session_factory))["sub_monthly"] == 1900


# ----------------------------------------------------------------------
# ① 资金面：一条都不写的校验
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_cents", 19.00),  # 浮点金额
        ("price_cents", "1900"),  # 字符串金额
        ("price_cents", True),  # bool 是 int 的子类，单用 isinstance 会放它过去
        ("price_cents", -100),
        ("duration_days", 1.5),
    ],
)
def test_money_and_numeric_shapes_are_rejected(product_seed, field, value) -> None:
    rows = _rows()
    rows[1][field] = value
    with pytest.raises(product_seed.SeedError):
        product_seed.validate(rows, GRANT_POINTS)


def test_subscription_display_points_must_equal_the_configured_grant(product_seed) -> None:
    """入账读配置、页面读目录：两处不一致时资金承诺在说谎，必须拒灌而不是警告。"""
    rows = _rows()
    rows[0]["points"] = 500
    with pytest.raises(product_seed.SeedError) as exc:
        product_seed.validate(rows, GRANT_POINTS)
    message = str(exc.value)
    assert "subscription_monthly_grant_points" in message and "sub_monthly" in message


def test_points_pack_cannot_expire_and_catalog_codes_must_be_unique(product_seed) -> None:
    rows = copy.deepcopy(_rows())
    rows[2]["duration_days"] = 30
    with pytest.raises(product_seed.SeedError):
        product_seed.validate(rows, GRANT_POINTS)

    duplicated = copy.deepcopy(_rows())
    duplicated[3] = dict(duplicated[0])
    with pytest.raises(product_seed.SeedError):
        product_seed.validate(duplicated, GRANT_POINTS)


async def test_validation_failure_writes_nothing(product_seed, session_factory) -> None:
    """拒灌必须是"整批不写"，不是"写了一半发现不对"。"""
    rows = _rows()
    rows[4]["price_cents"] = 98.0
    with pytest.raises(product_seed.SeedError):
        await product_seed.seed_products(session_factory, rows, update=False, grant_points=GRANT_POINTS)
    assert await _codes(session_factory) == {}


async def test_stored_prices_are_ints_not_floats(product_seed, session_factory) -> None:
    """SQLite 的列类型不做保护：浮点只要落过一次，读回来就是 float，账目从此带误差。"""
    await product_seed.seed_products(session_factory, _rows(), update=False, grant_points=GRANT_POINTS)
    async with session_factory() as session:
        prices = (
            (await session.execute(select(Product.price_cents).order_by(Product.code))).scalars().all()
        )
        total = (await session.execute(select(func.sum(Product.price_cents)))).scalar_one()
    assert all(isinstance(p, int) and not isinstance(p, bool) for p in prices), prices
    assert isinstance(total, int)
