"""Preserve both cross-class detections without colliding opaque region identities."""
import collections
from . import core,audit,local_closure,business_contracts as bc

def run():
    rd=audit.run_dir(local_closure.args());path=rd/'catalog/formula_regions.jsonl';before=core.sha256_of(path)
    rows=core.load_jsonl(path);groups=collections.defaultdict(list)
    for f in rows:groups[f['formula_region_id']].append(f)
    aliases=[];pages=set()
    for identity,group in groups.items():
        if len(group)==1:continue
        if len(group)!=2 or {f['region_type'] for f in group}!={'embedding','isolated'}:
            raise ValueError('ambiguous same-type formula duplication requires inspection')
        for f in group:
            f['formula_region_id']=identity+'-'+f['region_type'];bc.validate('formula_region',f)
            aliases.append({'old_region_id':identity,'region_type':f['region_type'],'new_region_id':f['formula_region_id'],
                'source_sha256':f['source_sha256'],'page':f['page'],'crop_ref':f['crop_ref'],
                'reason':'same geometric rectangle detected under two distinct classes; both candidates retained'})
            pages.add((f['source_sha256'],f['page']))
    if len({f['formula_region_id'] for f in rows})!=len(rows):raise ValueError('formula IDs still collide')
    if aliases:
        mappings={(r['old_region_id'],r['region_type']):r['new_region_id'] for r in aliases}
        for sha,page in pages:
            p=rd/'parsed'/sha/('formula-page-%04d.json'%page);v=core.load_json(p)
            for f in v['formulas']:
                key=(f['formula_region_id'],f['region_type'])
                if key in mappings:f['formula_region_id']=mappings[key]
            core.write_json(p,v)
        core.write_jsonl(path,rows);core.write_jsonl(rd/'quality/formula_region_identity_aliases.jsonl',aliases)
        report={'before_catalog_sha256':before,'after_catalog_sha256':core.sha256_of(path),'ambiguous_cross_class_boxes':len(aliases)//2,
            'changed_ids':len(aliases),'all_region_ids_unique':True,'total_regions':len(rows),'source_geometry_and_bytes_unchanged':True,
            'changed_rows_schema_validated':True,'candidate_semantic_state_unchanged':True}
        core.write_json(rd/'quality/formula_identity_repair.json',report)
    else:report=core.load_json(rd/'quality/formula_identity_repair.json')
    qa=core.load_json(rd/'quality/full_business_contract_validation.json')
    if not qa['formulas_included'] or qa['counts']['formula_region']!=len(rows):raise ValueError('previous full schema QA absent or count differs')
    qa['formula_region_ids_unique']=True;qa['identity_repair']=report
    core.write_json(rd/'quality/full_business_contract_validation.json',qa)
    print(report,flush=True)

if __name__=='__main__':run()
