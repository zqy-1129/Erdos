> 当前本地团队 v3 接收约定见 [LOCAL_V3.md](LOCAL_V3.md)；本页旧版本说明保留供追溯。

> 此文件为旧 Trae 交付说明。运行版本、地址与命令请使用 [Codex 本地交付对接手册](CODEX_LOCAL_HANDOFF.md)，旧命令不作为本次验收证据。

# 对接交付：客户端（FE）

状态：`existing`=DE 本地数据/适配器；`proposed`=建议 IPC 字段（不称接口已冻结）。

## 1. 赛事启动描述 bootstrap

```powershell
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/cumcm_delivery.py retrieve   # 无 --request 时返回 bootstrap
```

返回：family 版本、ruleset 状态（unknown）、可用格式（latex 源码未编译 / typst 源码未编译）、retrieval_mode=lexical_only、组件计数。

## 2. 选择格式与模板下载

- LaTeX 完整依赖包：`out/cumcm_delivery/releases/cumcm-2010-2025-v1/seed/templates.json`（`cumcm-latex-family-v1.zip`）。
- 下载后 SHA 校验（manifest/integrity.json），描述见 `dependency_manifest.json`。
- 客户端缓存 key=(asset_id, version, sha256)，命中复验；篡改缓存拒绝下载（`fetch` 校验行为同构）。

## 3. 状态展示

- `ruleset_status=unknown` → 显示“当届规则待核验，通用草稿”。
- `consumable/`：历史论文 license 未授权 → 正常消费者 `retrieve` 返回 `no_reference`（仅有自研通用 recipe）。
- 断网：候选包本地自足（元数据+模板字节）；对象资源走对象存储（待接）。

## 4. 建议 IPC

`content.bootstrap() -> bootstrap`；`content.fetch(asset_id, version) -> bytes+sha`；`content.retrieve(request) -> capsule`。
字段语义见 `schemas/cumcm/v2/` 与 `retrieve` 输出样例。
