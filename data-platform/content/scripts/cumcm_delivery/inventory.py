"""阶段1：cumcm 2010—2025 全量盘点、年度覆盖、身份账本与去重。"""
import json
import os
import re
import stat
from pathlib import Path

from . import core

YEAR_PROBLEM = re.compile(r"^CUMCM(\d{4})Problems$")
YEAR_PAPER = re.compile(r"^(\d{4})年")

MEDIA = {
    ".doc": "application/msword", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf", ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv", ".txt": "text/plain", ".rar": "application/vnd.rar", ".zip": "application/zip",
    ".7z": "application/x-7z-compressed", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".mp4": "video/mp4",
}

ROLE_HINTS = {
    "problem": {"doc", "docx", "pdf"},
    "attachment": {"xls", "xlsx", "csv",
                   "rar", "zip", "7z", "tif", "tiff", "jpg", "jpeg", "png", "gif", "mp4", "txt"},
}


def role_of(ext):
    for role, exts in ROLE_HINTS.items():
        if ext.lstrip(".") in exts:
            return role
    return "other"


def detect_year(rel, root_kind):
    """从父目录确定年份（不取文件名时间戳）。返回 (year_or_None, basis)。"""
    parts = rel.split("/")
    for part in parts:
        if root_kind == "problem":
            m = YEAR_PROBLEM.match(part)
            if m:
                return int(m.group(1)), "parent-dir CUMCM{year}Problems"
        else:
            m = YEAR_PAPER.match(part)
            if m:
                return int(m.group(1)), "parent-dir {year}年"
    return None, "no year dir"


def read_stable(path):
    p = Path(path)
    try:
        st = os.lstat(str(p))
    except OSError as exc:
        return {"status": "failed", "note": str(exc), "sha256": None, "size": None}
    if stat.S_ISLNK(st.st_mode) or (getattr(st, "st_file_attributes", 0) & core.FILE_ATTR_REPARSE):
        return {"status": "failed", "note": "link/reparse rejected", "sha256": None, "size": None}
    if not stat.S_ISREG(st.st_mode):
        return {"status": "failed", "note": "not regular file", "sha256": None, "size": None}
    size = int(st.st_size)
    if size == 0:
        return {"status": "empty", "note": "empty file", "sha256": core.sha256_bytes(b""), "size": 0}
    before = {"size": size, "mtime_ns": int(st.st_mtime_ns)}
    sha = core.sha256_of(p)
    try:
        after = os.lstat(str(p))
    except OSError as exc:
        return {"status": "unstable", "note": str(exc), "sha256": None, "size": None}
    if before["size"] != int(after.st_size) or before["mtime_ns"] != int(after.st_mtime_ns):
        return {"status": "unstable", "note": "changed while reading", "sha256": None, "size": None}
    return {"status": "ok", "note": "", "sha256": sha, "size": size}


def walk_files(root):
    out = []
    core.no_links(root)
    for abs_dir, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not (getattr(os.lstat(str(Path(abs_dir) / d)), "st_file_attributes", 0) & core.FILE_ATTR_REPARSE) and not stat.S_ISLNK(os.lstat(str(Path(abs_dir) / d)).st_mode)]
        if stat.S_ISLNK(os.lstat(abs_dir).st_mode) or (
                getattr(os.lstat(abs_dir), "st_file_attributes", 0) & core.FILE_ATTR_REPARSE):
            continue
        for fn in sorted(files):
            ap = Path(abs_dir) / fn
            rel = ap.relative_to(root).as_posix()
            out.append((rel, ap))
    return out


