"""阶段3：文本层解析全量执行（pypdfium2 快路径；扫描页标记 needs_ocr，不伪装 OCR 成功）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from . import core
import profile_extraction  # noqa: E402


def page_quality(text):
    if not text or not text.strip():
        return "needs_ocr"
    stripped = "".join(text.split())
    if len(stripped) < 20:
        return "needs_ocr"
    return "readable"


def main(args):
    data_root = Path(args.data_root)
    run_dir = core.CONTENT_DIR / "normalized" / "cumcm_delivery" / args.run_id
    records = core.load_jsonl(run_dir / "sources" / "source_files.jsonl")
    pdfs = [r for r in records if r["in_scope"] and r["status"] == "ok"
            and r["extension"].lower() == ".pdf" and r["size"]]
    if args.max_documents > 0:
        pdfs = pdfs[: args.max_documents]

    parse_dir = run_dir / "parse_runs"
    parse_dir.mkdir(parents=True, exist_ok=True)

    coverage = []
    failures = []
    tool_version = None

    for r in pdfs:
        src_abs = core.no_links(data_root / r["relative_path"])
        try:
            pages, sha, ver = profile_extraction.extract_pdf_pages(str(src_abs))
            tool_version = ver
            page_rows = []
            readable = 0
            needs_ocr = 0
            for i, text in enumerate(pages, start=1):
                q = page_quality(text)
                if q == "readable":
                    readable += 1
                else:
                    needs_ocr += 1
                page_rows.append({"page": i, "quality": q,
                                  "text": text if q == "readable" else "",
                                  "text_len": len(text)})
            d = parse_dir / r["source_id"]
            d.mkdir(parents=True, exist_ok=True)
            core.write_jsonl(d / "pages.jsonl", page_rows)
            core.write_json(d / "diagnostics.json", {
                "source_id": r["source_id"], "total_pages": len(pages),
                "readable_pages": readable, "needs_ocr_pages": needs_ocr,
                "tool": "pypdfium2", "tool_version": ver,
                "input_sha256": r["sha256"],
            })
            coverage.append({**{k: r[k] for k in ("source_id", "relative_path", "year", "root_kind", "sha256")},
                             "total_pages": len(pages), "readable_pages": readable,
                             "needs_ocr_pages": needs_ocr, "status": "published_text_layer"})
        except Exception as exc:
            failures.append({"source_id": r["source_id"], "relative_path": r["relative_path"],
                             "reason": str(exc)})

    (run_dir / "quality").mkdir(parents=True, exist_ok=True)
    core.write_json(run_dir / "quality" / "page_coverage.json", {
        "tool": "pypdfium2", "tool_version": tool_version,
        "total_pdfs": len(pdfs), "processed": len(coverage), "failed": len(failures),
        "documents": coverage,
    })
    core.write_json(run_dir / "quality" / "processing_failures.json", {"count": len(failures), "failures": failures})

    total_pages = sum(c["total_pages"] for c in coverage)
    ocr_pages = sum(c["needs_ocr_pages"] for c in coverage)
    print("parse 完成：PDF={}，页={}，其中需OCR页={}，失败={}（工具 pypdfium2 {}）".format(
        len(coverage), total_pages, ocr_pages, len(failures), tool_version))
    return 0
