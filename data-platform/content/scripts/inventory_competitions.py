"""资料盘点工具（阶段01）：扫描 D:/Erdos_data 原件，产出逐文件记录、重复组、
覆盖矩阵与问题清单，并支持工作区临时夹具自检。

用法：
    <venv>/python scripts/inventory_competitions.py \
        --data-root D:/Erdos_data \
        --registry config/competition_registry.json \
        --out out/inventory --hash-mode full

    <venv>/python scripts/inventory_competitions.py --selftest   # 工作区临时夹具自检

原则（对齐阶段01完成标准）：
- D:/Erdos_data 只读：本脚本只读取原件，绝不写入/移动/删除/解压到原件目录。
- 逐文件流式计算 sha256（1MB 分块），读取前后核对 size/mtime；下载中变化、读失败、
  不可访问、临时下载后缀文件列为 unstable/failed/pending，不作为有效记录。
- 相同未变化的输入重复运行，除生成时间等运行元信息外，记录与统计应一致。
- 资源类型提示仅基于来源根/文件名，正文未核验不标为已确认题目/优秀论文。
脚本兼容 Python 3.8，仅用标准库。
"""

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

CHUNK_SIZE = 1024 * 1024
KNOWN_ROOTS = ("problems", "papers_pending_auth", "templates", "CPMCM")
TEMP_SUFFIXES = (".part", ".crdownload", ".download", ".tmp", ".temp", ".aria2", ".partial")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CACHE_FILENAME = ".hash_cache.json"
# Windows reparse 点（symlink/junction）属性位；非 Windows 回退到已知常量 0x400
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _scandir_entries(path):
    """os.scandir 的可覆盖入口（测试注入枚举失败 / 假条目用）。"""
    return os.scandir(str(path))


def _entry_stat(entry):
    """DirEntry 的非跟随 stat（可覆盖入口）。follow_symlinks=False 对应 lstat。"""
    return entry.stat(follow_symlinks=False)


