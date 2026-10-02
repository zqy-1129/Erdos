# Ed25519 权益快照与阶段许可签名说明（SP0-2 契约基线）

本文档定义 Erdos 服务端签发的两类签名对象（权益快照 + 阶段许可）的**规范化字节序签名**，
客户端凭服务端公钥（JWKS）离线验签。签名算法统一为 **Ed25519**。

## 1. 规范化字节序（Canonical Bytes）

签名对象为 JSON 对象，规范化规则（跨端验签一致）：

1. **键排序**：对 JSON 对象的所有键按字典序（Unicode 码点）升序排列；
2. **紧凑序列化**：`json.dumps(payload, sort_keys=True, separators=(",", ":"))`，
   即键值间用 `:`、条目间用 `,`，不含空格；
3. **UTF-8 编码**：序列化字符串 `.encode("utf-8")` 得到待签名字节。

## 2. 权益快照（entitlement_snapshots）

payload 字段（对齐《数据模型设计》entitlement_snapshots.payload）：

| 字段 | 类型 | 说明 |
|---|---|---|
| subscribed | bool | 是否活跃订阅 |
| sub_end_at | string \| null | 订阅到期时间（ISO 8601，无订阅为 null） |
| purchased_balance | int | 购买积分余额（永不过期） |
| monthly_balance | int | 月度积分余额（月底清零） |
| frozen | bool | 是否欠费冻结 |
| issued_at | string | 签发时间（ISO 8601，纳入签名载荷，客户端据此做防回拨/重放判定） |

规范化示例：

```json
{"frozen":false,"issued_at":"2026-10-03T05:00:00+00:00","monthly_balance":400,"purchased_balance":100,"sub_end_at":"2026-11-01T00:00:00+00:00","subscribed":true}
```

## 3. 阶段许可（stage_grants）

payload 字段（对齐《数据模型设计》stage_grants）：

| 字段 | 类型 | 说明 |
|---|---|---|
| exec_id | string | 许可幂等键 |
| user_id | string | 用户 |
| task_id | string \| null | 任务 |
| stage | string | 阶段（analysis/modeling/solving/writing） |
| points | int | 预扣积分 |
| issued_at | int | 签发时间（Unix 秒） |
| expires_at | int | 过期时间（Unix 秒） |

## 4. 签名与验签

- **签名**：`Ed25519PrivateKey.sign(canonical_bytes)`，输出 `signature.hex()`（128 字符）；
- **密钥版本**：随签名返回 `kid`（密钥 ID），客户端按 kid 取对应公钥；
- **验签**：`Ed25519PublicKey.verify(signature, canonical_bytes)`；
- **防回拨**：`issued_at` 为签发时间，客户端据此拒绝过期/重放。

## 5. 一致性约束

- 任一字段变更（含类型、顺序无关但值变化）都会导致验签失败；
- 快照与许可的 `signature` 均为 hex 字符串，`key_version`/`kid` 标识签发密钥；
- 客户端缓存快照用于 DF-005 离线宽限（72h），联网后重新拉取。
