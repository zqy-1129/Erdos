> 当前本地团队 v3 接收约定见 [LOCAL_V3.md](LOCAL_V3.md)；本页旧版本说明保留供追溯。

> 此文件为旧 Trae 交付说明。运行版本、地址与命令请使用 [Codex 本地交付对接手册](CODEX_LOCAL_HANDOFF.md)，旧命令不作为本次验收证据。

# 对接交付：运维（OPERATIONS）

## 环境

- Python 固定 `E:/Anaconda/envs/pytorch/python.exe`（3.8.19）。服务端 `>=3.12` 仅指对端，数据工具不 import 其新语法。
- 本地测试 PostgreSQL：docker 容器 `cumcm-pg-test`（pgvector/pgvector:pg16，127.0.0.1:55432，db=cumcm_delivery，user=erdos）。
- 工具可用性：docker=True、7z=False（RAR 未展开）、typst/xelatex=False（LaTeX/Typst 未编译）。

## 检查点与恢复

- run 状态：`reports/trae/cumcm_delivery_run_state.json`。
- 只读 QC（不写库/不补文件）：`scripts/cumcm_delivery.py check --release ...`。
- build 每次新 run-id/release-id；`update` 增量（inventory→expand→parse→relate→pack）。

## 外部依赖清单

1. RAR 展开：无 7z/rar 工具 → 48 个 rar/7z 归档未展开（2013/2014 论文为主），恢复命令需先获得 unrar/7z。
2. 版面/OCR/公式/图表：docker 镜像 `llm-chat-parser:docling-native` 存在但未接入运行；4087 页 needs_ocr。
3. 对象存储：minio 镜像被 registry 拒拉；file backend 仅单测。
4. embedding：无 provider/model；retrieval=lexical_only。
5. 生产发布/激活：无云端目标与凭据。

## 键/凭据

连接来自环境变量/本地 `.env`（见 `config/cumcm_delivery/.env.example`），不进报告、命令输出或 Git。
