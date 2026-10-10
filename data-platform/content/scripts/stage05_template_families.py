"""阶段05：十九赛事格式与内容模板体系（template_family）。

- 为注册表中固定的 19 个 competition_id 各自生成一个独立、自包含的 template_family。
- family 由 ruleset / base_outline / writing_policy / figure_style / table_style / layout(latex+docx) 构成，
  实体 + 资产 + 真实文件逐一落盘，资产 sha256 为文件精确字节 hash（身份不依赖内容 hash）。
- 已有赛事模板源码（D:/Erdos_data/templates/<id>/<lang>-latex）优先复制复用；
  官方 docx 仅 cumcm/mcm 在 out/templates 存在，复制复用；其余用 python-docx 现场生成真实 docx。
- 缺失资料的五赛事（认证杯/泰迪杯/中青杯/深圳杯/小美赛）用自研通用框架/样式回退，
  并在矩阵与消费信息中明确标注“通用草稿，赛事规则待核验”，不冒充官方。
- 官方规则离线无法核验：ruleset.verification_status=unknown，不硬编码字体/字号/页数/页边距。
- 图表样式用 matplotlib + 合成数据生成独立样图并标注 fixture 身份；LaTeX 无编译器保持未编译，
  仅产出可审核源码与依赖清单。

用法（工作目录 D:/Erdos/data-platform/content）：
    <pytorch>/python scripts/stage05_template_families.py
    <pytorch>/python scripts/stage05_template_families.py --data-root D:/Erdos_data --out normalized/stage05
"""

import argparse
import hashlib
import json
import os
import shutil
import stat
import zipfile
from datetime import datetime
from pathlib import Path
import content_runtime as rt
import stage03_fileio as fio

CONTENT_DIR = Path(os.path.abspath(__file__)).parent.parent

# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_bytes(path, data):
    fio.write_exact(path,data)
    return sha256_bytes(data)


def write_text(path, text):
    return write_bytes(path, text.encode("utf-8"))


def json_bytes(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_json(path, obj):
    return write_bytes(path, json_bytes(obj))


def empty_license():
    return {"internal_analysis": False, "distribute_original": False,
            "distribute_profile": False, "send_to_third_party_model": False}


def stable_asset_id(owner_id, role_short, rel_path):
    """资产身份由 owner+role+源相对路径 hash 生成，不依赖内容 hash。"""
    digest = sha256_bytes(rel_path.encode("utf-8"))
    return "{}-{}-{}".format(owner_id, role_short, digest[:12])


# --------------------------------------------------------------------------- #
# 文档生成（DOCX）
# --------------------------------------------------------------------------- #

def make_docx(sections, lang, competition_id):
    """用 python-docx 现场生成真实 docx 模板（标题层级、页码、三线表样例）。"""
    import docx
    from docx import Document
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    doc.core_properties.created = datetime(2000,1,1)
    doc.core_properties.modified = datetime(2000,1,1)
    doc.core_properties.author = 'Erdos data engineering'
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    # 正文默认字体
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman" if lang == "en" else "SimSun"
    style.font.size = Pt(12)
    style.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'),'SimSun')

    title = ("Template — " + competition_id) if lang == "en" else ("模板 — " + competition_id)
    doc.add_paragraph(title,style='Title')

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta.add_run("Erdos content template draft (pending verification, template fixture)")
    if lang == "zh":
        doc.add_paragraph("说明：通用草稿，赛事规则待核验；本章节结构仅为基础框架，非官方规定。")

    doc.add_heading("Abstract" if lang == "en" else "摘要", level=1)
    doc.add_paragraph("(Fill with the current task results.)" if lang == "en" else "（此处填写当前任务的摘要，由求解结果填充。）")

    for sec_type, sec_title, repeat, optional in sections:
        if sec_type in ("abstract",):
            continue
        suffix = "" if not optional else (" (可选)" if lang == "zh" else " (optional)")
        rep_note = "" if repeat == "once" else (" （按小问展开）" if lang == "zh" else " (per subproblem)")
        doc.add_heading(sec_title + suffix + rep_note, level=1)
        doc.add_paragraph("（此处为当前任务求解结果填充区域，不预填历史结论。）" if lang == "zh"
                          else "(To be filled with the current task's solution results.)")

    # 三线表样例
    doc.add_heading("Table example" if lang == "en" else "表格示例", level=1)
    table = doc.add_table(rows=3, cols=3)
    table.style = None
    borders=OxmlElement('w:tblBorders')
    for edge in ['top','bottom','left','right','insideH','insideV']:
        element=OxmlElement('w:'+edge);element.set(qn('w:val'),'single' if edge in ['top','bottom'] else 'nil');element.set(qn('w:sz'),'8');borders.append(element)
    table._tbl.tblPr.append(borders)
    for cell in table.rows[0].cells:
        cellb=OxmlElement('w:tcBorders');edge=OxmlElement('w:bottom');edge.set(qn('w:val'),'single');edge.set(qn('w:sz'),'8');cellb.append(edge);cell._tc.get_or_add_tcPr().append(cellb)
    headers = (["Item", "Value", "Unit"] if lang == "en" else ["项目", "数值", "单位"])
    for j, h in enumerate(headers):
        table.rows[0].cells[j].text = h
    for i in range(1, 3):
        for j in range(3):
            table.rows[i].cells[j].text = "—"

    # 页脚页码
    section = doc.sections[0]
    footer = section.footer
    p = footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    run = p.add_run()
    fld_begin = OxmlElement("w:fldChar"); fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText"); instr.set(qn("xml:space"), "preserve"); instr.text = "PAGE"
    fld_end = OxmlElement("w:fldChar"); fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin); run._r.append(instr); run._r.append(fld_end)

    import tempfile
    tmp = tempfile.mktemp(suffix=".docx")
    doc.save(tmp)
    return tmp


