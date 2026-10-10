# Ed25519 权益快照与阶段许可签名说明（SP0-2 契约基线）

本文档定义 Erdos 服务端签发的两类签名对象（权益快照 + 阶段许可）的**规范化字节序签名**，
客户端凭服务端公钥（JWKS）离线验签。签名算法统一为 **Ed25519**。

## 1. 规范化字节序（Canonical Bytes）

签名对象为 JSON 对象，规范化规则（跨端验签一致）：

1. **键排序**：对 JSON 对象的所有键按字典序（Unicode 码点）升序排列；
2. **紧凑序列化**：`json.dumps(payload, sort_keys=True, separators=(",", ":"))`，
   即键值间用 `:`、条目间用 `,`，不含空格；
3. **非 ASCII 转义**：沿用 `ensure_ascii=True`（`json.dumps` 默认值，**不得改为 False**）——
   非 ASCII 字符一律转为 `\uXXXX`（小写十六进制），增补平面字符转为 UTF-16 代理对
   （`😀` → `\ud83d\ude00`）。改 False 会让中文/emoji 字段产出不同字节，跨端验签整体失效；
4. **数值**：签名载荷只允许**整数**（金额、积分、Unix 秒），不带小数点与指数；
   非整数在两侧都被拒绝（金额一律整数分口径，禁浮点）；
5. **UTF-8 编码**：序列化字符串 `.encode("utf-8")` 得到待签名字节。规范化结果本身是纯 ASCII。

> 上述规则的可执行判据是 `contracts/snapshot-vectors.json`（载荷 → 规范化字节 → 签名 三元组金样例），
> 服务端 `server/tests/unit/test_snapshot_vectors.py` 与客户端
> `client/tests/contract-snapshot-vectors.test.ts` 各读同一份文件断言。**文档与金样例必须同步修订**：
> 只改本文档而 regenerate 向量文件，两侧守护会红。

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
- 客户端缓存快照用于 DF-005 离线宽限（72h），联网后重新拉取；
- 服务端两处独立实现（`app/domain/entitlement/service.py` 快照、`app/domain/points/service.py` 阶段许可）
  的规范化输出必须逐字一致，由金样例守护测试断言。

## 6. 跨端金样例（`contracts/snapshot-vectors.json`）

| 项 | 口径 |
|---|---|
| 内容 | 5 条向量：文档示例快照 / 冻结无订阅快照（含 null）/ 阶段许可 / 非 ASCII+转义 / 篡改负向样本 |
| 每条字段 | `payload`（乱序书写以真实测到键排序）、`canonical`（期望字节串，纯 ASCII）、`signature`、`expect_valid` |
| 密钥 | `key.public_key_spki_der_hex` + `key.kid`；**一次性测试密钥，私钥未落盘、未提交**，仅公钥随向量发布 |
| 消费方 | 服务端 `test_snapshot_vectors.py`、客户端 `contract-snapshot-vectors.test.ts`（各读同一份，不互相 import） |
| 修订 | 载荷字段增删（本文档第 2/3 节变更）时必须重新生成向量，并与契约评审同批提交 |

