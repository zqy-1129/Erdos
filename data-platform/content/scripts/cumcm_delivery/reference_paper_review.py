"""Actual final-PDF metrics and rendered pages; visual signoff is recorded separately."""
import ctypes,io,re
from . import core,audit,new_task_flow as flow

def prepare():
    audit.deps();import profile_extraction as pe;pdfium=pe.load_pdfium()
    import pypdfium2.raw as raw
    from PIL import Image,ImageDraw
    out=flow.root();pdf=out/'paper.pdf';doc=pdfium.PdfDocument(pdf.read_bytes())
    texts=[];fonts=set();bounds=[];sizes=[];images=[]
    try:
        for number,page in enumerate(doc,1):
            sizes.append(list(page.get_size()));tp=page.get_textpage()
            try:
                texts.append(re.sub(r'\n'+str(number)+r'\s*$','',tp.get_text_range()))
                for index in range(tp.count_chars()):
                    flags=ctypes.c_int();size=raw.FPDFText_GetFontInfo(tp,index,None,0,ctypes.byref(flags))
                    if size:
                        name=ctypes.create_string_buffer(size);raw.FPDFText_GetFontInfo(tp,index,name,size,ctypes.byref(flags));fonts.add(name.value.decode('utf-8','replace'))
                    try:
                        box=tp.get_charbox(index)
                        if 50<box[1]<page.get_size()[1]-40 and box[2]>box[0] and box[3]>box[1]+0.01 and tp.get_text_range(index,1).strip():bounds.append(box)
                    except Exception:pass
                bitmap=page.render(scale=1.6)
                try:
                    im=bitmap.to_pil().convert('RGB');images.append(im)
                    buffer=io.BytesIO();im.save(buffer,format='PNG');core.write_bytes(out/'qa_pages'/('page-%02d.png'%number),buffer.getvalue())
                finally:bitmap.close()
            finally:tp.close();page.close()
    finally:doc.close()
    full='\n'.join(texts);compact=re.sub(r'\s','',full);normalized={n.replace(' ','').lower() for n in fonts}
    cards=core.load_json(out/'result_cards.json');nodes=core.load_json(out/'document_nodes.json')
    checks={'all_a4':all(abs(w-595.28)<2 and abs(h-841.89)<2 for w,h in sizes),
        'no_horizontal_overflow':not bounds or min(b[0] for b in bounds)>=68 and max(b[2] for b in bounds)<=527,
        'no_title_or_abstract_placeholders':all(t not in full for t in ('[论文标题]','中文摘要内容','关键词1')),
        'abstract_numeric_summary_from_current_cards':re.sub(r'\s','',flow.current_numeric_summary(cards)) in re.sub(r'\s','',texts[0]),
        'all_paragraph_nodes_in_pdf':all(re.sub(r'\s','',n['content']['text']) in compact for n in nodes if n['node_type']=='paragraph'),
        'code_sha_visible':core.sha256_of(out/'calibration_solver.py') in compact,
        'current_input_sha_visible':core.sha256_of(out/'input.csv') in compact,
        'three_current_result_tables':all('表'+str(q) in compact for q in (1,2,3)),
        'three_current_figure_captions':all('图'+str(q) in compact for q in (1,2,3)),
        'body_under_30_pages':len(texts)<=30,'file_under_20mb':pdf.stat().st_size<20*1024**2}
    font_checks={f:any(f.replace(' ','').lower() in n for n in normalized) for f in ('SimSun','SimHei','Times New Roman','Courier New')}
    checks['actual_four_used_typefaces']=all(font_checks.values())
    for start in range(0,len(images),2):
        sheet=Image.new('RGB',(1400,1010),'#dddddd');draw=ImageDraw.Draw(sheet)
        for offset,im in enumerate(images[start:start+2]):
            small=im.copy();small.thumbnail((660,940));sheet.paste(small,(offset*700+20,40))
            draw.text((offset*700+20,15),'Current reference paper / page '+str(start+offset+1),fill='black')
        buffer=io.BytesIO();sheet.save(buffer,format='PNG');core.write_bytes(out/'qa_pages'/('contact-%02d.png'%(start//2+1)),buffer.getvalue())
    report={'pdf_sha256':core.sha256_of(pdf),'pages':len(texts),'bytes':pdf.stat().st_size,'checks':checks,
        'fonts':sorted(fonts),'font_checks':font_checks,'all_passed':all(checks.values()),
        'visual_signoff':'pending actual page inspection','scope':'self-authored bounded reference flow'}
    core.write_json(out/'paper_full_checks.json',report);core.write_bytes(out/'qa_pages/extracted.txt',full.encode('utf-8'))
    print(report,flush=True)
    return report

if __name__=='__main__':prepare()
