"""模板库数据包构建（工作包2）：4 套模板 → 资产 + 灌库数据包 + 溯源清单。

输入：config/templates_v0.yaml（+ 生成的 out/templates/*.docx）
输出（out/pack/）：
- templates.json                  灌库数据包（字段严格对齐 server ports.TemplateRecord）
- templates_seed_v0_sources.json  溯源清单（zip 内逐文件 sha256 / 生成脚本留痕）
资产（out/templates/）：{business_id}.zip（LaTeX，确定性打包）与 .docx（脚本生成）。

用法：python scripts/make_docx_templates.py && python scripts/build_template_pack.py
"""

import argparse
import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

CONTENT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = CONTENT_DIR / "config" / "templates_v0.yaml"
OUT_DIR = CONTENT_DIR / "out" / "templates"
PACK_DIR = CONTENT_DIR / "out" / "pack"

# 与 server/app/domain/entitlement/ports.py::TemplateRecord 字段严格一致
TEMPLATE_FIELDS = ("business_id", "competition", "format", "oss_key", "sha256", "version", "changelog", "tier")

ZIP_FIXED_TIME = (2026, 1, 1, 0, 0, 0)


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


def build_zip(source_dir, target):
    """确定性打包：文件按名排序、固定时间戳，保证重建 sha256 稳定。"""
    files = sorted(p for p in source_dir.rglob("*") if p.is_file())
    if not files:
        raise RuntimeError("模板目录为空：" + str(source_dir))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for file_path in files:
            arcname = file_path.relative_to(source_dir).as_posix()
            info = zipfile.ZipInfo(arcname, ZIP_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            zout.writestr(info, file_path.read_bytes())
    return files


def main():
    reconfigure_stdout()
    parser = argparse.ArgumentParser(description="模板库数据包构建")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    version = config["version"]
    root = Path(os.environ.get("ERDOS_DATA_ROOT", "D:/Erdos_data"))
    repo_root = find_repo_root(CONTENT_DIR)
    generated_at = utc_now()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    sources_items = []
    errors = []

    for item in config["templates"]:
        business_id = item["business_id"]
        if item["package"] == "zip":
            source_dir = root / item["source_dir"]
            if not source_dir.is_dir():
                errors.append("{}: 模板源目录不存在 {}".format(business_id, source_dir))
                continue
            target = OUT_DIR / (business_id + ".zip")
            files = build_zip(source_dir, target)
            file_entries = [
                {"rel": p.relative_to(source_dir).as_posix(), "sha256": sha256_of(p), "bytes": p.stat().st_size}
                for p in files
            ]
            provenance = {"kind": "zip", "source_dir": item["source_dir"], "files": file_entries}
        else:
            target = CONTENT_DIR / item["file"]
            if not target.exists():
                errors.append("{}: 生成文件缺失 {}（先运行 make_docx_templates.py）".format(business_id, target))
                continue
            provenance = {"kind": "generated", "generator": "scripts/make_docx_templates.py"}

        record = {
            "business_id": business_id,
            "competition": item["competition"],
            "format": item["format"],
            "oss_key": item["oss_key"],
            "sha256": sha256_of(target),
            "version": item["version"],
            "changelog": item["changelog"],
            "tier": item["tier"],
        }
        if set(record.keys()) != set(TEMPLATE_FIELDS):
            errors.append("{}: 字段与 TemplateRecord 不一致".format(business_id))
        records.append(record)
        sources_items.append({
            "business_id": business_id,
            "artifact": {
                "repo_path": target.resolve().relative_to(repo_root).as_posix(),
                "sha256": record["sha256"],
                "bytes": target.stat().st_size,
            },
            "provenance": provenance,
            "license_note": (
                "基于社区开源模板体系转换；对外发布前需完成上游 License 核验"
                if item["package"] == "zip" else
                "由本仓库脚本生成（对照官方格式规范）"
            ),
        })

    if errors:
        print("构建失败，{} 条错误：".format(len(errors)))
        for err in errors:
            print("  - " + err)
        sys.exit(1)

    (PACK_DIR / "templates.json").write_bytes(
        (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (PACK_DIR / "templates_seed_{}_sources.json".format(version)).write_bytes(
        (json.dumps({
            "pack": "templates_seed_" + version,
            "generated_at": generated_at,
            "items": sources_items,
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    print("构建完成：templates.json {} 套".format(len(records)))
    for record in records:
        print("  {:<26} {:<6} {:<12} {} | {}".format(
            record["business_id"], record["format"], record["tier"],
            record["sha256"][:16], record["oss_key"]))
    print("输出目录：{} / {}".format(OUT_DIR, PACK_DIR))


if __name__ == "__main__":
    main()