def normalize_zip(src, dst):
    """重打包 zip：条目按名排序、固定时间戳，保证同输入同字节（确定性）。"""
    with zipfile.ZipFile(src, "r") as zin:
        names = sorted(zin.namelist())
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                info = zin.getinfo(name)
                data = zin.read(name)
                zi = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                zi.create_system = info.create_system
                zout.writestr(zi, data)


def build_docx(sections, lang, competition_id, dst):
    tmp = make_docx(sections, lang, competition_id)
    import tempfile
    fd,norm=tempfile.mkstemp(suffix='.docx');os.close(fd)
    try:
        normalize_zip(tmp,norm)
        write_bytes(dst,Path(norm).read_bytes())
    finally:
        os.remove(tmp)
        os.remove(norm)
    return sha256_of(dst)


# --------------------------------------------------------------------------- #
# 图表样式预览（matplotlib + 合成数据，fixture）
# --------------------------------------------------------------------------- #

def _setup_chinese_font():
    try:
        import matplotlib
        from matplotlib import font_manager
        candidates = ["Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC", "WenQuanYi Zen Hei"]
        available = {f.name for f in font_manager.fontManager.ttflist}
        chosen = next((c for c in candidates if c in available), None)
        if chosen:
            matplotlib.rcParams["font.sans-serif"] = [chosen]
    except Exception:
        pass
    try:
        import matplotlib
        matplotlib.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass


