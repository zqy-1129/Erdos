"""案例标注（工作包3）：papers_pending_auth 论文 PDF → 结构化标注。

流程：
1. 按 config/cases_v0.yaml 遍历源目录（排除评委点评/题目文件，非 pdf 记 backlog）；
2. pdftotext 提取正文 → 解析 年份/题号/标题/关键词/摘要；
3. 按 config/method_tags_v0.yaml 词典在 标题/关键词/摘要 三区加权打分，产出方法标签；
4. 人工修正经 config/cases_v0_overrides.yaml 回灌；
5. 输出 out/cases_report.json（供复核与 build_case_pack.py 消费）。

用法：python scripts/annotate_cases.py [--match 子串] [--limit N]
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

CONTENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_problem_text import decode_pdf_text, find_pdftotext, reconfigure_stdout  # noqa: E402

DEFAULT_CASES_CONFIG = CONTENT_DIR / "config" / "cases_v0.yaml"
DEFAULT_TAGS_CONFIG = CONTENT_DIR / "config" / "method_tags_v0.yaml"
REPORT_PATH = CONTENT_DIR / "out" / "cases_report.json"

HEADER_SKIP = re.compile(
    r"^(problem chosen.*"
    r"|for office use only.*"
    r"|.*office use only.*"
    r"|\d+\s*$"
    r"|[a-f]\s*$"
    r"|[a-f]\s+\d{4,}\s*$"
    r"|.*summary sheet.*"
    r"|.*mcm/?icm.*"
    r"|team control number.*"
    r"|t\d[\s_].*"
    r"|f\d[\s_].*"
    r"|page \d+\s*(of|/)\s*\d+.*"
    r"|team\s*#?\s*\d+.*"
    r"|^\d+(?:\.\d+){0,3}[\s.、:：]+.{0,4}(?:background|introduction|methods?|analysis|models?|conclusions?|results?|discussion|references?|appendix|contents|abstract|summary)\b.*"
    r"|20\d{2}\s*$"
    r"|(?:summary|abstract)\s*$)",
    re.I,
)
HYPHEN_WRAP = re.compile(r"[-\u2010-\u2015]+\s*\n\s*")
# pdftotext 输出含连字（ﬁ/ﬂ 等），先归一才能命中英文词表
LIGATURES = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st"})
LONG_LINE = {"mcm": 70, "cumcm": 35}
CUMCM_BANNER = re.compile(r"高教社杯|数学建模竞赛|优秀论文")
KW_CN = re.compile(r"关\s*键\s*词\s*[:：]?\s*(.+)")
KW_EN = re.compile(r"key\s*-?\s*words?\s*[:：]?\s*(.+)", re.I)
A_MCM = re.compile(r"^\s*(summary|abstract)\b(?![\t :：]*sheet)", re.I | re.M)
A_CUMCM = re.compile(r"^\s*(?:[一二三四五六七八九十\d]+[、.．]?\s*)?摘\s*要", re.M)
CJK = re.compile(r"[\u4e00-\u9fff]")
# 获奖断言从紧：仅认正文/目录/文件名中的显式证据，否则退为集合标签「优秀论文」
AWARD_OUTSTANDING = re.compile(r"outstanding\s+(?:winner|paper|award)", re.I)
FOLDER_AWARD = re.compile(r"([OFMH])奖")
FOLDER_AWARD_NAME = {"O": "Outstanding", "F": "Finalist", "M": "Meritorious", "H": "Honorable Mention"}
# MCM 摘要页模板样板文字（2016-2019 旧模板论文中普遍存在，须在抽摘要前剥离）
BOILERPLATE = re.compile(
    r"(?:\(?\s*Attach\s+a\s+copy\s+of\s+this\s+page\s+to\s+your\s+solution\s+paper\.?\s*\)?\s*)?"
    r"Type\s+a\s+summary\s+of\s+your\s+results\s+on\s+this\s+page\.\s*"
    r"Do\s+not\s+include\s+the\s+name\s+of\s+your\s+school,\s*advisor,\s*"
    r"or\s+team\s+members\s+on\s+this\s+page\.",
    re.I,
)
# 模板占位标题（作者未替换）不视为有效标题
PLACEHOLDER_TITLES = {"论文标题", "标题", "title", "your title"}


def extract_text(path):
    proc = subprocess.run(
        [str(find_pdftotext()), "-enc", "UTF-8", "-layout", str(path), "-"],
        capture_output=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:200])
    return decode_pdf_text(proc.stdout)


def parse_mcm_rel(rel):
    """papers_pending_auth/01_.../{YEAR}年[/{CODE}|「{YEAR}年美赛{X}题O奖论文」]/xxx.pdf"""
    parts = rel.parts
    for i, part in enumerate(parts):
        m = re.match(r"^(\d{4})年$", part)
        if m:
            year = int(m.group(1))
            code = None
            if i + 2 < len(parts):
                sub = parts[i + 1]
                if re.match(r"^[A-Fa-f]$", sub):
                    code = sub.upper()
                else:
                    m2 = re.search(r"([A-F])题", sub)
                    if m2:
                        code = m2.group(1)
            return year, code
    return None, None


def folder_award(rel):
    """目录名奖项提示（如「2024年美赛A题O奖论文」→ (Outstanding, 目录名)）；无则 None。"""
    for part in rel.parts[:-1]:
        m = FOLDER_AWARD.search(part)
        if m:
            return FOLDER_AWARD_NAME[m.group(1)], part
    return None


def parse_cumcm_rel(rel):
    """papers_pending_auth/12_.../{YEAR}年.../xxx.pdf → year 取目录；code 取文件名"""
    year = None
    for part in rel.parts:
        m = re.match(r"^(\d{4})年", part)
        if m:
            year = int(m.group(1))
            break
    return year, extract_cumcm_code(rel.stem)


def extract_cumcm_code(stem):
    m = re.search(r"([A-E])\s*\d*\s*题", stem)
    if m:
        return m.group(1)
    m = re.search(r"等奖+\s*([A-E])(?![a-z])", stem)
    if m:
        return m.group(1)
    m = re.match(r"^([A-E])\d+", stem)
    if m:
        return m.group(1)
    m = re.match(r"^\d{2}([a-e])\d+", stem)
    if m:
        return m.group(1).upper()
    m = re.match(r"^20\d{2}年?([A-E])", stem)
    if m:
        return m.group(1)
    return None


def mcm_title(combined, long_threshold=70):
    """标题提取：优先取 Summary/Abstract 锚点之前的行；锚点后（如 "Summary Sheet" 头部
    之下）或全程无锚点时，取首个「长行」（摘要正文首句）之前的行。"""
    lines = [ln.strip() for ln in combined.split("\n")]
    anchor = None
    for i, ln in enumerate(lines):
        if re.match(r"^(summary|abstract)\b", ln, re.I):
            anchor = i
            break

    def collect(start, end, stop_on_long):
        cands = []
        for ln in lines[start:end]:
            if not ln or HEADER_SKIP.match(ln):
                continue
            if stop_on_long and len(ln) > long_threshold:
                break
            if len(ln) < 6:
                continue
            cands.append(ln)
            if len(cands) >= 4:
                break
        return cands

    if anchor is not None:
        cands = collect(0, anchor, False) or collect(anchor + 1, len(lines), True)
    else:
        cands = collect(0, len(lines), True)
    if not cands:
        return None, []
    if max(len(c) for c in cands) < 80:
        title = " ".join(cands)
    else:
        title = max(cands, key=len)
    return title, cands


def cumcm_title(page1, stem):
    lines = [ln.strip() for ln in page1.split("\n")]
    anchor = None
    for i, ln in enumerate(lines):
        if A_CUMCM.match(ln):
            anchor = i
            break
    cands = []
    if anchor is not None:
        pre = [ln for ln in lines[:anchor] if ln]
        pre = [ln for ln in pre if not (CUMCM_BANNER.search(ln) and len(ln) > 15)]
        cands = pre
    if cands:
        if len(cands) <= 2 and max(len(c) for c in cands) < 60:
            return " ".join(cands), cands
        return cands[-1], cands
    # 文件名兜底：去 [..] 前缀后取最后一个冒号段
    s = re.sub(r"^\[[^\]]*\]\s*", "", stem)
    s = re.split(r"[：:]", s)[-1].strip()
    return (s, []) if s else (None, [])


def find_keywords(combined):
    m = KW_CN.search(combined) or KW_EN.search(combined)
    if not m:
        return ""
    return m.group(1).strip().split("\n")[0][:300]


def find_abstract(combined, competition):
    pat = A_CUMCM if competition == "cumcm" else A_MCM
    m = pat.search(combined)
    if m:
        abstract = combined[m.end(): m.end() + 5000]
    else:
        # 无显式 Summary/Abstract 标题：摘要自首个「长行」（段首句）起
        mm = re.search(r"^\S.{%d,}" % LONG_LINE.get(competition, 70), combined, re.M)
        if not mm:
            return ""
        abstract = combined[mm.start(): mm.start() + 5000]
    km = KW_CN.search(abstract) or KW_EN.search(abstract)
    if km:
        abstract = abstract[: km.start()]
    return abstract


def award_of(competition, stem, combined, folder_hint=None):
    if competition == "mcm":
        if folder_hint:
            return folder_hint
        m = AWARD_OUTSTANDING.search(combined[:6000])
        if m:
            return "Outstanding", [m.group(0)]
        return "优秀论文", []
    if "MATLAB创新奖" in stem or "matlab创新奖" in stem.lower():
        return "MATLAB创新奖", []
    if "SPSS创新奖" in stem or "spss创新奖" in stem.lower():
        return "SPSS创新奖", []
    if "高教杯奖" in stem or "高教社杯奖" in stem:
        return "高教社杯奖", []
    return "优秀论文", []


def sample_slug(year, code, stem):
    """从文件名取稳定短标识（题号字母+编号等）；取不到返回 None 走序号兜底。"""
    s = re.sub(r"[^0-9a-zA-Z]+", "", stem)
    m = re.match(r"^" + re.escape(code) + r"(\d+)", s, re.I)
    if m:
        return m.group(1)
    m = re.match(r"^\d{2}" + re.escape(code) + r"(\d+)$", s, re.I)
    if m:
        return m.group(1).zfill(2)
    m = re.match(r"^20\d{2}" + re.escape(code) + r"(\d+)", s, re.I)
    if m:
        return m.group(1).zfill(2)
    if re.match(r"^\d+$", s):
        return s
    return None


def iter_sources(root, cfg):
    for src in cfg["sources"]:
        base = root / src["root"]
        if not base.exists():
            print("[WARN] 源目录不存在：{}".format(base))
            continue
        skip_dirs = set(src.get("skip_top_dirs", []))
        exclude_pats = [re.compile(p) for p in src.get("exclude_stem_patterns", [])]
        process_ext = set(src.get("process_ext", [".pdf"]))
        backlog_ext = set(src.get("backlog_ext", []))
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            rel_parts = rel.relative_to(src["root"]).parts
            if rel_parts and rel_parts[0] in skip_dirs:
                yield src, rel, path, "skip_dir"
                continue
            ext = path.suffix.lower()
            if ext in backlog_ext:
                yield src, rel, path, "backlog"
                continue
            if ext not in process_ext:
                yield src, rel, path, "skip_ext"
                continue
            if any(p.search(path.stem) for p in exclude_pats):
                yield src, rel, path, "excluded"
                continue
            yield src, rel, path, "process"


def build_regex_dict(tags_cfg):
    out = []
    for entry in tags_cfg["tags"]:
        out.append(
            {
                "tag": entry["tag"],
                "patterns": [re.compile(p, re.I) for p in entry["patterns"]],
            }
        )
    return out


def score_tags(title, keywords, abstract, body, tag_dict):
    zones = [
        ("keywords", keywords, 3),
        ("title", title or "", 2),
        ("abstract", abstract, 2),
        ("body", body, 1),
    ]
    accepted = []
    for entry in tag_dict:
        score = 0
        hit_zones = []
        for zname, ztext, weight in zones:
            if not ztext:
                continue
            for pat in entry["patterns"]:
                if pat.search(ztext):
                    score += weight
                    hit_zones.append(zname)
                    break
        if score >= 2:
            accepted.append({"tag": entry["tag"], "score": score, "zones": hit_zones})
    accepted.sort(key=lambda t: (-t["score"], t["tag"]))
    return accepted


def resolve_conflicts(accepted, conflicts):
    tagset = {t["tag"] for t in accepted}
    drop = set()
    for rule in conflicts:
        when = rule["when"]
        when = [when] if isinstance(when, str) else when
        if any(w in tagset for w in when):
            drop.update(rule["remove"])
    return [t for t in accepted if t["tag"] not in drop]


def apply_overrides(item, ov):
    rel = item["rel_path"]
    if rel in ov.get("code_overrides", {}):
        item["code"] = ov["code_overrides"][rel]
        item["flags"].append("code_override")
    if rel in ov.get("title_overrides", {}):
        item["title"] = ov["title_overrides"][rel]
        item["flags"].append("title_override")
    if rel in ov.get("tags_overrides", {}):
        item["tags"] = [
            {"tag": t, "score": 99, "zones": ["manual"]} for t in ov["tags_overrides"][rel]
        ]
        item["flags"].append("tags_override")


def main():
    reconfigure_stdout()
    parser = argparse.ArgumentParser(description="案例标注")
    parser.add_argument("--config", type=Path, default=DEFAULT_CASES_CONFIG)
    parser.add_argument("--tags-config", type=Path, default=DEFAULT_TAGS_CONFIG)
    parser.add_argument("--match", default=None, help="仅处理 rel_path 含该子串的文件")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    tags_cfg = yaml.safe_load(args.tags_config.read_text(encoding="utf-8"))
    root = Path(cfg.get("dataset_root", "D:/Erdos_data"))
    ov_path = CONTENT_DIR / "config" / cfg.get("overrides", "cases_v0_overrides.yaml")
    ov = yaml.safe_load(ov_path.read_text(encoding="utf-8")) or {}
    exclude_set = set(ov.get("exclude", []))

    tag_dict = build_regex_dict(tags_cfg)
    conflicts = tags_cfg.get("conflicts", [])
    min_chars = cfg.get("min_text_chars", 3000)

    items = []
    stats = {
        "process": 0, "ok": 0, "no_text": 0, "no_title": 0, "tags_insufficient": 0,
        "excluded": 0, "backlog": 0, "skip_dir": 0, "skip_ext": 0,
        "manual_excluded": 0, "extract_fail": 0, "encoding_broken": 0,
    }
    backlog_list = []

    for src, rel, path, kind in iter_sources(root, cfg):
        rel_posix = rel.as_posix()
        if args.match and args.match not in rel_posix:
            continue
        if kind != "process":
            stats[kind if kind in stats else "excluded"] += 1
            if kind == "backlog":
                backlog_list.append(rel_posix)
            continue
        if rel_posix in exclude_set:
            stats["manual_excluded"] += 1
            continue
        if args.limit and stats["process"] >= args.limit:
            break
        stats["process"] += 1
        if stats["process"] % 50 == 0:
            print("...已处理 {} 个".format(stats["process"]))

        competition = src["competition"]
        if src["layout"] == "mcm_year_code":
            year, code = parse_mcm_rel(rel)
        else:
            year, code = parse_cumcm_rel(rel)

        item = {
            "rel_path": rel_posix, "competition": competition, "year": year, "code": code,
            "title": None, "title_candidates": [], "title_from": None,
            "keywords": "", "tags": [], "award": None, "award_evidence": [],
            "text_chars": 0, "flags": [],
        }
        try:
            text = extract_text(path)
        except Exception as exc:  # noqa: BLE001
            item["flags"].append("extract_fail")
            item["notes"] = str(exc)[:160]
            stats["extract_fail"] += 1
            items.append(item)
            continue

        pages = text.split("\f")
        combined = "\n".join(pages[:2]).translate(LIGATURES)
        combined_hy = BOILERPLATE.sub("", HYPHEN_WRAP.sub("", combined))
        item["text_chars"] = len(text)
        if len(text) < min_chars:
            item["flags"].append("no_text")
            stats["no_text"] += 1
            items.append(item)
            continue
        # 中文论文出现大段 CMap 损坏乱码（无汉字）时无法可靠标注
        if competition == "cumcm" and len(CJK.findall(text)) < 100:
            item["flags"].append("encoding_broken")
            stats["encoding_broken"] += 1
            items.append(item)
            continue

        if competition == "mcm":
            title, cands = mcm_title(combined_hy, long_threshold=LONG_LINE["mcm"])
        else:
            title, cands = cumcm_title(pages[0] if pages else "", path.stem)
        if title and title.strip().lower() in PLACEHOLDER_TITLES:
            title, cands = None, []
        if competition == "mcm":
            item["title_from"] = "content" if title else None
        else:
            item["title_from"] = "content" if (title and cands) else ("filename" if title else None)
        item["title"] = title.strip()[:120] if title else None
        item["title_candidates"] = cands[:4]

        item["keywords"] = find_keywords(combined_hy)
        abstract = find_abstract(combined_hy, competition)
        body = HYPHEN_WRAP.sub("", "\n".join(pages[2:]).translate(LIGATURES))
        item["award"], item["award_evidence"] = award_of(
            competition, path.stem, combined_hy, folder_award(rel))

        accepted = score_tags(item["title"], item["keywords"], abstract, body, tag_dict)
        accepted = resolve_conflicts(accepted, conflicts)
        item["tags"] = accepted

        apply_overrides(item, ov)

        if not item["title"]:
            item["flags"].append("no_title")
            stats["no_title"] += 1
        if len(item["tags"]) < 3:
            item["flags"].append("tags_insufficient")
            stats["tags_insufficient"] += 1
        if item["flags"] == []:
            stats["ok"] += 1
        items.append(item)

    # ---- 生成 business_id / problem_id（按 (comp,year,code) 分组、rel_path 排序分配序号）----
    # 同组内 slug 去重：文件 slug 冲突时改发序号（同一赛题的不同论文各自保留）
    seq_counter = {}
    used_slugs = {}
    for item in sorted(items, key=lambda x: x["rel_path"]):
        if item.get("flags") and "no_text" in item["flags"] and not item.get("year"):
            continue
        code = item.get("code")
        year = item.get("year")
        if not code or not year:
            item["flags"].append("no_code")
            item["business_id"] = None
            item["problem_id"] = None
            continue
        key = (item["competition"], year, code)
        used = used_slugs.setdefault(key, set())
        slug = sample_slug(year, code, Path(item["rel_path"]).stem)
        if not slug or slug in used:
            while True:
                seq_counter[key] = seq_counter.get(key, 0) + 1
                slug = "{:02d}".format(seq_counter[key])
                if slug not in used:
                    break
        used.add(slug)
        item["business_id"] = "{}-{}-{}-{}".format(item["competition"], year, code, slug)
        item["problem_id"] = "{}-{}-{}".format(item["competition"], year, code)

    eligible = [
        it for it in items
        if it.get("business_id") and it["title"] and len(it["tags"]) >= 3
    ]
    review = [
        it["rel_path"] for it in eligible
        if len(it["tags"]) < cfg.get("review_min_tags", 4)
        or len(it["title_candidates"]) > 2
        or "title_override" in it["flags"] or "code_override" in it["flags"]
        or "tags_override" in it["flags"]
    ]
    # business_id 冲突检测
    seen = {}
    id_conflicts = []
    for it in eligible:
        bid = it["business_id"]
        if bid in seen:
            id_conflicts.append([seen[bid], it["rel_path"]])
        seen[bid] = it["rel_path"]

    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tags_config": args.tags_config.name,
        "stats": dict(stats, eligible=len(eligible), review=len(review),
                      id_conflicts=len(id_conflicts)),
        "backlog": backlog_list,
        "id_conflicts": id_conflicts,
        "review": review,
        "items": items,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))

    print("---")
    print("标注完成：处理 {}，合格 {}，无文本层 {}，无标题 {}，标签不足 {}，"
          "乱码 {}，评委点评/题目排除 {}，backlog {}，抽取失败 {}".format(
              stats["process"], len(eligible), stats["no_text"], stats["no_title"],
              stats["tags_insufficient"], stats["encoding_broken"], stats["excluded"],
              stats["backlog"], stats["extract_fail"]))
    print("人工复核清单 {} 条（见 report.review）；business_id 冲突 {} 处".format(
        len(review), len(id_conflicts)))
    print("报告 → {}".format(REPORT_PATH))


if __name__ == "__main__":
    main()
