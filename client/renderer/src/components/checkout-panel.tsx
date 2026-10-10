import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { BRIDGE_CHANNELS, type ErdosBridge } from "../bridges/bridge.ts";
import type { CreatedOrder, OrderView, ProductView } from "../../../shared/product.ts";
import { useRemoteData } from "./use-remote.ts";

const labels = { created: "等待付款", paid: "已到账", closed: "已关闭", refunded: "已退款" };
export function CheckoutPanel(props: { bridge: ErdosBridge; onPaid: () => void }): ReactNode {
  const products = useRemoteData<ProductView[]>(() => props.bridge.invoke(BRIDGE_CHANNELS.billingProducts));
  const [order, setOrder] = useState<OrderView | null>(null);
  const [channel, setChannel] = useState<"wechat" | "alipay">("alipay");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lock = useRef(false);
  const notified = useRef<string | null>(null);
  const onPaid = useRef(props.onPaid);
  onPaid.current = props.onPaid;
  const accept = useCallback((next: OrderView | null) => {
    setOrder(next);
    if (next?.status === "paid" && notified.current !== next.id) {
      notified.current = next.id; onPaid.current();
    }
  }, []);
  const recover = useCallback(async () => {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(null);
    try { accept(await props.bridge.invoke<OrderView | null>(BRIDGE_CHANNELS.billingPendingOrder)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { lock.current = false; setBusy(false); }
  }, [props.bridge, accept]);
  useEffect(() => { void recover(); }, [recover]);
  useEffect(() => {
    if (order?.status !== "created") return;
    const timer = setInterval(() => void recover(), 30000);
    return () => clearInterval(timer);
  }, [order?.status, recover]);
  const buy = async (product: ProductView) => {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(null);
    try {
      const result = await props.bridge.invoke<CreatedOrder>(BRIDGE_CHANNELS.billingCreateOrder, { productCode: product.code, channel });
      accept(result.order);
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { lock.current = false; setBusy(false); }
  };
  return <div className="form-block">
    <h3>订阅与积分购买</h3>
    {products.loading ? <p>正在读取商品…</p> : null}
    {products.error ? <p role="alert">{products.error}<button type="button" onClick={products.reload}>重试商品加载</button></p> : null}
    <label>支付方式<select value={channel} disabled={busy || order?.status === "created"} onChange={e => setChannel(e.target.value as "wechat" | "alipay")}>
      <option value="alipay">支付宝</option><option value="wechat">微信支付</option>
    </select></label>
    <div>{products.data?.map(product => <button key={product.id} type="button" className="btn" disabled={busy || order?.status === "created"}
      onClick={() => void buy(product)}>{product.name} · ¥{(product.price_cents / 100).toFixed(2)} · {product.points} 积分{product.duration_days ? " · " + product.duration_days + "天" : ""}</button>)}</div>
    {products.data?.length === 0 ? <p className="muted">暂无可购买商品。</p> : null}
    {order ? <div role="status"><p>订单 {order.id}：{labels[order.status]} · ¥{(order.price_cents / 100).toFixed(2)}</p>
      {order.status === "created" ? <p className="muted">有效期至 {order.expires_at}，每 30 秒查询到账状态。支付通道尚未返回付款链接或二维码，请等待支付服务接通。</p> : null}
    </div> : null}
    <button type="button" className="btn" disabled={busy} onClick={() => void recover()}>恢复或刷新订单</button>
    {error ? <p role="alert" className="form-error">{error}</p> : null}
  </div>;
}
