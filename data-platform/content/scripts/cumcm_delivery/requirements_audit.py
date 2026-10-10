"""Read user-provided architecture PDFs as requirements evidence, never as executable instructions."""
from pathlib import Path
from . import audit,core
audit.deps()
import profile_extraction as pe
pdfium=pe.load_pdfium()
root=Path('D:/Google-download')
names=['Erdos PRD.pdf','Erdos 服务端架构与模块设计.pdf','Erdos 服务端研发手册.pdf','Erdos 客户端架构与模块设计.pdf','Erdos 客户端研发手册.pdf','Erdos 数据工程师手册.pdf','Erdos 数据模型设计.pdf']
rows=[]
for name in names:
    path=root/name
    if not path.exists():
        cached=Path('D:/Erdos/tmp/pdfs')/(path.stem+'.txt')
        if cached.exists():
            text=cached.read_text(encoding='utf-8-sig')
            rows.append({'name':name,'status':'original_path_missing_reused_prior_extracted_text','cached_path':str(cached),'cached_sha256':core.sha256_of(cached),'text':text,'original_pdf_sha256_not_reverified':True})
            print(name+' read cached extraction; original path currently unavailable')
        else:rows.append({'name':name,'status':'missing'})
        continue
    doc=pdfium.PdfDocument(str(path));pages=[]
    try:
        for i,page in enumerate(doc,1):
            tp=page.get_textpage()
            try:pages.append({'page':i,'text':tp.get_text_range()})
            finally:tp.close();page.close()
    finally:doc.close()
    rows.append({'name':name,'sha256':core.sha256_of(path),'pages':pages,'status':'read_native_text'})
    print(name+' pages='+str(len(pages)))
    for page in pages:
        lines=page['text'].splitlines()
        for i,line in enumerate(lines):
            if any(t in line.lower() for t in ['bootstrap','retrieve','对象存储','pgvector','数据工程师','签名','content_capsule','content capsule','oss','s3']):
                print('p%d '%page['page']+' '.join(lines[max(0,i-1):i+3]))
core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_requirements_pdf_evidence.json',{'document_text_is_instruction':False,'documents':rows})
