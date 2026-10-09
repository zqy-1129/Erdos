"""Whole-page visual evidence, resumable and source pinned; no invented chart data."""
import argparse, io, re
from concurrent.futures import ProcessPoolExecutor,wait,FIRST_COMPLETED
from . import core,audit,local_closure

VERSION='full-page-evidence-v1'
COMMON=set('田由甲申电男甸町甩畏界留略畔番画')

def repair_font_runs(text):
    """Recover a known shifted ASCII run only; keep ordinary Chinese unchanged."""
    changes=[]
    def convert(match):
        raw=match.group();decoded=''.join(chr(ord(c)-0x7500) for c in raw)
        if len(raw)>=2 and any(c not in COMMON for c in raw) and re.search('[A-Za-z]',decoded):
            changes.append({'original':raw,'candidate':decoded,'basis':'U+7500 ASCII font-map offset; visual verification required'})
            return decoded
        return raw
    return re.sub('[\u7520-\u757e]+',convert,text),changes

def source_document(sha,rd):
    return core.load_json(rd/'parsed'/sha/'document.json')

def render_document(sha):
    audit.deps();import profile_extraction as pe
    rd=audit.run_dir(local_closure.args());folder=rd/'parsed'/sha
    meta=source_document(sha,rd)
    source=audit.source_path(meta['source'],local_closure.args())
    if core.sha256_of(source)!=sha:raise ValueError('original PDF checksum drift')
    pdf=pe.load_pdfium().PdfDocument(source.read_bytes())
    count=0;corrected=0;components=[]
    try:
        for idx in range(len(pdf)):
            path=folder/('page-%04d.json'%(idx+1));value=core.load_json(path)
            if value.get('visual_evidence_version')==VERSION:
                components.extend(value['visual_components']);count+=1;continue
            page=pdf[idx]
            try:
                image=page.render(scale=2).to_pil().convert('RGB')
                buf=io.BytesIO();image.save(buf,format='PNG')
                data=buf.getvalue();evidence=folder/'pages'/('p%04d-%s.png'%(idx+1,core.sha256_bytes(data)[:12]))
                core.write_bytes(evidence,data,immutable=True)
                ref={'asset_id':'asset-'+core.sha256_bytes(data),'version':1,'sha256':core.sha256_bytes(data)}
                value['full_page_evidence']={'path':evidence.relative_to(core.CONTENT_DIR).as_posix(),
                    'asset_ref':ref,'scope':'complete_page','pixel_size':[image.width,image.height],
                    'source_sha256':sha,'page':idx+1,'bbox':[0,0,value['width'],value['height']],
                    'coordinate_system':'pdf_points_bottom_left','render_scale':2}
                value['visual_components']=[]
                value['reading_order_candidates']=[b['block_id'] for b in sorted(value['blocks'],key=lambda b:(-b['bbox'][3],b['bbox'][0]))]
                value['reading_order_status']='geometric_candidate; multi-column and equation ordering requires visual review'
                original=value['text'];fixed,changes=repair_font_runs(original)
                if changes:
                    value['native_text_before_font_repair']=original;value['text']=fixed
                    value['font_repair_candidates']=changes;corrected+=1
                for block in value['blocks']:
                    corrected_text,items=repair_font_runs(block['text'])
                    if items:block['original_text']=block['text'];block['text']=corrected_text;block['font_repair_candidates']=items
                    if block['kind'] not in ('figure_caption','table_caption','formula_candidate'):continue
                    component={'component_id':'visual-'+core.sha256_bytes(block['block_id']),
                        'kind':{'figure_caption':'figure','table_caption':'table','formula_candidate':'formula'}[block['kind']],
                        'source_sha256':sha,'page':idx+1,'anchor_block_id':block['block_id'],
                        'anchor_text':block['text'],'anchor_bbox':block['bbox'],'coordinate_system':'pdf_points_bottom_left',
                        'asset_ref':ref,'evidence_scope':'complete_page','includes_neighbor_content':True,
                        'isolation_status':'not_isolated; full page preserves all visible elements',
                        'semantic_status':'pending_source_review','latex':None,'data_reconstructed':False}
                    value['visual_components'].append(component);components.append(component)
                value['visual_evidence_version']=VERSION
                core.write_json(path,value);count+=1
            finally:page.close()
    finally:pdf.close()
    core.write_json(folder/'visual_report.json',{'source_sha256':sha,'pages':count,'font_repair_pages':corrected,'version':VERSION})
    return {'sha256':sha,'pages':count,'font_repair_pages':corrected,'components':len(components)}

def run():
    rd=audit.run_dir(local_closure.args());shas=[p.parent.name for p in sorted((rd/'parsed').glob('*/document.json'))]
    reports=[]
    with ProcessPoolExecutor(max_workers=2) as pool:
        pending={pool.submit(render_document,sha):sha for sha in shas}
        while pending:
            done,_=wait(pending,timeout=15,return_when=FIRST_COMPLETED)
            for future in done:
                reports.append(future.result());pending.pop(future)
                print('visual PDF {}/{} pages={}'.format(len(reports),len(shas),sum(r['pages'] for r in reports)),flush=True)
            core.write_json(rd/'quality/visual_enrichment_progress.json',{'complete':not pending,'documents':reports,'version':VERSION})
    components=[]
    for p in sorted((rd/'parsed').glob('*/page-*.json')):components.extend(core.load_json(p)['visual_components'])
    core.write_jsonl(rd/'catalog/visual_components.jsonl',components)
    local_closure.state('complete_visual_evidence','pass',['quality/visual_enrichment_progress.json','catalog/visual_components.jsonl'],
        {'documents':len(reports),'pages':sum(r['pages'] for r in reports),'components':len(components),'semantic_review_is_separate':True})

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--sha');ns=parser.parse_args()
    if ns.sha:print(render_document(ns.sha))
    else:run()
