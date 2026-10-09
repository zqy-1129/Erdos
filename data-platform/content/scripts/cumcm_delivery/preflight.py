"""阶段0：preflight + protected_baseline + 外部依赖清单 + run_state 初始化。"""
import json
import os
import shutil
import subprocess
import importlib.metadata as md
from pathlib import Path

from . import core

REPORTS_DIR = core.CONTENT_DIR.parent.parent / "reports" / "trae"
RUN_STATE = REPORTS_DIR / "cumcm_delivery_run_state.json"


def which(*names):
    found = {}
    for n in names:
        found[n] = shutil.which(n)
    return found


def package_version(p):
    try:
        return md.version(p)
    except Exception:
        return None


def protected_files():
    files = []
    for p in sorted((core.CONTENT_DIR / "schemas" / "v1").glob("*.json")):
        files.append(str(p.relative_to(core.CONTENT_DIR)))
    for p in sorted((core.CONTENT_DIR / "config").glob("*.json")):
        files.append(str(p.relative_to(core.CONTENT_DIR)))
    for rel in [
        "normalized/stage03/cumcm/bundle.json",
        "normalized/stage03/cumcm/assets.json",
        "normalized/stage03/cumcm/provenance.json",
        "normalized/stage03/cumcm/sample_manifest.json",
        "out/pack/problems.json", "out/pack/templates.json", "out/pack/cases.json",
        "out/pack/problems_seed_v0_sources.json", "out/pack/templates_seed_v0_sources.json",
        "out/pack/cases_seed_v0_sources.json", "out/pack/regression_manifest.json",
    ]:
        if (core.CONTENT_DIR / rel).is_file():
            files.append(rel)
    return files


def compute_baseline():
    out = {"generated_at": core.now_utc_iso(), "python": core.PY, "files": {}}
    for rel in protected_files():
        p = core.CONTENT_DIR / rel
        out["files"][rel] = {"sha256": core.sha256_of(p), "size": p.stat().st_size}
    return out


def docker_images():
    try:
        r = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                           capture_output=True, text=True, timeout=30)
        return [l for l in r.stdout.splitlines() if l.strip()]
    except Exception as exc:
        return ["docker images 失败: " + str(exc)]


def external_dependencies():
    deps = []
    # 解析/OCR
    if not package_version("paddleocr") and not package_version("docling"):
        deps.append({
            "id": "layout_ocr_parser", "title": "版面识别/OCR（公式/图表/扫描页）",
            "need": "处理 96 个待 OCR 的论文 PDF、公式 crop、图表/表格结构识别",
            "available": "docker 镜像 llm-chat-parser:docling-native 已存在；本机 pytorch 环境无 paddleocr/docling",
            "status": "blocked_partial", "next": "通过授权解析服务适配器接入 docling-native（需核对其 HTTP 协议/模型版本/许可）",
        })
    # embedding
    if not package_version("sentence_transformers"):
        deps.append({
            "id": "embedding_service", "title": "语义向量/embedding",
            "need": "阶段8 hybrid 检索的真实向量（非 hash 随机向量）",
            "available": "无本地 embedding 模型；无 provider 凭据",
            "status": "blocked", "next": "提供 embedding provider/model 后接入；在此之前 retrieval_mode=lexical_only",
        })
    # object storage
    deps.append({
        "id": "object_storage", "title": "S3/OSS 对象存储",
        "need": "阶段7 对象上传/取回/校验（非 file backend）",
        "available": "docker 可运行；minio/minio 镜像拉取被 registry 拒绝（pull access denied），未验证",
        "status": "blocked", "next": "提供可用镜像/tag/registry 或云对象存储端点后验证；已实现 S3 兼容适配器与 file backend（仅单测）",
    })
    # cloud target
    deps.append({
        "id": "cloud_deploy", "title": "生产云端部署/正式发布",
        "need": "正式激活 release、鉴权下载、生产入库",
        "available": "无生产目标/凭据",
        "status": "blocked", "next": "由用户/BE 提供目标与凭据；本地真实 PG(import)+本地对象适配器已验证/待验证",
    })
    return deps


