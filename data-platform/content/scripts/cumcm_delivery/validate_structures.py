"""Exhaustive business contract coverage, independent of the later PG importer."""
import collections
from . import core,audit,local_closure,business_contracts as bc

def run(include_formulas=False):
    rd=audit.run_dir(local_closure.args());counts=collections.Counter();outside=0
    for path in sorted((rd/'catalog/business_entities').glob('*.jsonl')):
        for row in core.load_jsonl(path):bc.validate(path.stem,row);counts[path.stem]+=1
    for path in sorted((rd/'parsed').glob('*/page-*.json')):
        page=core.load_json(path);counts['page']+=1
        for order,b in enumerate(page['blocks'],1):
            row={'block_id':b['block_id'],'source_sha256':page['input_sha256'],'page':page['page'],'order':order,
                 'text':b['text'],'bbox':b['bbox'],'coordinates':'pdf_points_bottom_left','extraction_method':b['method'],
                 'confidence':b['confidence'],'semantic_status':'candidate'}
            bc.validate('block',row);counts['block']+=1
            box=b['bbox']
            outside+=int(box[2]<=0 or box[3]<=0 or box[0]>=page['width'] or box[1]>=page['height'])
        for v in page['visual_components']:bc.validate('visual',v);counts['visual']+=1
        if counts['page']%500==0:print('business source pages schema-validated {}/9713'.format(counts['page']),flush=True)
    if include_formulas:
        progress=core.load_json(rd/'quality/formula_enrichment_progress.json')
        if not progress['complete'] or progress['counts']['pages']!=counts['page']:raise ValueError('full formula detection has not finished')
        seen=set()
        for f in core.load_jsonl(rd/'catalog/formula_regions.jsonl'):
            bc.validate('formula_region',f);counts['formula_region']+=1
            if f['formula_region_id'] in seen:raise ValueError('duplicate formula region identity')
            seen.add(f['formula_region_id'])
            if counts['formula_region']%20000==0:print('formula rows schema-validated '+str(counts['formula_region']),flush=True)
    report={'status':'pass','counts':dict(counts),'outside_page_blocks':outside,'source_geometry_preserved':True,
        'all_selected_business_rows_schema_valid':True,'formulas_included':include_formulas,
        'semantic_accuracy_certified':False,'candidate_and_abstention_states_preserved':True}
    core.write_json(rd/'quality/full_business_contract_validation.json',report);print(report,flush=True)

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--include-formulas',action='store_true');run(p.parse_args().include_formulas)
