"""Truthful, source-pinned independent review queue; sampling is not quality approval."""
from . import core
RUN=core.CONTENT_DIR/'normalized/cumcm_delivery/cumcm-2010-2025-codex-r002'

def main(run=None):
    global RUN
    if run is not None: RUN=run
    papers=core.load_json(RUN/'catalog/papers.json');units=core.load_jsonl(RUN/'catalog/retrieval_units.jsonl')
    selected=[]
    for year in range(2010,2026):
        pool=sorted([p for p in papers if p['year']==year],key=lambda p:core.sha256_bytes('holdout-v1:'+p['source_sha256']))
        selected.extend(pool[:2])
    rows=[]
    for paper in selected:
        rows.append({'review_id':'structure:'+paper['case_id'],'kind':'full_paper_structure','source_sha256':paper['source_sha256'],'case_id':paper['case_id'],'year':paper['year'],'page_range':[1,paper['page_count']],'status':'pending','reviewer_type':None,'ground_truth':None,'requires':'review all pages against PDF, including heading hierarchy and disjoint length boundaries'})
    pools={'paragraph':[],'formula':[],'figure':[],'table':[]}
    for unit in units:
        if unit['case_id'] and len(unit['text'])>=80:
            pools['paragraph'].append({'source_sha256':unit['source_sha256'],'page':unit['page'],'unit_id':unit['unit_id'],'block_ids':unit['block_ids'],'recognized_text':unit['text']})
    for paper in papers:
        for name,field in [('formula','formula_candidates'),('figure','figure_captions'),('table','table_captions')]:
            pools[name].extend(dict(b,case_id=paper['case_id']) for b in paper[field])
    denominators={}
    for kind,target in [('paragraph',100),('formula',100),('figure',50),('table',30)]:
        pool=sorted(pools[kind],key=lambda v:core.sha256_bytes('holdout-v1:'+str(v.get('block_id',v.get('unit_id')))))
        chosen=pool[:target];denominators[kind]={'available_candidates':len(pool),'selected':len(chosen),'target':target}
        for value in chosen:
            locator=value.get('block_id',value.get('unit_id'))
            rows.append(dict(value,review_id=kind+':'+locator,kind=kind,status='pending',reviewer_type=None,ground_truth=None,requires='independent PDF ground truth; caption crop does not establish complete figure/table geometry or data; formula crop requires semantic transcription'))
    core.write_jsonl(RUN/'quality/quality_review_queue.jsonl',rows)
    core.write_json(RUN/'quality/quality_metrics.json',{'reviewer_type':'none yet; machine extraction is not ground truth','sampling_seed':'holdout-v1 independent SHA order; do not tune on this review set','full_papers_selected':len(selected),'year_coverage':sorted({p['year'] for p in selected}),'denominators':denominators,'items_reviewed':0,'native_text_cer':None,'ocr_text_cer':None,'formula_symbol_accuracy':None,'figure_caption_link_accuracy':None,'table_caption_link_accuracy':None,'heading_structure_f1':None,'status':'review queue prepared; quantitative quality acceptance pending'})
    print('source-pinned quality queue prepared: {} items; no unmeasured accuracy claimed'.format(len(rows)))

if __name__=='__main__':main()