def _is_reparse_attrs(file_attributes):
    """判定是否为 Windows reparse 点（symlink/junction）。纯函数，可单测。"""
    return bool((file_attributes or 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _is_link_stat(st):
    return stat.S_ISLNK(st.st_mode) or _is_reparse_attrs(getattr(st, "st_file_attributes", 0))


def _safe_stat(path):
    """跟随型 stat 的兜底封装，失败返回 None。"""
    try:
        st = os.lstat(str(path))
    except OSError:
        return None
    if _is_link_stat(st) or not stat.S_ISREG(st.st_mode):
        return None
    return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}


def reconfigure_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def stat_changed(before, after):
    return before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns


def has_temp_suffix(name):
    lower = name.lower()
    return any(lower.endswith(s) for s in TEMP_SUFFIXES)


def _stream_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_with_check(path, _stat=None, _stream=None):
    """读取单个文件并核对前后 size/mtime，一次读取返回与 hash 一致的稳定元数据。

    R2：hash 与 size/mtime 必须来自同一次稳定读取；前后不一致则 unstable，不能 ok。
    返回 dict：{sha256, status, note, size, mtime_ns}。size/mtime 与 hash 同源（空文件亦然）。
    `_stat`/`_stream` 为测试注入缝，默认走真实 os.stat/_stream_sha256。
    """
    stat_fn = _stat if _stat is not None else os.lstat
    stream_fn = _stream if _stream is not None else _stream_sha256
    path_s = str(path)

    def _fail(msg):
        return {"sha256": None, "status": "failed", "note": msg, "size": None, "mtime_ns": None}

    def _unstable(msg):
        return {"sha256": None, "status": "unstable", "note": msg, "size": None, "mtime_ns": None}

    try:
        before = stat_fn(path_s)
    except OSError as exc:
        return _fail("stat 失败：{}".format(exc))
    if _is_link_stat(before):
        return _fail("读取前发现链接/reparse 点，未读取目标")
    if stat.S_ISDIR(before.st_mode):
        return _fail("路径是目录而非文件")
    if before.st_size == 0:
        # 空文件记录真实空内容 hash，但标记不可用；同时做稳定性确认
        try:
            after = stat_fn(path_s)
        except OSError as exc:
            return _unstable("空文件二次 stat 失败：{}".format(exc))
        if stat_changed(before, after) or _is_link_stat(after):
            return _unstable("空文件前后 stat 变化（不稳定）")
        return {"sha256": EMPTY_SHA256, "status": "empty",
                "note": "空文件（内容 hash 为真，但不可用于正文/模板）",
                "size": int(after.st_size), "mtime_ns": int(after.st_mtime_ns)}
    try:
        digest = stream_fn(path)
    except OSError as exc:
        return _fail("读取失败：{}".format(exc))
    try:
        after = stat_fn(path_s)
    except OSError as exc:
        return _unstable("读取后 stat 失败：{}".format(exc))
    if stat_changed(before, after):
        return _unstable("读取前后 size/mtime 变化（活跃下载/被写入）")
    if _is_link_stat(after):
        return _unstable("读取后路径变为链接/reparse 点")
    return {"sha256": digest, "status": "ok", "note": "",
            "size": int(after.st_size), "mtime_ns": int(after.st_mtime_ns)}


def build_registry_maps(registry):
    """目录名(二级) -> competition_id；以及 competition_id -> 记录；以及 cpmcm 是否有 CPMCM 候选来源。"""
    dir_to_comp = {}
    comp_by_id = {}
    for comp in registry.get("competitions", []):
        dir_to_comp[comp["source_dir_name"]] = comp["competition_id"]
        comp_by_id[comp["competition_id"]] = comp
    return dir_to_comp, comp_by_id


def cpmcm_subdir_hint(subdir_name):
    if "题目" in subdir_name:
        return "研赛题目候选"
    if "论文" in subdir_name:
        return "研赛优秀论文候选"
    return "研赛来源"


def classify_relpath(rel, dir_to_comp, cpmcm_id):
    """把相对路径归类到赛事/共享/未归属，返回 dict。相对路径以 data-root 为准、POSIX 风格。"""
    parts = rel.split("/")
    top = parts[0] if parts else ""
    scope = "unattributed"
    comp_ids = []
    resource_hint = "未归属"
    basis = "来源根未识别：{}".format(top or "<空>")

    if top in KNOWN_ROOTS:
        if len(parts) >= 2:
            sub = parts[1]
            if top == "CPMCM":
                comp_ids = [cpmcm_id] if cpmcm_id else []
                scope = "competition" if cpmcm_id else "unattributed"
                resource_hint = cpmcm_subdir_hint(sub)
                basis = "来源根 CPMCM → {}（研赛候选来源，待内容复核）".format(cpmcm_id or "<未登记>")
            elif top == "templates" and sub == "00_通用模板":
                scope = "shared"
                resource_hint = "模板/格式资源"
                basis = "来源根 templates / 00_通用模板（共享来源，不占十九入口）"
            else:
                comp_id = dir_to_comp.get(sub)
                if comp_id:
                    comp_ids = [comp_id]
                    scope = "competition"
                    basis = "来源根 {} / 目录 {} → {}".format(top, sub, comp_id)
                else:
                    scope = "unattributed"
                    basis = "来源根 {} 下目录 {} 未匹配十九赛事/共享注册表".format(top, sub)
                resource_hint = {
                    "problems": "题目或附件",
                    "papers_pending_auth": "论文或题目",
                    "templates": "模板/格式资源",
                }.get(top, "未归属")
        else:
            scope = "unattributed"
            basis = "来源根 {} 下无二级目录（未能归属）".format(top)
            resource_hint = {
                "problems": "题目或附件",
                "papers_pending_auth": "论文或题目",
                "templates": "模板/格式资源",
                "CPMCM": "研赛来源",
            }.get(top, "未归属")
    return {
        "competition_ids": comp_ids,
        "scope": scope,
        "source_root": top if top in KNOWN_ROOTS else "__uncovered__",
        "resource_type_hint": resource_hint,
        "recognition_basis": basis,
    }


def discover_files(data_root, issues):
    """顶层发现并遍历四个已知来源根，逐条目做真实目标边界检查（R1/R3）。

    - 目录无法枚举：记 error（含路径），不猜内部文件数。
    - 任何 reparse 点（Windows symlink/junction）统一跳过并记 reparse_skipped，绝不读取其目标，
      因此不会走出数据根、也不产生目录环。
    - 返回 (file_entries, uncovered)，file_entries 为 [(rel, abs_path)]（此处不 stat 文件内容，
      元数据由读取阶段 read_with_check 一次性稳定取得）。
    """
    data_root = Path(data_root)
    file_entries = []  # [(rel, abs_path)]
    uncovered = []

    def _iter(path):
        it = _scandir_entries(path)
        try:
            return sorted(it, key=lambda e: e.name)
        finally:
            close = getattr(it, "close", None)
            if close is not None:
                close()

    def _walk(abs_dir):
        try:
            entries = _iter(abs_dir)
        except OSError as exc:
            issues.append({"severity": "error", "code": "dir_enumerate_failed",
                           "message": "目录无法枚举：{}（{}）".format(abs_dir, exc)})
            return
        for e in entries:
            try:
                st = _entry_stat(e)
            except OSError as exc:
                issues.append({"severity": "error", "code": "entry_stat_failed",
                               "message": "条目元数据不可读：{}（{}）".format(getattr(e, "path", "?"), exc)})
                p = Path(e.path)
                file_entries.append((p.relative_to(data_root).as_posix(), p,
                                     "发现条目后元数据读取失败，文件/目录类型待确认：{}".format(exc)))
                continue
            if _is_link_stat(st):
                issues.append({"severity": "warning", "code": "reparse_skipped",
                               "message": "跳过链接/junction/reparse 点（不读取其目标）：{}".format(getattr(e, "path", "?"))})
                continue
            if stat.S_ISDIR(st.st_mode):
                _walk(Path(e.path))
            else:
                p = Path(e.path)
                try:
                    rel = p.relative_to(data_root).as_posix()
                except ValueError:
                    issues.append({"severity": "error", "code": "path_outside_root",
                                   "message": "路径超出数据根：{}".format(p)})
                    continue
                file_entries.append((rel, p))

    try:
        tops = _iter(data_root)
    except OSError as exc:
        issues.append({"severity": "error", "code": "root_enumerate_failed",
                       "message": "数据根无法枚举：{}（{}）".format(data_root, exc)})
        tops = []

    for e in tops:
        name = e.name
        abs_path = Path(e.path)
        try:
            st = _entry_stat(e)
        except OSError as exc:
            issues.append({"severity": "error", "code": "entry_stat_failed",
                           "message": "顶层条目元数据不可读：{}（{}）".format(getattr(e, "path", "?"), exc)})
            file_entries.append((abs_path.relative_to(data_root).as_posix(), abs_path,
                                 "顶层条目元数据读取失败，文件/目录类型待确认：{}".format(exc)))
            continue
        if _is_link_stat(st):
            issues.append({"severity": "warning", "code": "reparse_skipped",
                           "message": "跳过顶层链接/junction/reparse 点（不读取其目标）：{}".format(abs_path)})
            continue
        if stat.S_ISDIR(st.st_mode):
            if name in KNOWN_ROOTS:
                _walk(abs_path)
            else:
                uncovered.append({"kind": "dir", "name": name, "path": abs_path.as_posix()})
        else:
            uncovered.append({"kind": "file", "name": name, "path": abs_path.as_posix()})
    return file_entries, uncovered


def build_record(rel, cls, sha256, status, note, size=None, mtime_ns=None):
    ext = Path(rel).suffix.lower()
    recognition_status = {
        "ok": "待核验",
        "empty": "空文件不可用",
    }.get(status, "未识别")
    return {
        "relative_path": rel,
        "size": int(size) if size is not None else None,
        "mtime_ns": int(mtime_ns) if mtime_ns is not None else None,
        "extension": ext,
        "sha256": sha256,
        "competition_ids": cls["competition_ids"],
        "scope": cls["scope"],
        "source_root": cls["source_root"],
        "resource_type_hint": cls["resource_type_hint"],
        "recognition_basis": cls["recognition_basis"],
        "recognition_status": recognition_status,
        "hash_source": "none",  # 由下方逻辑填充
        "status": status,
        "note": note,
    }


def run_scan(data_root, registry, hash_mode="full", cache_path=None):
    """核心扫描。返回 dict：records / duplicates / coverage / issues / stats。"""
    dir_to_comp, comp_by_id = build_registry_maps(registry)
    cpmcm_id = "cpmcm" if "cpmcm" in comp_by_id else None
    cpmcm_id = cpmcm_id or dir_to_comp.get("13_华为杯研赛")

    issues = []
    file_entries, uncovered = discover_files(data_root, issues)
    for u in uncovered:
        kind_label = "目录" if u["kind"] == "dir" else "文件"
        issues.append({"severity": "warning", "code": "uncovered_top",
                       "message": "顶层未覆盖{}（不做深扫）：{}".format(kind_label, u["path"])})

    # 加载/初始化阶段专用 hash 缓存
    cache = {}
    if cache_path is not None and cache_path.exists():
        try:
            cache = load_json(cache_path)
        except (OSError, ValueError) as exc:
            issues.append({"severity": "warning", "code": "cache_unreadable",
                           "message": "缓存不可读，将忽略：{}（{}）".format(cache_path, exc)})
            cache = {}
    if not isinstance(cache, dict):
        cache = {}

    records = []
    new_cache = {}
    for entry in file_entries:
        rel, fp = entry[:2]
        cls = classify_relpath(rel, dir_to_comp, cpmcm_id)

        if len(entry) > 2:
            rec = build_record(rel, cls, None, "failed", entry[2])
            rec["entry_kind"] = "unknown"
            records.append(rec)
            continue

        if has_temp_suffix(fp.name):
            st = _safe_stat(fp)
            rec = build_record(rel, cls, None, "pending", "临时下载后缀文件（下载中/未完成）",
                               st["size"] if st else None, st["mtime_ns"] if st else None)
            rec["hash_source"] = "none"
            records.append(rec)
            continue

        # cached 模式：先对当前文件做一次全新 stat 与缓存条目核对（弱边界）
        if hash_mode == "cached":
            cur = _safe_stat(fp)
            cached_entry = cache.get(rel)
            if cur is not None and isinstance(cached_entry, dict) and \
                    cached_entry.get("size") == cur["size"] and \
                    cached_entry.get("mtime_ns") == cur["mtime_ns"] and \
                    cached_entry.get("sha256") and _safe_stat(fp) == cur:
                sha256 = cached_entry["sha256"]
                new_cache[rel] = {"size": cur["size"], "mtime_ns": cur["mtime_ns"], "sha256": sha256}
                rec = build_record(rel, cls, sha256, "ok",
                                   "缓存命中（size/mtime 一致；该模式不抵御保留 size/mtime 的改写）",
                                   cur["size"], cur["mtime_ns"])
                rec["hash_source"] = "cached"
                records.append(rec)
                continue

        # full（或 cached 未命中）：同一次稳定读取得到 hash + size + mtime
        info = read_with_check(fp)
        sha256 = info["sha256"]
        status = info["status"]
        note = info["note"]
        size = info["size"]
        mtime = info["mtime_ns"]
        if status == "ok":
            new_cache[rel] = {"size": size, "mtime_ns": mtime, "sha256": sha256}
        rec = build_record(rel, cls, sha256, status, note, size, mtime)
        rec["hash_source"] = "full"
        records.append(rec)

    # 持久化阶段专用 hash 缓存（全量重算后整体重建；删除/变化文件自然不再进入缓存）
    if cache_path is not None:
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(new_cache, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")

    # 排序（稳定输出）
    records.sort(key=lambda r: r["relative_path"])

    # 重复组：仅 status==ok 且 size>0
    by_hash = {}
    for r in records:
        if r["status"] == "ok" and r["sha256"] and r["size"] > 0:
            by_hash.setdefault(r["sha256"], []).append(r)

    duplicates = []
    for h in sorted(by_hash):
        group = by_hash[h]
        if len(group) > 1:
            duplicates.append({
                "sha256": h,
                "size": group[0]["size"],
                "count": len(group),
                "paths": [r["relative_path"] for r in group],
            })

    # 覆盖矩阵
    coverage = build_coverage(records, dir_to_comp, comp_by_id, cpmcm_id)

    # 问题清单：逐文件异常
    for r in records:
        if r["status"] == "ok":
            continue
        sev = {"failed": "error", "unstable": "error", "pending": "info", "empty": "info"}.get(r["status"], "info")
        issues.append({"severity": sev, "code": "file_" + r["status"],
                       "message": "[{}] {}：{}".format(r["status"], r["relative_path"], r["note"])})

    # 空目录 / 缺失模板：按注册表来源目录核对
    issues.extend(empty_and_missing(Path(data_root), registry))

    # 输出顺序稳定
    issues.sort(key=lambda x: (x["code"], x.get("message", "")))

    stats = {
        "total_files": len(records),
        "ok_files": sum(1 for r in records if r["status"] == "ok"),
        "empty_files": sum(1 for r in records if r["status"] == "empty"),
        "pending_files": sum(1 for r in records if r["status"] == "pending"),
        "failed_files": sum(1 for r in records if r["status"] == "failed"),
        "unstable_files": sum(1 for r in records if r["status"] == "unstable"),
        "duplicate_groups": len(duplicates),
        "duplicate_files": sum(d["count"] for d in duplicates),
        "cached_hashes": sum(1 for r in records if r["hash_source"] == "cached"),
        "full_hashes": sum(1 for r in records if r["hash_source"] == "full"),
    }

    return {
        "records": records,
        "duplicates": duplicates,
        "coverage": coverage,
        "issues": issues,
        "stats": stats,
        "uncovered": uncovered,
        "cache_updated": new_cache,
    }


def dir_nonempty(path):
    """返回 True/False；OSError 时返回 None。"""
    try:
        with os.scandir(str(path)) as it:
            return any(True for _ in it)
    except OSError:
        return None


def source_path_state(data_root, path):
    """逐段 lstat；空目录检查也不得枚举链接目标或根外来源。"""
    root = Path(os.path.abspath(str(data_root)))
    target = Path(os.path.abspath(str(path)))
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        return "error", "注册来源超出数据根：{}".format(target)
    current = root
    candidates = [root]
    for part in parts:
        current = current / part
        candidates.append(current)
    for candidate in candidates:
        try:
            st = os.lstat(str(candidate))
        except FileNotFoundError:
            return "missing", "来源不存在：{}".format(candidate)
        except OSError as exc:
            return "error", "来源元数据不可读：{}（{}）".format(candidate, exc)
        if _is_link_stat(st):
            return "skipped", "来源检查跳过链接/reparse 点，未枚举目标：{}".format(candidate)
        if not stat.S_ISDIR(st.st_mode):
            return "error", "注册目录来源不是目录：{}".format(candidate)
    return "directory", ""


def empty_and_missing(data_root, registry):
    """空目录提示与模板缺失提示（空目录是状态提示，不等同程序错误）。"""
    results = []

    def guard(p):
        state, message = source_path_state(data_root, p)
        if state == "skipped":
            results.append({"severity": "warning", "code": "reparse_skipped", "message": message})
        elif state == "error":
            results.append({"severity": "error", "code": "source_check_failed", "message": message})
        return state

    for comp in registry.get("competitions", []):
        for src in comp.get("sources", []):
            p = data_root / src["relative_path"]
            state = guard(p)
            if state in ("skipped", "error"):
                continue
            if state == "missing":
                if src["source_root"] == "templates":
                    results.append({"severity": "warning", "code": "template_missing",
                                    "message": "模板目录缺失：{}（{}）".format(src["relative_path"], comp["competition_id"])})
                else:
                    results.append({"severity": "info", "code": "source_missing",
                                    "message": "来源目录缺失：{}（{}）".format(src["relative_path"], comp["competition_id"])})
                continue
            nonempty = dir_nonempty(p)
            if nonempty is None:
                results.append({"severity": "error", "code": "dir_read_failed",
                                "message": "目录列举失败：{}（{}）".format(src["relative_path"], comp["competition_id"])})
            elif not nonempty:
                sev = "warning" if src["source_root"] == "templates" else "info"
                results.append({"severity": sev, "code": "empty_dir",
                                "message": "空目录（下载补充中）：{}（{}）".format(src["relative_path"], comp["competition_id"])})
    # 共享来源空目录
    for src in registry.get("shared_sources", []):
        p = data_root / src["relative_path"]
        state = guard(p)
        if state in ("skipped", "error"):
            continue
        if state == "missing":
            results.append({"severity": "info", "code": "shared_missing",
                            "message": "共享来源目录缺失：{}".format(src["relative_path"])})
            continue
        nonempty = dir_nonempty(p)
        if nonempty is None:
            results.append({"severity": "error", "code": "dir_read_failed",
                            "message": "目录列举失败：{}（共享来源）".format(src["relative_path"])})
        elif not nonempty:
            results.append({"severity": "info", "code": "empty_dir",
                            "message": "空目录：{}（共享来源）".format(src["relative_path"])})
    return results


def _src():
    return {"files": 0, "bytes": 0}


def _empty_agg():
    return {"total_files": 0, "total_bytes": 0, "valid_files": 0, "valid_bytes": 0,
            "sources": {}, "extensions": {}, "statuses": {}}


def build_coverage(records, dir_to_comp, comp_by_id, cpmcm_id):
    """十九赛事按来源/扩展名/状态统计，独立列共享与未归属。"""

    comps = {cid: _empty_agg() for cid in comp_by_id}
    for cid in comps:
        comps[cid]["sources"] = {"problems": _src(), "papers_pending_auth": _src(),
                                 "templates": _src(), "CPMCM": _src()}
    shared = _empty_agg()
    shared["sources"] = {"templates": _src()}
    unattributed = _empty_agg()
    unattributed["sources"] = {}

    def add(agg, r):
        agg["total_files"] += 1
        agg["total_bytes"] += (r["size"] or 0)
        if r["status"] == "ok":
            agg["valid_files"] += 1
            agg["valid_bytes"] += (r["size"] or 0)
        src = r["source_root"]
        src_agg = agg["sources"].setdefault(src, _src())
        src_agg["files"] += 1
        src_agg["bytes"] += (r["size"] or 0)
        ext = r["extension"] or "<none>"
        e = agg["extensions"].setdefault(ext, _src())
        e["files"] += 1
        e["bytes"] += (r["size"] or 0)
        st = r["status"]
        s = agg["statuses"].setdefault(st, _src())
        s["files"] += 1
        s["bytes"] += (r["size"] or 0)

    for r in records:
        if r["scope"] == "shared":
            add(shared, r)
        elif r["scope"] == "competition":
            for cid in r["competition_ids"]:
                if cid in comps:
                    add(comps[cid], r)
        else:
            add(unattributed, r)

    return {
        "competitions": comps,
        "shared": shared,
        "unattributed": unattributed,
    }


def write_outputs(out_dir, result, data_root, registry, hash_mode, cache_path):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "source_files.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in result["records"]),
        encoding="utf-8")

    (out_dir / "duplicates.json").write_text(
        json.dumps(result["duplicates"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    coverage_doc = {
        "generated_at": now_utc_iso(),
        "data_root": str(data_root),
        "hash_mode": hash_mode,
        "stats": result["stats"],
        "coverage": result["coverage"],
    }
    (out_dir / "coverage.json").write_text(
        json.dumps(coverage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    issues_doc = {
        "generated_at": now_utc_iso(),
        "count": len(result["issues"]),
        "issues": result["issues"],
    }
    (out_dir / "issues.json").write_text(
        json.dumps(issues_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    (out_dir / "inventory_summary.md").write_text(
        render_summary(result, data_root, registry, hash_mode), encoding="utf-8")


def render_summary(result, data_root, registry, hash_mode):
    stats = result["stats"]
    cov = result["coverage"]
    lines = []
    lines.append("# 资料盘点摘要（阶段01）\n")
    lines.append("- 生成时间（UTC）：{}".format(now_utc_iso()))
    lines.append("- 数据根：`{}`（只读扫描）".format(data_root))
    lines.append("- hash 模式：`{}`；缓存命中 {} 条、全量计算 {} 条".format(hash_mode, stats["cached_hashes"], stats["full_hashes"]))
    lines.append("- 扫描限制：输入扫描非原子快照；若存在活跃下载，期间可能变化，见 issues.json。\n")
    lines.append("## 总量")
    lines.append("- 文件总数 {}；有效(ok) {}；空文件 {}；pending {}；failed {}；unstable {}".format(
        stats["total_files"], stats["ok_files"], stats["empty_files"],
        stats["pending_files"], stats["failed_files"], stats["unstable_files"]))
    lines.append("- 重复组 {}（涉及文件 {}，按 sha256，保留全部来源不删原件）\n".format(
        stats["duplicate_groups"], stats["duplicate_files"]))
    lines.append("## 覆盖矩阵（文件数/字节，非题目数或论文数）\n")
    lines.append("| competition_id | 题目 problems | 论文 papers | 模板 templates | CPMCM | 合计文件 | 合计字节 |")
    lines.append("|---|---|---|---|---|---|---|")
    for comp in registry["competitions"]:
        cid = comp["competition_id"]
        c = cov["competitions"].get(cid)
        if not c:
            continue
        def n(src):
            return c["sources"].get(src, {}).get("files", 0)
        def b(src):
            return c["sources"].get(src, {}).get("bytes", 0)
        lines.append("| {} | {} / {}B | {} / {}B | {} / {}B | {} / {}B | {} | {} |".format(
            cid, n("problems"), b("problems"), n("papers_pending_auth"), b("papers_pending_auth"),
            n("templates"), b("templates"), n("CPMCM"), b("CPMCM"),
            c["total_files"], c["total_bytes"]))
    sh = cov["shared"]
    lines.append("| （共享 00_通用模板） | - | - | {} / {}B | - | {} | {} |".format(
        sh["sources"].get("templates", {}).get("files", 0),
        sh["sources"].get("templates", {}).get("bytes", 0),
        sh["total_files"], sh["total_bytes"]))
    un = cov["unattributed"]
    lines.append("| （未归属） | - | - | - | - | {} | {} |".format(un["total_files"], un["total_bytes"]))
    lines.append("\n> 说明：`文件数` 是磁盘文件数，不是题目/论文条数；题目与论文的身份识别留待内容复核。\n")
    lines.append("## 待核验 / 下一步")
    lines.append("1. 题目/论文资源身份按内容复核（本阶段仅来源/文件名提示，未核验正文）。")
    lines.append("2. CPMCM 候选来源按内容确认后归研赛，不新建第 20 入口。")
    lines.append("3. 5 个缺失模板目录（认证杯/泰迪杯/中青杯/深圳杯/小美赛）待下载补充。")
    lines.append("4. 空目录赛事（题目/论文为 0）按「下载补充中」管理，不宣告齐全。")
    lines.append("5. 授权/许可核验与可发布状态留待后续批次（本阶段不触碰付费 API、不做 LLM 标注）。")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 自检（工作区临时夹具，不落入原始资料目录）                                    #
# --------------------------------------------------------------------------- #

def _mini_registry():
    return {
        "dataset_root": "FIXTURE",
        "competitions": [
            {"competition_id": "fa", "source_dir_name": "01_fa",
             "sources": [
                 {"source_root": "problems", "relative_path": "problems/01_fa", "role_hint": ""},
                 {"source_root": "papers_pending_auth", "relative_path": "papers_pending_auth/01_fa", "role_hint": ""},
                 {"source_root": "templates", "relative_path": "templates/01_fa", "role_hint": ""},
             ],
             "material_status": "待下载", "template_status": "待核验"},
            {"competition_id": "fb", "source_dir_name": "02_fb",
             "sources": [
                 {"source_root": "problems", "relative_path": "problems/02_fb", "role_hint": ""},
                 {"source_root": "papers_pending_auth", "relative_path": "papers_pending_auth/02_fb", "role_hint": ""},
             ],
             "material_status": "待下载", "template_status": "待下载"},
        ],
        "shared_sources": [
            {"source_root": "templates", "relative_path": "templates/00_通用模板", "role_hint": ""}
        ],
    }


def _write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class _FakeStat:
    def __init__(self, mode, size=0, mtime_ns=0, file_attributes=0):
        self.st_mode = mode
        self.st_size = size
        self.st_mtime_ns = mtime_ns
        self.st_file_attributes = file_attributes


class _FakeEntry:
    def __init__(self, name, path, mode, file_attributes=0):
        self.name = name
        self.path = str(path)
        self._st = _FakeStat(mode, 0, 0, file_attributes)

    def stat(self, follow_symlinks=False):
        return self._st


def run_selftest(report_path):
    import subprocess
    import shutil

    global _scandir_entries
    real_scandir_entries = _scandir_entries

    results = []

    def check(name, passed, detail=""):
        if passed is None:
            results.append({"name": name, "result": "skip", "detail": detail})
        else:
            results.append({"name": name, "result": "pass" if passed else "fail", "detail": detail})

    def with_scandir(fake, fn):
        global _scandir_entries
        saved = _scandir_entries
        _scandir_entries = fake
        try:
            return fn()
        finally:
            _scandir_entries = saved

    tmp = Path(tempfile.mkdtemp(prefix="inv_selftest_"))
    try:
        data_root = tmp / "data"
        reg = _mini_registry()

        # 1) 注册 ID 唯一（用真实注册表）
        real_reg = load_json(Path("config/competition_registry.json"))
        ids = [c["competition_id"] for c in real_reg["competitions"]]
        dirs = [c["source_dir_name"] for c in real_reg["competitions"]]
        check("十九注册 ID 唯一且共 19 个", len(ids) == 19 and len(set(ids)) == 19,
              "ids={}".format(ids))
        check("十九注册 目录名 唯一且共 19 个", len(dirs) == 19 and len(set(dirs)) == 19)

        # 2) 空目录
        _write_bytes(data_root / "problems" / "01_fa" / "README.txt", b"hi")
        _write_bytes(data_root / "problems" / "01_fa" / "a" / "x", b"")
        (data_root / "problems" / "02_fb").mkdir(parents=True, exist_ok=True)
        res = run_scan(data_root, reg, "full")
        codes = [i["code"] for i in res["issues"]]
        check("空目录被提示为状态信息（empty_dir）", "empty_dir" in codes)
        check("空目录不算错误", all(i["severity"] != "error" for i in res["issues"] if i["code"] == "empty_dir"))

        # 3) 同字节不同文件名的重复组
        _write_bytes(data_root / "papers_pending_auth" / "01_fa" / "p1.pdf", b"SAME_CONTENT_123")
        _write_bytes(data_root / "papers_pending_auth" / "01_fa" / "p2_copy.pdf", b"SAME_CONTENT_123")
        res = run_scan(data_root, reg, "full")
        dup_paths = [set(d["paths"]) for d in res["duplicates"] if d["size"] == len(b"SAME_CONTENT_123")]
        check("同字节不同文件名生成重复组（保留全部来源）",
              any({"papers_pending_auth/01_fa/p1.pdf", "papers_pending_auth/01_fa/p2_copy.pdf"} == s for s in dup_paths),
              "duplicates={}".format(res["duplicates"]))

        # 4) 文件修改后 hash/计数更新
        before_hash = {r["relative_path"]: r["sha256"] for r in res["records"] if r["status"] == "ok"}
        _write_bytes(data_root / "papers_pending_auth" / "01_fa" / "p1.pdf", b"CHANGED_CONTENT")
        res2 = run_scan(data_root, reg, "full")
        after_hash = {r["relative_path"]: r["sha256"] for r in res2["records"] if r["status"] == "ok"}
        check("文件修改后 hash 更新", after_hash["papers_pending_auth/01_fa/p1.pdf"] != before_hash["papers_pending_auth/01_fa/p1.pdf"],
              "{} -> {}".format(before_hash["papers_pending_auth/01_fa/p1.pdf"][:8], after_hash["papers_pending_auth/01_fa/p1.pdf"][:8]))
        check("文件修改后有效计数不累加（数量一致）", len(after_hash) == len(before_hash))

        # 5) 删除文件后不保留旧记录
        os.remove(str(data_root / "papers_pending_auth" / "01_fa" / "p2_copy.pdf"))
        res3 = run_scan(data_root, reg, "full")
        paths3 = [r["relative_path"] for r in res3["records"]]
        check("删除文件后不再出现旧记录", "papers_pending_auth/01_fa/p2_copy.pdf" not in paths3)

        # 6) 未知来源有记录
        _write_bytes(data_root / "zzz_unknown_root" / "x.bin", b"data")
        _write_bytes(data_root / "problems" / "99_unknown_comp" / "y.txt", b"data")
        res4 = run_scan(data_root, reg, "full")
        codes4 = [i["code"] for i in res4["issues"]]
        unattributed_paths = [r["relative_path"] for r in res4["records"] if r["scope"] == "unattributed"]
        check("未知顶层目录有 uncovered_top 提示", "uncovered_top" in codes4)
        check("未匹配目录文件归入未归属", "problems/99_unknown_comp/y.txt" in unattributed_paths)

        # 7) 临时下载后缀 → pending
        _write_bytes(data_root / "problems" / "01_fa" / "dl.pdf.part", b"partial")
        res5 = run_scan(data_root, reg, "full")
        stat5 = {r["relative_path"]: r["status"] for r in res5["records"]}
        check("临时下载后缀文件标 pending 不作有效记录",
              stat5.get("problems/01_fa/dl.pdf.part") == "pending")

        # 8) 读取失败 / 文件变化异常不能标成功
        ok_read = read_with_check(Path(data_root) / "problems" / "01_fa" / "a")  # 目录
        fail_read = read_with_check(Path(data_root) / "does_not_exist.bin")
        check("读取目录/不存在路径返回 failed 非 ok",
              ok_read["status"] == "failed" and fail_read["status"] == "failed")

        # 9) 相同输入重复运行稳定
        resA = run_scan(data_root, reg, "full")
        resB = run_scan(data_root, reg, "full")
        recA = [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in resA["records"]]
        recB = [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in resB["records"]]
        check("相同输入重复运行记录稳定一致", recA == recB)

        # 10) 缓存模式命中不误标 full
        cache_path = tmp / "cache.json"
        run_scan(data_root, reg, "full", cache_path=cache_path)
        resC = run_scan(data_root, reg, "cached", cache_path=cache_path)
        cached_files = [r for r in resC["records"] if r["hash_source"] == "cached" and r["status"] == "ok"]
        check("缓存模式按 size/mtime 复用 hash", len(cached_files) > 0)

        # ================= R1/R2/R3 回归（独立夹具根 rt） =================
        rt = tmp / "rt"
        _write_bytes(rt / "problems" / "01_fa" / "good.txt", b"hello")
        b35 = b"0123456789" * 3 + b"01234"  # 35 字节
        _write_bytes(rt / "problems" / "01_fa" / "r2.bin", b35)
        _write_bytes(rt / "problems" / "01_fa" / "empty.dat", b"")
        _write_bytes(rt / "problems" / "02_fb" / "blocked" / "hidden.txt", b"secret")

        # R2-1 读取产出的 size 与 hash 同源一致
        info = read_with_check(rt / "problems" / "01_fa" / "r2.bin")
        check("R2 读取 size 与 hash 同源一致（35 字节）",
              info["status"] == "ok" and info["size"] == 35 and info["sha256"] == hashlib.sha256(b35).hexdigest(),
              "size={} hash={}".format(info["size"], (info["sha256"] or "")[:8]))

        # R2-2 读取中 size 8→35 变化 → unstable（用 _stat 注入稳定复现）
        def stat_seq(sizes):
            it = iter(sizes)
            def fn(p):
                s = next(it)
                return SimpleNamespace(st_mode=stat.S_IFREG, st_size=s, st_mtime_ns=1000 + s)
            return fn
        info2 = read_with_check("x", _stat=stat_seq([8, 35]), _stream=lambda p: "deadbeef")
        check("R2 读取中 size 8→35 变化 → unstable 非 ok",
              info2["status"] == "unstable" and info2["sha256"] is None)

        # R2-3 空文件稳定性确认
        infoE = read_with_check(rt / "problems" / "01_fa" / "empty.dat")
        check("R2 空文件稳定确认 → empty 且 size 0",
              infoE["status"] == "empty" and infoE["size"] == 0 and infoE["sha256"] == EMPTY_SHA256)
        infoE2 = read_with_check("e", _stat=stat_seq([0, 5]), _stream=lambda p: "x")
        check("R2 空文件前后变化 → unstable", infoE2["status"] == "unstable")

        # R1-1 深层目录枚举失败 → error issue（含路径），不猜内部文件数
        blocked_dir = rt / "problems" / "02_fb" / "blocked"
        def raising_scandir(path):
            if str(path) == str(blocked_dir):
                raise PermissionError(13, "Permission denied", str(path))
            return real_scandir_entries(str(path))
        res_r1 = with_scandir(raising_scandir, lambda: run_scan(rt, reg, "full"))
        err_r1 = [i for i in res_r1["issues"] if i["severity"] == "error" and i["code"] == "dir_enumerate_failed"]
        rec_paths_r1 = [r["relative_path"] for r in res_r1["records"]]
        check("R1 深层目录枚举失败 → error issue 且带路径", len(err_r1) >= 1 and "blocked" in err_r1[0]["message"])
        check("R1 无法枚举目录不猜内部文件数", "problems/02_fb/blocked/hidden.txt" not in rec_paths_r1)

        # R1-2 已发现文件读取/stat 失败 → failed 记录，清单与统计一致
        vanished_path = rt / "problems" / "01_fa" / "vanished.txt"
        def ghost_scandir(path):
            if str(path) == str(rt / "problems" / "01_fa"):
                entries = list(real_scandir_entries(str(path)))
                entries.append(_FakeEntry("vanished.txt", vanished_path, stat.S_IFREG))
                entries.sort(key=lambda e: e.name)
                return entries
            return real_scandir_entries(str(path))
        res_r12 = with_scandir(ghost_scandir, lambda: run_scan(rt, reg, "full"))
        failed_recs = [r for r in res_r12["records"] if r["status"] == "failed"]
        fr = next((r for r in failed_recs if r["relative_path"].endswith("vanished.txt")), None)
        check("R1 已发现文件读取失败 → failed 记录", fr is not None)
        check("R1 failed 记录 size/mtime/sha256 为空",
              fr is not None and fr["size"] is None and fr["mtime_ns"] is None and fr["sha256"] is None)
        check("R1 failed_files 与逐文件记录一致", res_r12["stats"]["failed_files"] == len(failed_recs))
        check("R1 failed 进入 issues 且为 error",
              any(i["code"] == "file_failed" and i["severity"] == "error" for i in res_r12["issues"]))

        # R1-3 CLI 非零退出（真实子进程，数据根不存在）
        script = Path(os.path.abspath(__file__))
        real_reg_path = script.parent.parent / "config" / "competition_registry.json"
        cli_out = tmp / "cli_out"
        proc = subprocess.run(
            [sys.executable, str(script), "--data-root", str(tmp / "no_such_root"),
             "--registry", str(real_reg_path), "--out", str(cli_out)],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        check("R1 CLI 数据根不存在 → 退出码非零", proc.returncode != 0, "rc={}".format(proc.returncode))

        # R3-1 单元：reparse 属性位判定
        check("R3 _is_reparse_attrs 判定 reparse/普通",
              _is_reparse_attrs(FILE_ATTRIBUTE_REPARSE_POINT) is True and _is_reparse_attrs(0) is False)

        # R3-2 嵌套 reparse 目录/文件被跳过，不读目标
        def reparse_scandir(path):
            if str(path) == str(rt / "problems" / "01_fa"):
                entries = list(real_scandir_entries(str(path)))
                entries.append(_FakeEntry("linkdir", rt / "problems" / "01_fa" / "linkdir",
                                         stat.S_IFDIR, FILE_ATTRIBUTE_REPARSE_POINT))
                entries.append(_FakeEntry("linkfile.bin", rt / "problems" / "01_fa" / "linkfile.bin",
                                         stat.S_IFREG, FILE_ATTRIBUTE_REPARSE_POINT))
                entries.sort(key=lambda e: e.name)
                return entries
            return real_scandir_entries(str(path))
        res_r3 = with_scandir(reparse_scandir, lambda: run_scan(rt, reg, "full"))
        reparse_msgs = [i["message"] for i in res_r3["issues"] if i["code"] == "reparse_skipped"]
        rec_paths_r3 = [r["relative_path"] for r in res_r3["records"]]
        check("R3 嵌套 reparse 目录/文件被跳过并记录",
              any("linkdir" in m for m in reparse_msgs) and any("linkfile.bin" in m for m in reparse_msgs))
        check("R3 reparse 目标未被当作文件读取",
              not any("linkdir" in p or "linkfile.bin" in p for p in rec_paths_r3))

        # R3-3 顶层来源为 reparse → 跳过，不深入
        tiny = tmp / "tiny"
        tiny.mkdir(parents=True, exist_ok=True)
        def top_reparse_scandir(path):
            if str(path) == str(tiny):
                return [_FakeEntry("problems", tiny / "problems", stat.S_IFDIR, FILE_ATTRIBUTE_REPARSE_POINT)]
            return real_scandir_entries(str(path))
        res_top = with_scandir(top_reparse_scandir, lambda: run_scan(tiny, reg, "full"))
        check("R3 顶层来源为 reparse → 跳过且不产生文件",
              any(i["code"] == "reparse_skipped" and "problems" in i["message"] for i in res_top["issues"])
              and res_top["stats"]["total_files"] == 0)

        # R3-4 真实 Windows 链接/junction 夹具不可建 → 记 skip（不以 mock 冒充真实通过）
        check("R3 实际 Windows symlink/junction 夹具", None,
              "skip：本机建 symlink 遇 WinError1314、junction 退出1（见 stage01_codex_evidence.json），改用 mock reparse 验证")

        # R2-4 缓存删除/变化后不残留旧条目（同一 cache 文件重跑）
        cp = tmp / "rtcache.json"
        run_scan(rt, reg, "full", cache_path=cp)
        cache_before = load_json(cp)
        _write_bytes(rt / "problems" / "01_fa" / "good.txt", b"changed-content!")
        os.remove(str(rt / "problems" / "01_fa" / "r2.bin"))
        run_scan(rt, reg, "full", cache_path=cp)
        cache_after = load_json(cp)
        check("R2 缓存删除后不残留旧条目", "problems/01_fa/r2.bin" not in cache_after)
        check("R2 缓存变化后条目 hash 更新",
              cache_after.get("problems/01_fa/good.txt", {}).get("sha256")
              != cache_before.get("problems/01_fa/good.txt", {}).get("sha256"))
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
        _scandir_entries = real_scandir_entries

    ok = all(r["result"] != "fail" for r in results)
    n_pass = sum(1 for r in results if r["result"] == "pass")
    n_fail = sum(1 for r in results if r["result"] == "fail")
    n_skip = sum(1 for r in results if r["result"] == "skip")
    report = {"generated_at": now_utc_iso(), "passed": ok, "pass": n_pass, "fail": n_fail, "skip": n_skip,
              "results": results}
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for r in results:
        tag = {"pass": "[PASS]", "fail": "[FAIL]", "skip": "[SKIP]"}[r["result"]]
        print("{tag} {name}{detail}".format(
            tag=tag, name=r["name"],
            detail=("  -- " + r["detail"]) if r.get("detail") else ""))
    print("自检{}：{} 通过 / {} 失败 / {} 跳过".format(
        "通过" if ok else "失败", n_pass, n_fail, n_skip))
    return ok


def main():
    reconfigure_stdout()
    parser = argparse.ArgumentParser(description="资料盘点（阶段01）：扫描 D:/Erdos_data 原件，产出逐文件记录/重复组/覆盖矩阵/问题清单")
    parser.add_argument("--data-root", type=Path, default=Path("D:/Erdos_data"), help="数据集根目录（只读）")
    parser.add_argument("--registry", type=Path, default=Path("config/competition_registry.json"), help="赛事注册表")
    parser.add_argument("--out", type=Path, default=Path("out/inventory"), help="产物输出目录")
    parser.add_argument("--hash-mode", choices=["full", "cached"], default="full", help="full=全量重算；cached=按 size/mtime 复用缓存")
    parser.add_argument("--cache-file", type=Path, default=None, help="阶段专用 hash 缓存路径（默认 --out/.hash_cache.json）")
    parser.add_argument("--selftest", action="store_true", help="运行工作区临时夹具自检")
    parser.add_argument("--selftest-report", type=Path, default=None, help="自检报告 JSON 输出路径")
    args = parser.parse_args()

    if args.selftest:
        ok = run_selftest(args.selftest_report)
        sys.exit(0 if ok else 1)

    data_root = Path(args.data_root)
    registry = load_json(args.registry)
    out_dir = Path(args.out)
    cache_path = args.cache_file or (out_dir / CACHE_FILENAME)

    result = run_scan(data_root, registry, args.hash_mode, cache_path=cache_path)
    write_outputs(out_dir, result, data_root, registry, args.hash_mode, cache_path)

    s = result["stats"]
    print("盘点完成（{} 模式）：文件总数 {}，有效 {}，空 {}，pending {}，failed {}，unstable {}；重复组 {}；缓存命中 {}、全量 {}。".format(
        args.hash_mode, s["total_files"], s["ok_files"], s["empty_files"], s["pending_files"],
        s["failed_files"], s["unstable_files"], s["duplicate_groups"], s["cached_hashes"], s["full_hashes"]))
    has_err = any(i["severity"] == "error" for i in result["issues"])
    print("问题清单 {} 条（其中 error {} 条）；产物写入 {}".format(
        len(result["issues"]), sum(1 for i in result["issues"] if i["severity"] == "error"), out_dir))
    sys.exit(1 if has_err else 0)


if __name__ == "__main__":
    main()
