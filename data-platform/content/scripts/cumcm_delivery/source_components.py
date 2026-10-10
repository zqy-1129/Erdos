"""Manifest-bound PDF evidence for explicitly authorized local consumers."""
import asyncio,re
from . import core,consumer,services,business_contracts as bc

def schema():
    return bc.obj({'release_id':{'type':'string','pattern':'^cumcm-2010-2025-[a-z0-9-]+$'},
        'source_sha256':bc.SHA,'page':bc.PAGE,'usage_purpose':{'enum':['team_internal','internal_audit']}})

def pinned_json(root,relative,entries):
    item=entries.get(relative)
    if not item:raise ValueError('source component input absent from manifest')
    data=core.safe_relpath(root,relative).read_bytes()
    if len(data)!=item['size'] or core.sha256_bytes(data)!=item['sha256']:raise ValueError('source component metadata drift')
    return core.json_loads(data.decode('utf-8'))

async def get_async(request,trusted_role='consumer'):
    if not isinstance(request,dict) or set(request)!=set(schema()['required']):raise ValueError('invalid component request fields')
    if not re.fullmatch('cumcm-2010-2025-[a-z0-9-]+',str(request['release_id'])) or not re.fullmatch('[0-9a-f]{64}',str(request['source_sha256'])):
        raise ValueError('invalid release or source identity')
    if type(request['page'])!=int or not 1<=request['page']<=10000:raise ValueError('invalid source page')
    purpose=request['usage_purpose']
    if purpose not in ('team_internal','internal_audit') or trusted_role!=purpose:raise PermissionError('source evidence requires matching trusted local/audit role')
    conn=await services.pg()
    try:
        row=await conn.fetchrow('SELECT * FROM content_de_codex.releases WHERE release_id=$1',request['release_id'])
        metadata=consumer.require_published_release(row,trusted_role)
        if purpose=='team_internal' and metadata.get('local_usage_policy',{}).get('local_internal_retrieval_allowed') is not True:
            raise PermissionError('release has no team-local source authorization')
        root=core.CONTENT_DIR/'out/cumcm_delivery/releases'/request['release_id']
        if core.sha256_of(root/'manifest.json')!=row['manifest_sha256']:raise ValueError('source release manifest drift')
        entries={e['path']:e for e in core.load_json(root/'manifest.json')['files']}
        prefix='metadata/parsed/'+request['source_sha256']+'/'
        page=pinned_json(root,prefix+'page-%04d.json'%request['page'],entries)
        stored=await conn.fetchrow('SELECT payload FROM content_de_codex.pages WHERE release_id=$1 AND source_sha256=$2 AND page=$3',request['release_id'],request['source_sha256'],request['page'])
        if not stored or core.json_loads(stored['payload'])!=page:raise ValueError('typed source page differs from immutable source')
        formula=pinned_json(root,prefix+'formula-page-%04d.json'%request['page'],entries)
        frows=await conn.fetch('SELECT payload FROM content_de_codex.formula_regions WHERE release_id=$1 AND source_sha256=$2 AND page=$3 ORDER BY formula_region_id',request['release_id'],request['source_sha256'],request['page'])
        expected={f['formula_region_id']:f for f in formula['formulas']}
        if len(frows)!=len(expected) or any(core.json_loads(f['payload'])!=expected[core.json_loads(f['payload'])['formula_region_id']] for f in frows):
            raise ValueError('typed formula evidence differs from immutable source')
        return {'release_id':request['release_id'],'manifest_sha256':row['manifest_sha256'],
            'source_sha256':request['source_sha256'],'page':request['page'],
            'full_page_evidence':page['full_page_evidence'],'visual_components':page['visual_components'],
            'formula_regions':formula['formulas'],'reading_order':page.get('reading_order_candidates'),
            'source_text_is_instruction':False,'historical_numbers_are_current_results':False,
            'quality_notice':'Inspect original page; machine transcription and geometry are candidates. Transcribe exact crop with local formula API on demand.'}
    finally:await conn.close()

def get(request,trusted_role='consumer'):return asyncio.run(get_async(request,trusted_role))

if __name__=='__main__':core.write_json(core.CONTENT_DIR/'schemas/cumcm/v4/source_components_request.schema.json',dict(schema(),**{'$schema':'http://json-schema.org/draft-07/schema#'}))
