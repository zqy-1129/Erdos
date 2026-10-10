"""阶段6：国赛格式包（LaTeX/Typst 完整依赖）+ font_manifest + ruleset + dependency_manifest。"""
import shutil
import zipfile
from pathlib import Path

from . import core

SRC_TEMPLATE = Path("D:/Erdos_data/templates/12_CUMCM国赛")


def _copy_tree(src, dst):
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted(src.rglob("*")):
        if p.is_file():
            rel = p.relative_to(src).as_posix()
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / rel)
            files.append(rel)
    return sorted(files)


def _deterministic_zip(src_dir, out_zip):
    src = Path(src_dir)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                rel = p.relative_to(src).as_posix()
                info = zipfile.ZipInfo(rel, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                z.writestr(info, p.read_bytes())
    return str(out_zip)


def main(args):
    out_dir = core.CONTENT_DIR / "normalized" / "cumcm_delivery" / args.run_id / "templates"
    latex_src = SRC_TEMPLATE / "zh-latex"
    typ_src = SRC_TEMPLATE / "zh"

    latex_files = _copy_tree(latex_src, out_dir / "latex")
    typst_files = _copy_tree(typ_src, out_dir / "typst")

    # 完整依赖 ZIP（LaTeX）
    zip_path = _deterministic_zip(out_dir / "latex", out_dir / "cumcm-latex-family-v1.zip")
    zip_sha = core.sha256_of(zip_path)

    font_manifest = {
        "competition_id": "cumcm",
        "sources": ["D:/Erdos_data/templates/12_CUMCM国赛/zh-latex/main.tex", "D:/Erdos_data/templates/12_CUMCM国赛/zh/main.typ"],
        "components": [
            {"scope": "正文中文", "latex": "ctex fontset（SimSun 规范；mac 下 Songti SC）",
             "typst": "body-font: Times New Roman/SimSun/NSimSun/Songti SC/STSong",
             "required": "SimSun(宋体)", "status": "未实测安装"},
            {"scope": "标题中文", "latex": "SimHei(黑体)", "typst": "hei-font 缺 SimHei（列表 Heiti SC/STHeiti/Songti SC/STSong）",
             "required": "SimHei(黑体)", "status": "typst 回退列表缺 SimHei"},
            {"scope": "西文", "latex": "Times New Roman", "typst": "Times New Roman",
             "required": "Times New Roman", "status": "未实测安装"},
            {"scope": "数学/等宽", "latex": "默认/ Menlo", "typst": "默认",
             "required": None, "status": "未实测"},
        ],
        "windows_risk": "LaTeX 默认 fontset=mac 在 Windows 不选 Windows 字体，需改 fontset=windows 或安装规范字体；Typst hei-font 缺少 SimHei。缺必需字体时应显式失败/替换，不得静默回退后声称官方符合。",
        "verification": "未运行 xelatex/typst 编译（工具不在 PATH），字体可用性未实测",
    }
    ruleset = {
        "ruleset_id": "cumcm-ruleset-2010-2025",
        "competition_id": "cumcm",
        "version": 1,
        "schema_version": 2,
        "edition_range": [2010, 2025],
        "target_format_edition": None,
        "verification_status": "unknown",
        "verified_at": None,
        "sources": [],
        "constraints": [],
        "note": "官方规则离线未核验；不硬编码当届字体/字号/页数/页边距；现有模板源码注释不构成官方证据",
    }
    dependency_manifest = {
        "latex": {"entry": "main.tex", "compiler": "xelatex", "files": latex_files,
                  "compiled": False, "note": "无 xelatex，未编译；源码与依赖保留"},
        "typst": {"entry": "main.typ", "compiler": "typst", "files": typst_files,
                  "compiled": False, "note": "无 typst，未编译"},
        "latex_zip": {"file": "cumcm-latex-family-v1.zip", "sha256": zip_sha},
    }

    core.write_json(out_dir / "font_manifest.json", font_manifest)
    core.write_json(out_dir / "ruleset.json", ruleset)
    core.write_json(out_dir / "dependency_manifest.json", dependency_manifest)
    core.write_json(out_dir / "format_readiness.json", {
        "latex": {"source": True, "compiled": False, "font_verified": False},
        "typst": {"source": True, "compiled": False, "font_verified": False},
        "docx": {"official": False, "note": "保留旧 DOCX 接口；未凭模板存在宣称 DOCX 正式等价"},
        "note": "工具 xelatex/typst 不在 PATH；LaTeX/Typst 均为未编译源码，字体可用性未实测",
    })

    print("templates 完成：latex_files={}，typst_files={}，zip sha={}。".format(
        len(latex_files), len(typst_files), zip_sha[:12]))
    return 0