def render_figure_preview(figure_style, lang, dst):
    import matplotlib
    import importlib.metadata,io
    matplotlib.__version__=importlib.metadata.version('matplotlib')
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    _setup_chinese_font()
    x = np.linspace(0, 10, 80)
    y1 = np.sin(x) * 2 + 0.3 * x
    y2 = np.cos(x) * 1.5 + 4
    fig, ax = plt.subplots(figsize=(6.0, 4.0), dpi=110)
    colors = figure_style.get("colors") or ["#1f77b4", "#d62728"]
    ax.plot(x, y1, color=colors[0], linewidth=figure_style.get("line_width") or 1.5, label="系列A (fixture)")
    ax.plot(x, y2, color=colors[1], linewidth=figure_style.get("line_width") or 1.5, label="系列B (fixture)")
    ax.set_xlabel("x / (单位)" if lang == "zh" else "x / (unit)")
    ax.set_ylabel("y / (单位)" if lang == "zh" else "y / (unit)")
    ax.set_title("合成数据样图（fixture，非历史结果）" if lang == "zh" else "Synthetic sample figure (fixture, not historical)")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.35)
    fig.tight_layout()
    buf=io.BytesIO();fig.savefig(buf,format='png');write_bytes(dst,buf.getvalue())
    plt.close(fig)


