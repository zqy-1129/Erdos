> 当前本地团队 v3 接收约定见 [LOCAL_V3.md](LOCAL_V3.md)；本页旧版本说明保留供追溯。

> 此文件为旧 Trae 交付说明。运行版本、地址与命令请使用 [Codex 本地交付对接手册](CODEX_LOCAL_HANDOFF.md)，旧命令不作为本次验收证据。

# 对接交付：服务端（BE）

状态：`existing`=DE 本地实现+真实 PG 验证；`proposed`=待 BE 生产接入。

## 1. 迁移（existing，真实 PostgreSQL 16.15 + pgvector 0.8.7 已验证）

```powershell
# 独立命名空间 content_de，不改动既有业务表
Get-Content sql/cumcm/0001_content_de_up.sql | docker exec -i cumcm-pg-test psql -U erdos -d cumcm_delivery -v ON_ERROR_STOP=1
# 回滚（非破坏，仅删 content_de）
Get-Content sql/cumcm/0001_content_de_rollback.sql | docker exec -i cumcm-pg-test psql -U erdos -d cumcm_delivery -v ON_ERROR_STOP=1
```

约束已实测生效：外键（subproblems→problems）、CHECK（asset version≥1、sha256 64hex）、embedding vector 类型（pgvector 0.8.7）。

## 2. 导入（existing + proposed）

```powershell
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/cumcm_delivery.py import --release out/cumcm_delivery/releases/cumcm-2010-2025-v1 --target local-test --dry-run
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/cumcm_delivery.py import --release out/cumcm_delivery/releases/cumcm-2010-2025-v1 --target local-test --apply
```

- 顺序：`asset → edition/problem/subproblem/paper → link → release_items`（deferrable 外键，避免循环依赖）。
- 幂等：`ON CONFLICT DO NOTHING`；同 (asset_id, version) 不同 sha 由 `UNIQUE(asset_id, version, sha256)` 拒绝。
- 连接：环境变量 `ERDOS_PG_*`（见 `config/cumcm_delivery/.env.example`），asyncpg 驱动，不从命令行传密码。

## 3. 对象键与下载（existing + proposed）

- 对象键：`content/cumcm/assets/<asset_id>/v<version>/<sha256>/<filename>`（见 `out/cumcm_delivery/releases/cumcm-2010-2025-v1/entities/assets.json`）。
- 库内存永久键；短期签名 URL 每次鉴权生成，不落库。
- 本地 binding：`out/cumcm_delivery/releases/cumcm-2010-2025-v1/assets/local_bindings.json`（asset_id → 源相对路径，仅本地测试；生产走对象存储）。
- `fetch` 命令真实读回字节并 SHA 校验（`scripts/cumcm_delivery.py fetch <asset_id>`）。

## 4. 行数核对（existing 实测）

本地 PG 导入后：problems=10、subproblems=9、papers=41、links=7（确认父题）、assets=557。

## 5. 未完成（BE 需要补齐的外部）

- 对象存储（minio 镜像被 registry 拒拉）：已实现 S3 兼容适配器思路与 file backend，生产需目标端点。
- 正式发布激活（`--activate`/release 指针切换）已留接口，本地未做生产激活。
