"""Inspect fonts actually used in rendered PDF text, beyond installed-font presence."""
import ctypes
from . import audit,core

def main():
    audit.deps()
    import profile_extraction
    pdfium=profile_extraction.load_pdfium()
    import pypdfium2.raw as raw
    run=core.CONTENT_DIR/'normalized/cumcm_delivery/cumcm-2010-2025-codex-r002'
    paths=[run/'templates/typst-preview.pdf',run/'templates/latex-preview/main.pdf']
    paths += [run/'quality/template_cases'/('%s-%d'%(kind,n))/('preview.pdf' if kind=='typst' else 'main.pdf') for kind in ('typst','latex') for n in (2,4)]
    reports=[]
    for path in paths:
        if not path.exists():
            if path.name=='typst-preview.pdf':path=run/'templates/preview.pdf'
            if not path.exists():raise ValueError('rendered PDF missing '+str(path))
        doc=pdfium.PdfDocument(path.read_bytes());pages=[];names=set()
        try:
            for pi,page in enumerate(doc,1):
                tp=page.get_textpage();fonts={}
                try:
                    for index in range(tp.count_chars()):
                        flags=ctypes.c_int()
                        size=raw.FPDFText_GetFontInfo(tp,index,None,0,ctypes.byref(flags))
                        if not size:continue
                        value=ctypes.create_string_buffer(size)
                        raw.FPDFText_GetFontInfo(tp,index,value,size,ctypes.byref(flags))
                        name=value.value.decode('utf-8','replace')
                        names.add(name);fonts[name]=fonts.get(name,0)+1
                    pages.append({'page':pi,'fonts_by_character_count':fonts})
                finally:tp.close();page.close()
        finally:doc.close()
        checks={'chinese_body_simsun':any('simsun' in n.lower() for n in names),'heading_simhei':any('simhei' in n.lower() for n in names),'latin_times_new_roman':any('timesnewroman' in n.replace(' ','').lower() for n in names)}
        reports.append({'pdf':str(path),'sha256':core.sha256_of(path),'font_names':sorted(names),'pages':pages,'checks':checks,'passed':all(checks.values())})
    result={'review_type':'agent_programmatic_pdf_font_inspection','pdfs':reports,'all_passed':all(r['passed'] for r in reports),
            'scope':'actual text fonts in scaffold and synthetic dynamic-section PDFs; product defaults, not official mandatory font certification; unused KaiTi/Courier remain declared client dependencies'}
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_rendered_fonts.json',result)
    if not result['all_passed']:raise ValueError('Actual rendered-font checks failed; inspect report')
    print('Actual SimSun/SimHei/Times New Roman usage checked in 6 rendered PDFs')

if __name__=='__main__':main()
