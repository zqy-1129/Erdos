"""支付渠道查单驱动（EC-N7 / DEC-022 的服务端半部）。

三态语义是这里的唯一契约，别把它简化成布尔值：
- PAID：渠道确认已收款（真实驱动必须自己完成验签才允许返回）；
- NOT_PAID：渠道明确回答"没有这笔收款"——**只有这个状态才允许关单**；
- UNKNOWN：查不动（未配凭据、网络失败、回包异常）。既不关单也不入账，宁可让订单停在
  created 等下一轮，也不能把"不确定"当"未支付"处理（异常规范 §1 fail-closed 原则）。

真实微信/支付宝通道要商户号与证书（DEC-022 外部阻塞项），当前一律返回 UNKNOWN 并给出
可诊断的 reason；mock 渠道只在 dev/test 注册，收款证据必须显式登记，绝不凭空判定已收款。
"""

from app.core.config import Settings
from app.domain.billing.ports import (
    ChannelPayment,
    ChannelQueryResult,
    ChannelQueryStatus,
    PaymentChannel,
)

DEV_CHANNELS = ("dev", "test")


class MockPaymentChannel:
    """dev/test 渠道：收款证据由联调脚本/测试显式登记，未登记即视为未收款。"""

    name = "mock"

    def __init__(self) -> None:
        self._payments: dict[str, ChannelPayment] = {}

    def mark_paid(self, order_id: str, payment_no: str, amount_cents: int) -> None:
        """登记一笔渠道侧收款（模拟支付成功但回调未送达）。"""
        self._payments[order_id] = ChannelPayment(
            payment_no=payment_no, amount_cents=amount_cents
        )

    def clear(self, order_id: str) -> None:
        self._payments.pop(order_id, None)

    async def query_order(self, order_id: str) -> ChannelQueryResult:
        payment = self._payments.get(order_id)
        if payment is None:
            return ChannelQueryResult(
                status=ChannelQueryStatus.NOT_PAID, reason="mock 渠道未登记该订单收款"
            )
        return ChannelQueryResult(status=ChannelQueryStatus.PAID, payment=payment)


class UnconfiguredPaymentChannel:
    """真实渠道在凭据到位前的占位实现：如实返回 UNKNOWN，不伪装成"未收款"。"""

    def __init__(self, name: str, missing: str) -> None:
        self.name = name
        self._missing = missing

    async def query_order(self, order_id: str) -> ChannelQueryResult:
        return ChannelQueryResult(
            status=ChannelQueryStatus.UNKNOWN,
            reason=f"渠道 {self.name} 未接入（缺 {self._missing}，DEC-022）",
        )


def build_payment_channels(settings: Settings) -> dict[str, PaymentChannel]:
    """装配渠道注册表：mock 只在 dev/test 注册，真实渠道未配凭据即 UNKNOWN。

    生产环境出现 channel=mock 的订单会落到"未注册渠道"分支（查不动、不关单、不计入补账），
    与 DEC-013 的渠道隔离纪律一致：替身不得伪装成真实通道。
    """
    channels: dict[str, PaymentChannel] = {
        "wechat": UnconfiguredPaymentChannel("wechat", "ERDOS_WECHAT_* 商户凭据与回调证书"),
        "alipay": UnconfiguredPaymentChannel("alipay", "ERDOS_ALIPAY_* 应用密钥"),
    }
    if settings.env in DEV_CHANNELS:
        channels["mock"] = MockPaymentChannel()
    return channels
