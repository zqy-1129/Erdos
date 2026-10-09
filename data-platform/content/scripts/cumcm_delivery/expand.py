"""阶段1：原归档安全展开（zip 用标准库；rar/7z 无工具则显式失败，不伪造）。"""
import json
import os
import stat
import zipfile
from pathlib import Path

from . import core

SECRET_SUFFIXES = (".part", ".crdownload", ".download", ".tmp", ".aria2")
BAD_WIN_NAMES = ("CON", "PRN", "AUX", "NUL",
                 "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                 "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9")


def is_unsafe_member(name):
    n = name.replace("\\", "/")
    if n.startswith("/") or ":" in n or "\x00" in n:
        return "absolute/drive"
    parts = n.split("/")
    if any(p == ".." for p in parts):
        return "traversal"
    for p in parts:
        if p in ("", "."):
            return "empty/dot component"
        base = p.split(".")[0].upper()
        if base in BAD_WIN_NAMES:
            return "windows device name"
        if p.endswith(" ") or p.endswith("."):
            return "trailing space/dot"
    return None


def expand_zip(src, dst_dir, source_id, limit_bytes, max_members):
    members = []
    total = 0
    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        if len(infos) > max_members:
            raise ValueError("archive member count {} exceeds limit {}".format(len(infos), max_members))
        for info in infos:
            bad = is_unsafe_member(info.filename)
            if bad:
                raise ValueError("unsafe member '{}' ({})".format(info.filename, bad))
            if info.is_dir():
                continue
            total += info.file_size
        if total > limit_bytes:
            raise ValueError("expanded size {} exceeds limit".format(total))
        for info in infos:
            if info.is_dir():
                continue
            data = zf.read(info)
            rel = info.filename.replace("\\", "/")
            target = core.safe_relpath(dst_dir, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            member = {
                "source_id": source_id,
                "member_path": rel,
                "sha256": core.sha256_bytes(data),
                "size": len(data),
            }
            if target.exists():
                if core.sha256_bytes(target.read_bytes()) == member["sha256"]:
                    members.append(member)  # 幂等：已存在且字节一致
                    continue
                raise ValueError("conflicting member (different bytes): " + rel)
            target.write_bytes(data)
            members.append(member)
    return members


def main(args):
    scope = core.load_scope(args.scope_config)
    data_root = core.CONTENT_DIR / "normalized" / "cumcm_delivery" / args.run_id
    src_dir = data_root / "sources"
    records = core.load_jsonl(src_dir / "source_files.jsonl")
    member_dir = data_root / "archive_members"
    member_dir.mkdir(parents=True, exist_ok=True)

    archives = [r for r in records if r["in_scope"] and r["status"] == "ok"
                and r["extension"].lower() in (".zip", ".rar", ".7z") and r["size"]]

    members = []
    failures = []
    derivations = []

    for a in archives:
        src_abs = core.no_links(Path(args.data_root) / a["relative_path"])
        try:
            if a["extension"].lower() == ".zip":
                got = expand_zip(str(src_abs), member_dir / a["source_id"], a["source_id"],
                                 2 * 1024 * 1024 * 1024, 20000)
                members.extend(got)
                derivations.append({"source_id": a["source_id"], "kind": "archive_expand",
                                    "member_count": len(got), "status": "ok"})
            else:
                failures.append({"source_id": a["source_id"], "relative_path": a["relative_path"],
                                 "extension": a["extension"], "reason": "无 7z/rar 工具（不在 PATH），未展开"})
        except (ValueError, OSError) as exc:
            failures.append({"source_id": a["source_id"], "relative_path": a["relative_path"],
                             "extension": a["extension"], "reason": str(exc)})

    core.write_jsonl(data_root / "archive_members.jsonl", sorted(members, key=lambda m: m["member_path"]))
    core.write_json(data_root / "archive_failures.json", {"count": len(failures), "failures": failures})
    core.write_jsonl(data_root / "asset_derivations.jsonl", derivations)

    print("expand 完成：归档={}，展开成员={}，失败={}。".format(len(archives), len(members), len(failures)))
    return 0
