"""回归集题面提取（工作包1）：源文件（.pdf/.docx/.doc）→ 纯文本。

- .pdf  → pdftotext -enc UTF-8 -layout（PDFTOTEXT 环境变量或 PATH）
- .docx → python-docx 按文档流顺序遍历段落 + 表格
- .doc  → WPS COM（convert_doc_to_docx.ps1）转 .docx 缓存后再提取

输出：out/texts/{business_id}.txt（UTF-8，LF）。
用法：python scripts/extract_problem_text.py [--only business_id]
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

CONTENT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = CONTENT_DIR / "config" / "problems_regression_v0.yaml"
TEXTS_DIR = CONTENT_DIR / "out" / "texts"
OUT_DIR = CONTENT_DIR / "out"
DOCX_CACHE = OUT_DIR / "_docx_cache"
TMP_DIR = OUT_DIR / "tmp"

PDFTOTEXT_FALLBACKS = (
    Path("C:/Users/ltf02/.qoder/bin/git/mingw64/bin/pdftotext.exe"),
)


def reconfigure_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def find_pdftotext():
    env = os.environ.get("PDFTOTEXT")
    if env and Path(env).exists():
        return Path(env)
    which = shutil.which("pdftotext")
    if which:
        return Path(which)
    for candidate in PDFTOTEXT_FALLBACKS:
        if candidate.exists():
            return candidate
    raise SystemExit("pdftotext 未找到：请设置 PDFTOTEXT 环境变量或加入 PATH")


def decode_pdf_text(raw):
    """pdftotext 可能输出 CESU-8 代理对（数学符号字体映射），需组合回标准码点。"""
    text = raw.decode("utf-8", errors="surrogatepass")
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def extract_pdf(path):
    target = TMP_DIR / (path.stem + ".txt")
    subprocess.run(
        [str(find_pdftotext()), "-enc", "UTF-8", "-layout", str(path), str(target)],
        check=True,
    )
    return decode_pdf_text(target.read_bytes())


def iter_blocks(parent):
    from docx.document import Document as DocxDocument
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    if isinstance(parent, DocxDocument):
        parent_elm = parent.element.body
    else:
        parent_elm = parent._element
    for child in parent_elm.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, parent)
        elif child.tag == qn("w:tbl"):
            yield Table(child, parent)


def extract_docx(path):
    import docx
    from docx.table import Table

    document = docx.Document(str(path))
    lines = []
    for block in iter_blocks(document):
        if isinstance(block, Table):
            for row in block.rows:
                cells = [" ".join(cell.text.split()) for cell in row.cells]
                lines.append(" | ".join(cells))
            lines.append("")
        else:
            lines.append(block.text)
    return "\n".join(lines)


def extract_doc(path, business_id):
    cache = DOCX_CACHE / (business_id + ".docx")
    if not cache.exists():
        ps1 = Path(__file__).with_name("convert_doc_to_docx.ps1")
        subprocess.run(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(ps1),
                "-InPath", str(path.resolve()),
                "-OutPath", str(cache.resolve()),
            ],
            check=True,
        )
    if not cache.exists():
        raise RuntimeError("WPS 转换未产出文件：" + str(cache))
    return extract_docx(cache)


def normalize(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    out = []
    blank = 0
    for line in text.split("\n"):
        line = line.rstrip()
        if line.strip() == "":
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(line)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def main():
    reconfigure_stdout()
    parser = argparse.ArgumentParser(description="回归集题面提取")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--only", default=None, help="仅处理指定 business_id")
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    root = Path(os.environ.get("ERDOS_DATA_ROOT", config.get("dataset_root", "D:/Erdos_data")))

    TEXTS_DIR.mkdir(parents=True, exist_ok=True)
    DOCX_CACHE.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    failures = 0
    done = 0
    for item in config["problems"]:
        business_id = item["business_id"]
        if args.only and business_id != args.only:
            continue
        source = root / item["source"]
        if not source.exists():
            print("[FAIL] {}: 源文件不存在 {}".format(business_id, source))
            failures += 1
            continue
        suffix = source.suffix.lower()
        try:
            if suffix == ".pdf":
                text = extract_pdf(source)
            elif suffix == ".docx":
                text = extract_docx(source)
            elif suffix == ".doc":
                text = extract_doc(source, business_id)
            else:
                raise RuntimeError("不支持的源文件类型：" + suffix)
        except Exception as exc:  # noqa: BLE001 - 逐题报告失败
            print("[FAIL] {}: {}".format(business_id, exc))
            failures += 1
            continue

        text = normalize(text)
        target = TEXTS_DIR / (business_id + ".txt")
        target.write_bytes(text.encode("utf-8"))
        first_line = next((ln for ln in text.split("\n") if ln.strip()), "")
        flag = " [WARN>8192]" if len(text) > 8192 else ""
        print("[OK] {}: {} 字符{} | {}".format(business_id, len(text), flag, first_line[:48]))
        done += 1

    print("---")
    print("完成 {} 题，失败 {} 题；输出目录 {}".format(done, failures, TEXTS_DIR))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
