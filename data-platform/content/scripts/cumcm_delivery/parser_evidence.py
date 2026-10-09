"""Pin actual local parsing dependencies without claiming measured recognition accuracy."""
import importlib.metadata as metadata
from . import core,audit

def main():
    audit.deps()
    import profile_extraction
    pdfium=profile_extraction.load_pdfium()
    root=core.CONTENT_DIR/'normalized/cumcm_delivery/cumcm-2010-2025-codex-r002'
    models=audit.RUNTIME/'py38/rapidocr_onnxruntime/models'
    lock={'parser_version':audit.PARSER_VERSION,'python':core.PY,'observed_at':core.now_utc_iso(),
          'packages':{'rapidocr-onnxruntime':metadata.version('rapidocr-onnxruntime'),
                      'pypdfium2':metadata.version('pypdfium2')},
          'ocr_provider':'CUDAExecutionProvider; actual per-page provider recorded in page JSON',
          'onnxruntime':'1.19.2 CUDA12 + cuDNN9 from isolated workspace dependencies',
          'model_files':{p.name:core.sha256_of(p) for p in sorted(models.glob('*.onnx'))},
          'ocr_configuration_sha256':core.sha256_of(models.parent/'config.yaml'),
          'render_scale':2,'coordinate_system':'pdf_points_bottom_left','crop_margin_points':3,
          'native_text_readable_heuristic':'at least 20 non-whitespace characters and no replacement symbol',
          'fallback':'actual RapidOCR for pages failing native readability; sparse pages remain explicit needs_review',
          'formula_recognition':'text OCR + source crop only; semantic LaTeX recognition not certified',
          'visual_reconstruction':'caption/formula line crops only; full figure series or cross-page table reconstruction pending',
          'calibration_status':'runtime and source integrity checked; independently transcribed calibration metrics not measured'}
    core.write_json(root/'quality/parser_lock.json',lock)
    core.write_bytes(root/'quality/parser_selection.md',
        ('# 实际解析器与边界\n\nPDFium 读取原生文字；不可读页以 2 倍渲染调用 RapidOCR ONNX CUDA。'
         '每页保存实际 provider、识别置信度、bbox 与质量状态，原件 SHA 和三个 OCR 权重 SHA 可追溯。'
         '通用 OCR 不能证明数学公式语义正确；图题裁剪不能证明完整图表已重建。'
         '独立留出集复核前不发布 CER、公式准确率或标题 F1。\n').encode('utf-8'))
    print('Actual PDF/OCR model and configuration fingerprints recorded')

if __name__=='__main__':main()
