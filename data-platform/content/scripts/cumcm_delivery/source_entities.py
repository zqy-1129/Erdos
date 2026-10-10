"""Explicit source identities and file relationships without guessing unknown years."""
import collections,mimetypes
from . import core,audit,local_closure,business_contracts as bc

def run():
    rd=audit.run_dir(local_closure.args());sources=audit.all_sources(local_closure.args())+core.load_jsonl(rd/'converted_sources.jsonl')
    rows=[];roles=collections.Counter()
    for s in sources:
        root=s['root_kind'];ext=s['extension']
        if root not in ('template','problem','paper'):raise ValueError('unknown source root role')
        role='template' if root=='template' else root if ext in ('.pdf','.doc','.docx') else 'attachment'
        year=s.get('year')
        if year is not None and not 2010<=year<=2025:raise ValueError('source escaped agreed historical years')
        row={'source_id':s['source_id'],'sha256':s['sha256'],'year':year,'role':role,
            'asset_ref':{'asset_id':'asset-'+s['sha256'],'version':1,'sha256':s['sha256']},
            'media_type':s.get('media_type') or mimetypes.guess_type(s['relative_path'])[0] or 'application/octet-stream',
            'original_occurrences':[s['relative_path']]}
        bc.validate('source',row);rows.append(row);roles[role]+=1
    if len({r['source_id'] for r in rows})!=len(rows):raise ValueError('duplicate source identity')
    core.write_jsonl(rd/'catalog/business_entities/source.jsonl',rows)
    core.write_json(rd/'quality/source_business_contracts.json',{'rows':len(rows),'roles':dict(roles),
        'all_rows_schema_valid':True,'unknown_years_preserved':sum(r['year'] is None for r in rows),'source_assets_are_content_addressed':True})
    print({'source_rows':len(rows),'roles':dict(roles)})

if __name__=='__main__':run()