def run_preflight(args):
    scope = core.load_scope(args.scope)
    env = core.try_load_env()

    preflight = {
        "generated_at": core.now_utc_iso(),
        "python": {"exe": core.PY,
                   "version": subprocess.run([core.PY, "-c", "import sys;print(sys.version.split()[0])"],
                                             capture_output=True, text=True).stdout.strip(),
                   "resolved": os.path.abspath(core.PY)},
        "tools": which("docker", "7z", "7za", "typst", "xelatex", "pdflatex", "latexmk", "pdftotext", "pdfinfo", "psql", "rg"),
        "packages": {p: package_version(p) for p in
                     ["asyncpg", "sqlalchemy", "openpyxl", "numpy", "matplotlib", "scipy", "pandas",
                      "pypdfium2", "paddleocr", "docling", "sentence_transformers", "minio", "boto3"]},
        "docker_images": docker_images(),
        "postgres": {"docker_container": "cumcm-pg-test", "port": 55432, "pgvector": "probe 见 import 阶段"},
        "scope": scope,
    }

    deps = external_dependencies()

    core.write_json(core.CONTENT_DIR / "out" / "cumcm_delivery" / "preflight.json", preflight)
    core.write_json(core.CONTENT_DIR / "out" / "cumcm_delivery" / "protected_baseline.json", compute_baseline())
    core.write_json(core.CONTENT_DIR / "out" / "cumcm_delivery" / "external_dependencies.json", deps)

    run_state = {
        "generated_at": core.now_utc_iso(),
        "run_id": args.run_id,
        "release_id": args.release_id,
        "stages": {f"p{i}": "pending" for i in range(11)},
        "phase0": "done",
        "previous_snapshot": "normalized/competitions_codex_v6",
        "previous_release": "out/releases/content-candidate-codex-v3",
    }
    core.write_json(RUN_STATE, run_state)

    lines = [
        "# cumcm 交付实现映射（implementation map）",
        "",
        "参考既有 Codex v6 流水线（content_runtime.py/consumer_v2.py/batch_content_v2.py/profiles_v3.py/release_builder_v2.py），",
        "本轮国赛 2010-2025 交付在**新命名空间与目录**下实现，不改动既有 v6 产物。",
        "",
        "| 模块 | 落点 | 说明 |",
        "|---|---|---|",
        "| CLI 统一入口 | scripts/cumcm_delivery.py | preflight/inventory/expand/parse/relate/recipes/templates/pack/import/retrieve/fetch/check/evaluate/update/build |",
        "| 内部模块 | scripts/cumcm_delivery/ | core/preflight/inventory/expand/parse/relate/recipes/templates/pack/importdb/retrieve/evaluate |",
        "| v2 契约 | schemas/cumcm/v2/ | 结构块/关联/recipe/bundle/response/evidence |",
        "| SQL | sql/cumcm/ | 版本化 up/rollback/约束/索引/核对，schema=content_de |",
        "| 数据输出 | normalized/cumcm_delivery/<run_id>/ | scope/sources/parse_runs/problems/papers/components/links/recipes/quality |",
        "| 发布 | out/cumcm_delivery/releases/<release_id>/ | catalog/integrity/manifest/entities/assets/templates/retrieval/seed/provenance/reports |",
        "| 缓存 | out/cumcm_delivery/cache/ | 输入+配置+工具指纹绑定 |",
        "| 集成日志 | out/cumcm_delivery/integration/<run_id>/ | PG/对象/消费验证 |",
        "| 对接 | handoff/cumcm/ | BE/FE/EN/OPERATIONS |",
    ]
    (core.CONTENT_DIR / "out" / "cumcm_delivery" / "implementation_map.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")

    w = which("docker", "7z", "typst", "xelatex")
    print("preflight 完成：python={} docker={} 7z={} typst={} xelatex={}".format(
        preflight["python"]["version"], bool(w["docker"]), bool(w["7z"]), bool(w["typst"]), bool(w["xelatex"])))
    print("外部依赖 {} 项；protected_baseline {} 文件。".format(len(deps), len(protected_files())))
    return 0
