"""problems 种子数据包构建（工作包1）：题面 + 元数据 → 灌库数据包 + 溯源清单。

输入：config/problems_regression_v0.yaml + out/texts/{business_id}.txt
输出（out/pack/）：
- problems.json                  灌库数据包（字段严格对齐 server ports.ProblemRecord，供 content_seed.py 直读）
- problems_seed_v0_sources.json  溯源清单（源文件/附件 sha256 全量留痕）
- regression_manifest.json       SP1-7 移交清单（题面冻结 sha256，供 engine/QA 回归）

用法：python scripts/build_problem_pack.py
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

CONTENT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = CONTENT_DIR / "config" / "problems_regression_v0.yaml"
TEXTS_DIR = CONTENT_DIR / "out" / "texts"
PACK_DIR = CONTENT_DIR / "out" / "pack"

# 与 server/app/domain/entitlement/ports.py::ProblemRecord 字段严格一致（多/少一个键灌库即报错）
PROBLEM_FIELDS = (
    "business_id", "competition", "year", "problem_code", "title", "tags",
    "prompt_zh", "prompt_en", "attachments", "scoring", "dataset_hint", "visibility",
)
PROMPT_ZH_MAX = 8192  # 数据模型 8.1 / 迁移 0009 上限

MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".gif": "image/gif",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".rar": "application/vnd.rar",
    ".zip": "application/zip",
    ".txt": "text/plain",
}

EXTRACT_TOOL = {".pdf": "pdftotext -enc UTF-8 -layout", ".docx": "python-docx", ".doc": "WPS COM → python-docx"}


def reconfigure_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def sha256_of(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_repo_root(start):
    for parent in [start] + list(start.parents):
        if (parent / ".git").exists():
            return parent
    return start


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    reconfigure_stdout()
    parser = argparse.ArgumentParser(description="problems 种子数据包构建")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    version = config["version"]
    root = Path(os.environ.get("ERDOS_DATA_ROOT", config.get("dataset_root", "D:/Erdos_data")))
    repo_root = find_repo_root(CONTENT_DIR)
    generated_at = utc_now()

    problems_out = []
    sources_items = []
    regression_items = []
    errors = []

    seen = set()
    for item in config["problems"]:
        business_id = item["business_id"]
        if business_id in seen:
            errors.append("business_id 重复：" + business_id)
            continue
        seen.add(business_id)

        text_path = TEXTS_DIR / (business_id + ".txt")
        if not text_path.exists():
            errors.append("缺失题面文本（先运行 extract_problem_text.py）：" + str(text_path))
            continue
        prompt_zh = text_path.read_text(encoding="utf-8")
        if len(prompt_zh) > PROMPT_ZH_MAX:
            errors.append("{}: prompt_zh {} 字符超过上限 {}".format(business_id, len(prompt_zh), PROMPT_ZH_MAX))

        attachments = []
        attachment_sources = []
        for rel in item.get("attachments", []):
            rel_posix = rel.replace("\\", "/")
            file_path = root / rel_posix
            if not file_path.exists():
                errors.append("{}: 附件不存在 {}".format(business_id, rel_posix))
                continue
            sha256 = sha256_of(file_path)
            attachments.append({
                "name": file_path.name,
                "oss_key": rel_posix,  # 预上传占位：数据集相对路径；上 OSS 后改写为 object key
                "sha256": sha256,
                "media_type": MEDIA_TYPES.get(file_path.suffix.lower(), "application/octet-stream"),
            })
            attachment_sources.append({
                "rel": rel_posix, "sha256": sha256, "bytes": file_path.stat().st_size,
            })

        source_path = root / item["source"]
        if not source_path.exists():
            errors.append("{}: 源文件不存在 {}".format(business_id, item["source"]))
            continue

        record = {
            "business_id": business_id,
            "competition": item["competition"],
            "year": item["year"],
            "problem_code": item["problem_code"],
            "title": item["title"],
            "tags": item["tags"],
            "prompt_zh": prompt_zh,
            "prompt_en": "",
            "attachments": attachments,
            "scoring": "",
            "dataset_hint": "",
            "visibility": "member",
        }
        if set(record.keys()) != set(PROBLEM_FIELDS):
            errors.append("{}: 字段与 ProblemRecord 不一致".format(business_id))
        problems_out.append(record)

        sources_items.append({
            "business_id": business_id,
            "source": {
                "rel": item["source"].replace("\\", "/"),
                "sha256": sha256_of(source_path),
                "bytes": source_path.stat().st_size,
                "extract_tool": EXTRACT_TOOL.get(source_path.suffix.lower(), "unknown"),
            },
            "text": {
                "repo_path": text_path.resolve().relative_to(repo_root).as_posix(),
                "sha256": sha256_of(text_path),
                "chars": len(prompt_zh),
            },
            "attachments": attachment_sources,
            "engine_regression_category": item["engine_regression_category"],
        })
        regression_items.append({
            "business_id": business_id,
            "engine_regression_category": item["engine_regression_category"],
            "title": item["title"],
            "tags": item["tags"],
            "text_path": text_path.resolve().relative_to(repo_root).as_posix(),
            "text_sha256": sha256_of(text_path),
            "text_chars": len(prompt_zh),
        })

    if errors:
        print("构建失败，{} 条错误：".format(len(errors)))
        for err in errors:
            print("  - " + err)
        sys.exit(1)

    PACK_DIR.mkdir(parents=True, exist_ok=True)
    (PACK_DIR / "problems.json").write_bytes(
        (json.dumps(problems_out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (PACK_DIR / "problems_seed_{}_sources.json".format(version)).write_bytes(
        (json.dumps({
            "pack": "problems_seed_" + version,
            "generated_at": generated_at,
            "dataset_root": str(root),
            "items": sources_items,
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (PACK_DIR / "regression_manifest.json").write_bytes(
        (json.dumps({
            "version": version,
            "generated_at": generated_at,
            "note": "SP1-7 回归题面冻结清单：text_sha256 对应仓库内 out/texts/ 文件；engine_regression_category 对齐 engine/regression/problems.py",
            "items": regression_items,
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    total_att = sum(len(p["attachments"]) for p in problems_out)
    print("构建完成：")
    print("  problems.json                  {} 题".format(len(problems_out)))
    print("  problems_seed_{}_sources.json  溯源 {} 条（附件 {} 个）".format(version, len(sources_items), total_att))
    print("  regression_manifest.json       {} 题".format(len(regression_items)))
    for record in problems_out:
        print("    {:<14} {:>5} 字符 | {}".format(
            record["business_id"], len(record["prompt_zh"]), record["title"]))
    print("输出目录：{}".format(PACK_DIR))


if __name__ == "__main__":
    main()