def render_table_preview(table_style, lang, dst):
    import matplotlib
    import importlib.metadata,io
    matplotlib.__version__=importlib.metadata.version('matplotlib')
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _setup_chinese_font()
    header = (["指标", "数值", "单位"] if lang == "zh" else ["Metric", "Value", "Unit"])
    data = [["α", "12.5", "—"], ["β", "3.2", "—"]]
    col_labels = header
    cell = [[h] + [r[i] for r in data] for i, h in enumerate([])]
    cell_data = [list(r) for r in data]
    fig, ax = plt.subplots(figsize=(5.0, 2.4), dpi=110)
    ax.axis("off")
    table = ax.table(cellText=cell_data, colLabels=col_labels, cellLoc="center", loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(12)
    table.scale(1, 1.6)
    # 三线表：仅上/下/表头下横线（matplotlib 表近似）
    for key, cell_ in table.get_celld().items():
        r = key[0]
        cell_.visible_edges = 'TB' if r==0 else 'B' if r==len(cell_data) else ''
        cell_.set_linewidth(1.0 if r in (0, 1) else 0.4)
        cell_.set_edgecolor("black")
    ax.set_title("三线表样式样例（fixture）" if lang == "zh" else "Three-line table style sample (fixture)")
    fig.tight_layout()
    buf=io.BytesIO();fig.savefig(buf,format='png');write_bytes(dst,buf.getvalue())
    plt.close(fig)


# --------------------------------------------------------------------------- #
# 大纲 / 写作策略 / 样式定义
# --------------------------------------------------------------------------- #

ZH_SECTIONS = [
    ("abstract", "摘要", "once", False),
    ("restatement", "问题重述", "once", False),
    ("assumptions", "模型假设", "once", False),
    ("symbols", "符号说明", "once", True),
    ("analysis", "问题分析", "per_subproblem", False),
    ("model", "模型建立", "per_subproblem", False),
    ("solve", "模型求解", "per_subproblem", False),
    ("validation", "模型检验", "per_subproblem", True),
    ("sensitivity", "敏感性分析", "once", True),
    ("conclusion", "模型评价与结论", "once", False),
    ("references", "参考文献", "once", False),
    ("appendix", "附录", "once", True),
]

EN_SECTIONS = [
    ("abstract", "Abstract", "once", False),
    ("restatement", "Problem Restatement", "once", False),
    ("assumptions", "Model Assumptions", "once", False),
    ("symbols", "Notations", "once", True),
    ("analysis", "Problem Analysis", "per_subproblem", False),
    ("model", "Model Formulation", "per_subproblem", False),
    ("solve", "Model Solution", "per_subproblem", False),
    ("validation", "Model Validation", "per_subproblem", True),
    ("sensitivity", "Sensitivity Analysis", "once", True),
    ("conclusion", "Evaluation and Conclusion", "once", False),
    ("references", "References", "once", False),
    ("appendix", "Appendix", "once", True),
]

# 赛事语言：依据赛事公开性质与实际模板文件存在性（zh/en）。MCM 与 APMCM 英文为英文。
LANG = {
    "mcm": "en", "apmcm_en": "en",
    "huashu": "zh", "statistics": "zh", "mathorcup": "zh", "renzheng": "zh",
    "teddy": "zh", "wuyi": "zh", "electrician": "zh", "apmcm_zh": "zh",
    "zhongqing": "zh", "shenzhen": "zh", "cumcm": "zh", "cpmcm": "zh",
    "shuwei": "zh", "xiaomeisai": "zh", "yangtze": "zh", "dongbei": "zh", "huazhong": "zh",
}


def build_outline(outline_id, competition_id, lang):
    secs = EN_SECTIONS if lang == "en" else ZH_SECTIONS
    return {
        "outline_id": outline_id,
        "competition_id": competition_id,
        "version": 3,
        "schema_version": 1,
        "sections": [
            {"section_type": st, "title": t, "repeat": rep, "optional": opt}
            for (st, t, rep, opt) in secs
        ],
        "is_fixture": False,
    }


def build_ruleset(ruleset_id, competition_id):
    return {
        "ruleset_id": ruleset_id,
        "competition_id": competition_id,
        "version": 3,
        "schema_version": 1,
        "edition": None,
        "round": None,
        "track": None,
        "verification_status": "unknown",
        "verified_at": None,
        "sources": [],
        "constraints": [],
        "is_fixture": False,
        "extensions": {"note": "官方规则离线未核验；不硬编码语言/字号/页数/页边距，待联网核验主办方公开页面"},
    }


def build_figure_style(style_id, competition_id, lang):
    return {
        "style_id": style_id,
        "competition_id": competition_id,
        "version": 3,
        "schema_version": 1,
        "fonts": {"family": "Microsoft YaHei" if lang == "zh" else "Times New Roman",
                  "size": None, "axis_label_size": None},
        "colors": ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e"],
        "line_width": 1.5,
        "caption_style": ("图序号-标题，置于图下方，注明单位与来源" if lang == "zh"
                          else "Figure number + caption below, with units and source"),
        "numbering": "arabic",
        "output_format": ["png", "pdf", "svg"],
        "review_status": "pending_review",
        "is_fixture": False,
        "dimensions": {"width": 6.0, "height": 4.0, "unit": "inch"},
        "extensions": {"note": "自研通用样式；配色为色盲友好调色板，非赛事官方规定"},
    }


def build_table_style(style_id, competition_id, lang):
    return {
        "style_id": style_id,
        "competition_id": competition_id,
        "version": 3,
        "schema_version": 1,
        "font_family": "SimSun" if lang == "zh" else "Times New Roman",
        "font_size": 10.5,
        "borders": "three_line",
        "caption_style": ("表序号-标题，置于表上方，三线表" if lang == "zh"
                          else "Table number + caption above, three-line table"),
        "review_status": "pending_review",
        "is_fixture": False,
        "extensions": {"note": "自研通用三线表样式，非赛事官方规定"},
    }


def build_writing_policy(competition_id, lang, has_source):
    return {
        "policy_id": "{}-writing-policy".format(competition_id),
        "competition_id": competition_id,
        "language": lang,
        "provenance": {"kind": "rule", "note": "自研通用写作策略，非赛事官方规则"},
        "priority": ["same_competition", "same_problem_type", "cross_competition_general"],
        "matching": {
            "tag_types": ["problem_types", "method_tags", "data_tags", "domain_tags"],
            "method_combination": "OR",
            "note": "多标签匹配优先同赛事；无匹配时显式回退通用框架，不默默混入跨赛事",
        },
        "source_filter": {
            "min_quality": "readable",
            "license_required": ["internal_analysis"],
            "parent_required": "resolved",
            "note": "未授权或未解析父题来源剔除，返回 no_reference",
        },
        "missing_fallback": {
            "note": "样本不足时回退通用章节结构与自研写法；明确标注为通用草稿",
        },
        "length": {
            "unit": "zh_chars" if lang == "zh" else "en_words",
            "budget": {"range": [10000, 20000] if lang == "zh" else [4000, 9000],
                       "source": "self_authored_general", "verified": False},
            "note": "篇幅预算为自研通用经验，非官方规定，标注 pending",
        },
        "content_slot": {
            "note": "动态大纲根据新题小问展开，不固定三问；结果由当前任务求解填充",
        },
        "is_template_draft": True,
        "template_draft_note": "通用草稿，赛事规则待核验" if not has_source else "已复用赛事模板源码；官方规则仍待核验",
    }


def build_writing_profile(writing_profile_id, competition_id, writing_policy_ref):
    return {
        "writing_profile_id": writing_profile_id,
        "competition_id": competition_id,
        "type_id": "unknown",
        "version": 3,
        "schema_version": 1,
        "sample_count": 0,
        "sources": [],
        "statistics": {"section_order": [], "length": [], "figure_frequency": []},
        "missing_fallback": {
            "fallback_ref": writing_policy_ref,
            "note": "无授权样本，回退自研通用写作策略",
        },
        "review_status": "pending_review",
        "is_fixture": False,
    }


def build_family(family_id, competition_id, lang, ruleset_id, outline_id,
                 fig_style_id, table_style_id):
    return {
        "family_id": family_id,
        "competition_id": competition_id,
        "family_version": 3,
        "schema_version": 1,
        "publication_status": "staging",
        "ruleset_id": ruleset_id,
        "ruleset_version": 3,
        "ruleset_status": "unknown",
        "ruleset_ref": None,
        "language": lang,
        "layout_refs": [],
        "base_outline_ref": None,
        "writing_policy_ref": None,
        "figure_style_ref": None,
        "table_style_ref": None,
        "review_status": "pending_review",
        "_outline_id": outline_id,
        "_fig_style_id": fig_style_id,
        "_table_style_id": table_style_id,
    }


# --------------------------------------------------------------------------- #
# LaTeX 布局：复制源模板子树，或生成通用骨架
# --------------------------------------------------------------------------- #

def copy_tree(src_dir, dst_dir):
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for p in sorted(src_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src_dir)
        target = dst_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        fio.no_links(p)
        data = p.read_bytes()
        write_bytes(target, data)
        copied.append(str(rel))
    return copied


GENERIC_LATEX_ZH = r"""\documentclass[UTF8]{ctexart}
%% 通用 LaTeX 骨架（自研，非赛事官方；规则待核验）
\usepackage{amsmath,amssymb}
\usepackage{booktabs}
\usepackage{graphicx}
\usepackage{geometry}
\geometry{a4paper}

\title{论文标题（由当前任务填充）}
\author{}
\date{\today}

\begin{document}
\maketitle

\begin{abstract}
（此处填写当前任务的摘要，由求解结果填充。）
\end{abstract}

\section{问题重述}\label{sec:restatement}
（由当前任务求解结果填充，不预填历史结论。）

\section{模型假设}\label{sec:assumptions}
（由当前任务求解结果填充。）

\section{问题分析}\label{sec:analysis}
（按小问展开。）

\section{模型建立与求解}\label{sec:model}
（按小问展开；由当前任务结果填充。）

\section{模型检验}\label{sec:validation}
（由当前任务结果填充。）

\section{敏感性分析}\label{sec:sensitivity}
（由当前任务结果填充。）

\section{结论}\label{sec:conclusion}
（由当前任务结果填充。）

\begin{thebibliography}{9}
% 引用由当前任务生成，不预填历史文献
\end{thebibliography}

\end{document}
"""

GENERIC_LATEX_EN = r"""\documentclass[11pt]{article}
%% Generic LaTeX skeleton (self-authored, not official; rules pending verification)
\usepackage{amsmath,amssymb}
\usepackage{booktabs}
\usepackage{graphicx}
\usepackage[margin=1in]{geometry}

\title{Paper Title (filled by current task)}
\author{}
\date{\today}

\begin{document}
\maketitle

\begin{abstract}
(Filled with the current task's abstract, produced from solution results.)
\end{abstract}

\section{Problem Restatement}\label{sec:restatement}
(Filled by the current task's solution results.)

\section{Model Assumptions}\label{sec:assumptions}

\section{Problem Analysis}\label{sec:analysis}

\section{Model Formulation and Solution}\label{sec:model}

\section{Model Validation}\label{sec:validation}

\section{Sensitivity Analysis}\label{sec:sensitivity}

\section{Conclusion}\label{sec:conclusion}

\begin{thebibliography}{9}
% Citations generated by the current task
\end{thebibliography}

\end{document}
"""


def build_layout(competition_id, lang, source_template_dir, official_docx, layout_dir):
    """产出布局文件；返回 (layout_refs, files)。"""
    layout_dir = Path(layout_dir)
    latex_dir = layout_dir / "latex"
    latex_dir.mkdir(parents=True, exist_ok=True)

    if source_template_dir is not None:
        src = Path(source_template_dir) / ("{}-latex".format(lang))
        if src.is_dir():
            copy_tree(src, latex_dir)
            main_tex = latex_dir / "main.tex"
            if not main_tex.is_file():
                write_text(main_tex, GENERIC_LATEX_EN if lang == "en" else GENERIC_LATEX_ZH)
            latex_built = "copied"
        else:
            write_text(latex_dir / "main.tex", GENERIC_LATEX_EN if lang == "en" else GENERIC_LATEX_ZH)
            latex_built = "generated"
    else:
        write_text(latex_dir / "main.tex", GENERIC_LATEX_EN if lang == "en" else GENERIC_LATEX_ZH)
        latex_built = "generated"

    docx_dir = layout_dir / "docx"
    docx_dir.mkdir(parents=True, exist_ok=True)
    docx_target = docx_dir / "template.docx"
    if official_docx is not None and official_docx.is_file():
        write_bytes(docx_target, official_docx.read_bytes())
        docx_built = "copied_legacy_unverified"
    else:
        lang2 = lang
        secs = EN_SECTIONS if lang2 == "en" else ZH_SECTIONS
        build_docx(secs, lang2, competition_id, docx_target)
        docx_built = "generated"

    return latex_built, docx_built


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="阶段05 十九赛事模板体系")
    parser.add_argument("--data-root", type=Path, default=Path("D:/Erdos_data"))
    parser.add_argument("--out", type=Path, default=Path("normalized/stage05_codex_v3"))
    parser.add_argument("--registry", type=Path, default=CONTENT_DIR / "config" / "competition_registry.json")
    args = parser.parse_args()

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    competitions = registry["competitions"]
    official_templates_dir = CONTENT_DIR / "out" / "templates"
    out_root = Path(args.out)
    rt.disjoint_sources(args.data_root,out_root)

    matrix = []
    bundles = []  # 聚合 bundle 用

    for comp in competitions:
        cid = comp["competition_id"]
        display = comp["display_name"]
        lang = LANG[cid]
        family_dir = out_root / "competitions" / cid / "template_family"

        # 定位源模板
        source_template_dir = None
        for s in comp.get("sources", []):
            if s.get("source_root") == "templates":
                p = args.data_root / s["relative_path"]
                if p.is_dir():
                    source_template_dir = p
                break

        official_docx = official_templates_dir / "{}-docx-official-v1.docx".format(cid)
        if not official_docx.is_file():
            official_docx = None

        has_source = source_template_dir is not None

        # 实体 ID
        family_id = "{}-family".format(cid)
        ruleset_id = "{}-ruleset".format(cid)
        outline_id = "{}-base-outline".format(cid)
        fig_style_id = "{}-figure-style".format(cid)
        table_style_id = "{}-table-style".format(cid)
        writing_profile_id = "{}-writing-profile".format(cid)

        # 实体 JSON（先生成内容，再落盘 + 建资产）
        ruleset = build_ruleset(ruleset_id, cid)
        outline = build_outline(outline_id, cid, lang)
        fig_style = build_figure_style(fig_style_id, cid, lang)
        table_style = build_table_style(table_style_id, cid, lang)
        writing_policy = build_writing_policy(cid, lang, has_source)
        family = build_family(family_id, cid, lang, ruleset_id, outline_id, fig_style_id, table_style_id)

        # 布局文件
        latex_built, docx_built = build_layout(
            cid, lang, source_template_dir, official_docx, family_dir / "layout")

        # 预览样图（合成数据，fixture）
        preview_dir = family_dir / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        render_figure_preview(fig_style, lang, preview_dir / "figure_style_sample.png")
        render_table_preview(table_style, lang, preview_dir / "table_style_sample.png")

        # 落盘实体文件（先写内容，记录字节 hash）
        asset_hashes = {}
        asset_hashes["ruleset"] = write_json(family_dir / "ruleset.json", ruleset)
        asset_hashes["outline"] = write_json(family_dir / "base_outline.json", outline)
        asset_hashes["writing_policy"] = write_json(family_dir / "writing_policy.json", writing_policy)
        asset_hashes["figure_style"] = write_json(family_dir / "figure_style.json", fig_style)
        asset_hashes["table_style"] = write_json(family_dir / "table_style.json", table_style)

        main_tex = family_dir / "layout" / "latex" / "main.tex"
        latex_package = family_dir / 'layout' / 'latex.zip'
        write_bytes(latex_package,rt.deterministic_zip(main_tex.parent))
        main_tex_sha = sha256_of(latex_package)
        main_tex_size = latex_package.stat().st_size
        docx_file = family_dir / "layout" / "docx" / "template.docx"
        docx_sha = sha256_of(docx_file)
        docx_size = docx_file.stat().st_size

        # 资产
        assets = []
        def add_asset(role, owner_id, role_short, rel_path, sha, size, media, version=3, fmt=None):
            aid = stable_asset_id(owner_id, role_short, rel_path)
            a = {
                "asset_id": aid, "role": role, "competition_id": cid, "owner_id": owner_id,
                "version": version, "oss_key": rel_path, "sha256": sha, "size_bytes": size,
                "media_type": media, "license_status": "unknown", "license": empty_license(),
                "license_evidence": None, "schema_version": 1,
            }
            if fmt is not None:
                a["format"] = fmt
            assets.append(a)
            return aid

        ruleset_asset = add_asset("ruleset", ruleset_id, "ruleset", "ruleset.json",
                                  asset_hashes["ruleset"], (family_dir / "ruleset.json").stat().st_size,
                                  "application/json")
        outline_asset = add_asset("template_outline", outline_id, "outline", "base_outline.json",
                                  asset_hashes["outline"], (family_dir / "base_outline.json").stat().st_size,
                                  "application/json")
        wp_asset = add_asset("writing_policy", family_id, "writing-policy", "writing_policy.json",
                             asset_hashes["writing_policy"], (family_dir / "writing_policy.json").stat().st_size,
                             "application/json")
        fig_asset = add_asset("figure_style", fig_style_id, "figure-style", "figure_style.json",
                              asset_hashes["figure_style"], (family_dir / "figure_style.json").stat().st_size,
                              "application/json")
        table_asset = add_asset("table_style", table_style_id, "table-style", "table_style.json",
                                asset_hashes["table_style"], (family_dir / "table_style.json").stat().st_size,
                                "application/json")
        latex_asset = add_asset("template_layout", family_id, "layout-latex", "layout/latex.zip",
                                main_tex_sha, main_tex_size, "application/zip", fmt="latex")
        docx_asset = add_asset("template_layout", family_id, "layout-docx", "layout/docx/template.docx",
                               docx_sha, docx_size,
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document", fmt="docx")

        def ref(aid, sha):
            return {"asset_id": aid, "version": 3, "sha256": sha}

        # 组装 family 引用
        family["ruleset_ref"] = ref(ruleset_asset, asset_hashes["ruleset"])
        family["layout_refs"] = [
            {"format": "latex", "asset_ref": ref(latex_asset, main_tex_sha)},
            {"format": "docx", "asset_ref": ref(docx_asset, docx_sha)},
        ]
        family["base_outline_ref"] = ref(outline_asset, asset_hashes["outline"])
        family["writing_policy_ref"] = ref(wp_asset, asset_hashes["writing_policy"])
        family["figure_style_ref"] = ref(fig_asset, asset_hashes["figure_style"])
        family["table_style_ref"] = ref(table_asset, asset_hashes["table_style"])

        # writing_profile（零样本回退）
        writing_profile = build_writing_profile(writing_profile_id, cid, ref(wp_asset, asset_hashes["writing_policy"]))
        write_json(family_dir / "writing_profile.json", writing_profile)

        # family 实体（去掉内部字段）
        family.pop("_outline_id", None)
        family.pop("_fig_style_id", None)
        family.pop("_table_style_id", None)
        write_json(family_dir / "family.json", family)

        # 每赛事 assets / bundle
        assets_sorted = sorted(assets, key=lambda a: a["asset_id"])
        write_json(family_dir / "assets.json", assets_sorted)

        bundle = {
            "families": [family],
            "rulesets": [ruleset],
            "outlines": [outline],
            "figure_styles": [fig_style],
            "table_styles": [table_style],
            "writing_profiles": [writing_profile],
            "assets": assets_sorted,
            "is_fixture": False,
        }
        write_json(family_dir / "bundle.json", bundle)

        bundles.append(bundle)

        matrix.append({
            "competition_id": cid,
            "display_name": display,
            "language": lang,
            "language_verified": False,
            "source_template_exists": has_source,
            "source_template_dir": str(source_template_dir) if source_template_dir else None,
            "family_id": family_id,
            "family_built": True,
            "ruleset_status": "unknown",
            "rules_verified": False,
            "layout_latex": latex_built,
            "layout_docx": docx_built,
            "latex_compiled": "not_compiled",
            "latex_compiler": "unavailable",
            "docx_rendered": False,
            "docx_render_status": "generated_or_copied; not visually rendered",
            "consumable_status": "draft",
            "is_generic_fallback": not has_source,
            "missing": [] if has_source else ["专用模板源码缺失，使用自研通用框架回退"],
        })

    # 聚合 bundle + 矩阵
    agg = {
        "families": [], "rulesets": [], "outlines": [], "figure_styles": [],
        "table_styles": [], "writing_profiles": [], "assets": [], "is_fixture": False,
    }
    for b in bundles:
        for k in ["families", "rulesets", "outlines", "figure_styles", "table_styles", "writing_profiles", "assets"]:
            agg[k].extend(b[k])
    for k in ["families", "rulesets", "outlines", "figure_styles", "table_styles", "writing_profiles", "assets"]:
        key_id = {"families": "family_id", "rulesets": "ruleset_id", "outlines": "outline_id",
                  "figure_styles": "style_id", "table_styles": "style_id",
                  "writing_profiles": "writing_profile_id", "assets": "asset_id"}[k]
        agg[k] = sorted(agg[k], key=lambda x: x[key_id])
    write_json(out_root / "bundle.json", agg)

    matrix_out = {
        "schema_version": 1,
        "scope": "nineteen_cumcm_mcm_etc template families v1 (draft)",
        "note": "19 入口全部建立草稿 family；规则核验=false（离线）；LaTeX 未编译（无编译器）；官方 docx 仅 cumcm/mcm 复用。",
        "competitions": matrix,
    }
    write_json(out_root / "nineteen_family_matrix.json", matrix_out)

    built = len(matrix)
    generic = sum(1 for m in matrix if m["is_generic_fallback"])
    print("阶段05完成：19 赛事 family 全部生成（{} 个，其中 {} 个通用回退）。".format(built, generic))
    print("聚合 bundle 输出：{}".format(out_root / "bundle.json"))
    rt.validate(agg)
    rt.seal(out_root,metadata={'scope':'nineteen draft families; rules unverified'})
    rt.check_snapshot(out_root)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