def main(args):
    scope = core.load_scope(args.scope_config)
    data_root = Path(args.data_root)
    out_dir = core.CONTENT_DIR / "normalized" / "cumcm_delivery" / args.run_id
    (out_dir / "sources").mkdir(parents=True, exist_ok=True)

    records = []
    for root_kind in ("problem", "paper"):
        root = data_root / scope["sources"]["problems" if root_kind == "problem" else "papers"]
        if not root.is_dir():
            raise ValueError("missing required source root: " + str(root))
        for rel, ap in walk_files(root):
            year, basis = detect_year(rel, root_kind)
            ext = ap.suffix.lower()
            info = read_stable(ap)
            in_scope = (year is not None and scope["historical_year_min"] <= year <= scope["historical_year_max"])
            records.append({
                "source_id": "cumcm-src-" + core.sha256_bytes((root_kind + "/" + rel))[:16],
                "relative_path": (scope["sources"]["problems" if root_kind == "problem" else "papers"] + "/" + rel),
                "root_kind": root_kind,
                "year": year,
                "year_basis": basis,
                "in_scope": in_scope,
                "excluded_scope": (not in_scope),
                "extension": ext,
                "media_type": MEDIA.get(ext, "application/octet-stream"),
                "role_hint": role_of(ext),
                "sha256": info["sha256"],
                "content_id": "sha256:" + info["sha256"] if info["sha256"] else None,
                "size": info["size"],
                "status": info["status"],
                "note": info["note"],
            })

    records.sort(key=lambda r: r["relative_path"])
    core.write_jsonl(out_dir / "sources" / "source_files.jsonl", records)

    ledger = []
    for r in records:
        ledger.append({
            "source_id": r["source_id"], "relative_path": r["relative_path"], "sha256": r["sha256"],
            "size": r["size"], "year": r["year"], "in_scope": r["in_scope"], "identity_status": "pending_content_review",
        })
    core.write_json(out_dir / "sources" / "source_identity_ledger.json", sorted(ledger, key=lambda x: x["source_id"]))

    # 去重（字节）
    by_hash = {}
    for r in records:
        if r["status"] == "ok" and r["sha256"] and r["size"]:
            by_hash.setdefault(r["sha256"], []).append(r["relative_path"])
    duplicates = [{"sha256": h, "count": len(p), "paths": p} for h, p in by_hash.items() if len(p) > 1]

    # 年度覆盖
    years = {}
    for r in records:
        if r["year"] is None:
            continue
        y = r["year"]
        years.setdefault(y, {"problem_files": 0, "paper_files": 0, "problem_ext": {}, "paper_ext": {}})
        key = "problem_files" if r["root_kind"] == "problem" else "paper_files"
        years[y][key] += 1
        extk = r["extension"] or "<none>"
        extd = "problem_ext" if r["root_kind"] == "problem" else "paper_ext"
        years[y][extd][extk] = years[y][extd].get(extk, 0) + 1

    coverage = {
        "scope": {"competition_id": "cumcm", "year_min": 2010, "year_max": 2025},
        "note": "file counts, not logical problem/paper counts; year from parent dir",
        "by_year": [
            {"year": y, "problem_files": v["problem_files"], "paper_files": v["paper_files"],
             "problem_ext": dict(sorted(v["problem_ext"].items())),
             "paper_ext": dict(sorted(v["paper_ext"].items())),
             "in_scope": 2010 <= y <= 2025}
            for y, v in sorted(years.items())
        ],
        "totals": {
            "problem_files_in_scope": sum(1 for r in records if r["root_kind"] == "problem" and r["in_scope"]),
            "paper_files_in_scope": sum(1 for r in records if r["root_kind"] == "paper" and r["in_scope"]),
            "excluded_files": sum(1 for r in records if not r["in_scope"]),
            "duplicate_groups": len(duplicates),
            "ok_files": sum(1 for r in records if r["status"] == "ok"),
            "failed_files": sum(1 for r in records if r["status"] == "failed"),
            "empty_files": sum(1 for r in records if r["status"] == "empty"),
        },
    }
    core.write_json(out_dir / "sources" / "coverage_by_year.json", coverage)
    core.write_json(out_dir / "sources" / "duplicates.json", duplicates)

    # scope_report
    excluded = [{"relative_path": r["relative_path"], "year": r["year"], "note": "2026 或越界，excluded_scope（不删除原件）"}
                for r in records if not r["in_scope"]]
    core.write_json(out_dir / "sources" / "scope_report.json", {
        "in_scope_files": sum(1 for r in records if r["in_scope"]),
        "excluded": excluded,
    })

    print("inventory 完成：问题文件(in-scope)={}，论文文件(in-scope)={}，excluded={}，重复组={}".format(
        coverage["totals"]["problem_files_in_scope"], coverage["totals"]["paper_files_in_scope"],
        coverage["totals"]["excluded_files"], len(duplicates)))
    return 0
