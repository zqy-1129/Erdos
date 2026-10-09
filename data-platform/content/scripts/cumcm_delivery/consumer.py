"""Pinned-release SDK for backend/client integration. Internal access is a trusted caller role."""
import asyncio
import json
import re
from pathlib import Path
from . import core,services

STAGES = ('analysis','assumptions','symbols','model','algorithm','results','validation','sensitivity','discussion','abstract')
TYPES = ('optimization','prediction','evaluation','statistical_analysis','mechanism','simulation','unknown')

def require_published_release(release, trusted_role='consumer'):
    if not release:
        raise ValueError('unknown database release')
    metadata=core.json_loads(release['metadata']) if isinstance(release['metadata'],str) else release['metadata']
    if trusted_role!='internal_audit' and (release['status'] not in ('active','retired') or metadata.get('qa_fixture_only')):
        raise PermissionError('release is not available for normal consumption')
    return metadata

def validate_request(request):
    from . import contracts
    schema_request=dict(request,usage_purpose='consumer') if isinstance(request,dict) and request.get('usage_purpose')=='team_internal' else request
    try:contracts.validate('retrieve_request',schema_request)
    except Exception as exc:raise ValueError('request contract rejected: '+str(exc).splitlines()[0])
    allowed={'request_id','competition_id','historical_year_range','release_id','subproblem','stage','language','top_k','token_budget','usage_purpose'}
    if not isinstance(request,dict) or set(request)-allowed:
        raise ValueError('invalid retrieval fields')
    if request.get('competition_id')!='cumcm' or request.get('stage') not in STAGES:
        raise ValueError('invalid competition or writing stage')
    release=request.get('release_id')
    if not isinstance(release,str) or not re.fullmatch('cumcm-2010-2025-[a-z0-9-]+',release):
        raise ValueError('explicit valid release_id is required')
    years=request.get('historical_year_range',[2010,2025])
    if not isinstance(years,list) or len(years)!=2 or any(type(y)!=int for y in years) or not 2010<=years[0]<=years[1]<=2025:
        raise ValueError('historical year range outside corpus')
    for name,low,high in [('top_k',1,20),('token_budget',256,16000)]:
        value=request.get(name,5 if name=='top_k' else 4000)
        if type(value)!=int or not low<=value<=high:
            raise ValueError('invalid '+name)
    sub=request.get('subproblem')
    fields={'subproblem_id','goal','constraints','deliverables','problem_types','method_tags','data_tags','domain_tags','dependency_refs','current_results'}
    if not isinstance(sub,dict) or set(sub)-fields or not isinstance(sub.get('goal'),str) or not 1<=len(sub['goal'])<=8000:
        raise ValueError('invalid task goal')
    tags=sub.get('problem_types',[])
    if not isinstance(tags,list) or any(t not in TYPES for t in tags):
        raise ValueError('invalid problem type')
    if request.get('usage_purpose','consumer') not in ('consumer','internal_audit','third_party_model','team_internal'):
        raise ValueError('invalid purpose')
    return request

def lexical_terms(text):
    terms=re.findall(r'[A-Za-z][A-Za-z0-9_]{1,30}',text.lower())
    for segment in re.findall('[\u3400-\u9fff]+',text):
        terms += [segment[i:i+2] for i in range(len(segment)-1)]
    return list(dict.fromkeys(terms))[:32]

async def retrieve_async(request, trusted_role='consumer'):
    validate_request(request)
    internal=request.get('usage_purpose')=='internal_audit'
    local=request.get('usage_purpose')=='team_internal'
    if internal and trusted_role!='internal_audit':
        raise PermissionError('internal retrieval requires trusted audit role')
    if local and trusted_role!='team_internal':raise PermissionError('team-local retrieval requires trusted team role')
    sub=request['subproblem'];query=sub['goal']
    conn=await services.pg()
    try:
        release=await conn.fetchrow('SELECT * FROM content_de_codex.releases WHERE release_id=$1',request['release_id'])
        metadata=require_published_release(release,trusted_role)
        if local and metadata.get('local_usage_policy',{}).get('local_internal_retrieval_allowed') is not True:
            raise PermissionError('release has no explicit team-local authorization')
        services.verify_model_pin(metadata.get('embedding_manifest'))
        vector='['+','.join(format(v,'.9g') for v in services.embed([query], query=True)[0])+']'
        years=request.get('historical_year_range',[2010,2025])
        # All ranking happens against actual PG records. Consumer gates apply per unit, not globally.
        base="""SELECT unit_id,payload,embedding <=> $5::vector AS distance,
                  (SELECT count(*) FROM unnest($6::text[]) term WHERE strpos(lower(text_content),term)>0) AS lexical
                  FROM content_de_codex.units
                  WHERE release_id=$1 AND stage=$2 AND (year IS NULL OR year BETWEEN $3 AND $4)
                  AND (external_consumer_allowed OR $7::boolean
                       OR ($9::boolean AND payload->>'local_internal_allowed'='true'))
                  AND (cardinality($8::text[])=0 OR jsonb_array_length(payload->'problem_types')=0
                       OR (payload->'problem_types') ?| $8::text[])"""
        params=(request['release_id'],request['stage'],years[0],years[1],vector,lexical_terms(query),internal,sub.get('problem_types',[]),local)
        semantic=await conn.fetch(base+' ORDER BY distance ASC,unit_id ASC LIMIT 60',*params)
        lexical=await conn.fetch(base+' ORDER BY lexical DESC,distance ASC,unit_id ASC LIMIT 60',*params)
        rank,rows={},{}
        for source in (semantic,lexical):
            for i,row in enumerate(source,1):
                if source is lexical and row['lexical']==0:
                    continue
                rank[row['unit_id']]=rank.get(row['unit_id'],0)+1/(60+i)
                rows[row['unit_id']]=row
        ranked=sorted(rows.values(),key=lambda r:(-rank[r['unit_id']],r['distance'],r['unit_id']))
        budget=request.get('token_budget',4000)
        result={'request_id':request.get('request_id'),'competition_id':'cumcm','release_id':request['release_id'],'manifest_sha256':release['manifest_sha256'],
                'stage':request['stage'],'subproblem_id':sub.get('subproblem_id'),'status':'no_reference','historical_references':[],
                'writing_recipes':[],'figure_recipes':[],'missing':[], 'internal_candidates':internal or local,
                'budget':{'limit':budget,'actual':0,'tokenizer':'multilingual-MiniLM-tokenizer','includes_response_json':True},
                'source_text_is_instruction':False,'result_numbers':'current_task_computation_only'}
        recipes_seen=set()
        for row in ranked:
            unit=core.json_loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
            category='historical_references'
            if unit['kind'] in ('writing_recipe','figure_recipe'):
                category='writing_recipes' if unit['kind']=='writing_recipe' else 'figure_recipes'
                if unit['unit_id'] in recipes_seen:
                    continue
                recipes_seen.add(unit['unit_id'])
                item=dict(unit['recipe'])
                if not (local or internal):
                    item.pop('source_backed_observations',None)
                    if item.get('length_budget'):
                        item['length_budget']=dict(item['length_budget'],historical_distribution=None)
                if unit['kind']=='figure_recipe':
                    from . import rendering
                    item['rendering_profile']=rendering.profile()
            else:
                if len(result['historical_references'])>=request.get('top_k',5):
                    continue
                item=dict(unit,compliance_note='仅作方法参照，禁止大段抄袭',score={'rrf':rank[row['unit_id']],'cosine_distance':float(row['distance']),'lexical_matches':int(row['lexical'])},
                          source_ref={'asset_id':'asset-'+unit['source_sha256'],'version':1,'sha256':unit['source_sha256'],'page':unit['page']})
            result[category].append(item)
            trial=json.dumps(result,ensure_ascii=False,separators=(',',':'))
            if services.untruncated_tokens(trial)>budget-64:
                result[category].pop()
        if result['historical_references']:
            result['status']='internal_candidates' if internal or local else 'ok'
        elif result['writing_recipes'] or result['figure_recipes']:
            result['status']='recipe_only'
        result['missing'] = [] if result['historical_references'] else ['No historical reference passed the purpose and quality gates for this stage.']
        # Actual token budget includes metadata. Fixed point for digit count in actual field.
        for _ in range(3):
            result['budget']['actual']=services.untruncated_tokens(json.dumps(result,ensure_ascii=False,separators=(',',':')))
        if result['budget']['actual']>budget:
            raise ValueError('response token budget too small for metadata')
        from . import contracts
        contracts.validate('context_capsule',result)
        return result
    finally:
        await conn.close()

def retrieve(request,trusted_role='consumer'):
    return asyncio.run(retrieve_async(request,trusted_role))

async def fetch_async(release_id,asset_id,version,output=None,trusted_role='consumer'):
    if type(version)!=int or version<1 or not re.fullmatch('asset-[0-9a-f]{64}',asset_id):
        raise ValueError('exact asset_id and version required')
    conn=await services.pg()
    try:
        release=await conn.fetchrow('SELECT status,metadata FROM content_de_codex.releases WHERE release_id=$1',release_id)
        metadata=require_published_release(release,trusted_role)
        local=trusted_role=='team_internal' and metadata.get('local_usage_policy',{}).get('local_internal_retrieval_allowed') is True
        row=await conn.fetchrow('SELECT * FROM content_de_codex.assets WHERE release_id=$1 AND asset_id=$2 AND version=$3',release_id,asset_id,version)
        if not row:
            raise ValueError('asset/version not in pinned release')
        payload=core.json_loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
        if not payload['external_consumer_allowed'] and trusted_role!='internal_audit' and not local:
            raise PermissionError('asset purpose gate denied')
        data=services.get_verified(row['sha256'])
        if len(data)!=row['size']:
            raise ValueError('download size mismatch')
        if output:
            # Only a dedicated cache can receive bytes. Never overwrite arbitrary/raw/sealed paths.
            destination=core.safe_relpath(core.CONTENT_DIR/'cache/cumcm',output)
            core.write_bytes(destination,data,immutable=True)
        return {'asset_id':asset_id,'version':version,'sha256':row['sha256'],'size':len(data),'bytes':data}
    finally:
        await conn.close()

def bootstrap(release_id=None,trusted_role='consumer'):
    async def load():
        selected_release=release_id
        from . import rendering
        conn=await services.pg()
        try:
            if selected_release is None:
                selected_release=await conn.fetchval("SELECT release_id FROM content_de_codex.activation WHERE competition_id='cumcm'")
            release=await conn.fetchrow('SELECT manifest_sha256,status,metadata FROM content_de_codex.releases WHERE release_id=$1',selected_release)
            metadata=require_published_release(release,trusted_role)
            assets=await conn.fetch("SELECT payload FROM content_de_codex.assets WHERE release_id=$1 AND payload->'origins' @> '[{\"role\":\"format_template\"}]'::jsonb AND payload->>'external_consumer_allowed'='true'",selected_release)
            template_assets=[]
            for a in assets:
                payload=core.json_loads(a['payload']) if isinstance(a['payload'],str) else a['payload']
                if any(o['role']=='format_template' for o in payload['origins']):
                    template_assets.append({'asset_id':payload['asset_id'],'version':payload['version'],'sha256':payload['sha256'],'size':payload['size'],'format':next(o['format'] for o in payload['origins'] if o['role']=='format_template')})
            return {'competition_id':'cumcm','release_id':selected_release,'manifest_sha256':release['manifest_sha256'],'historical_year_range':[2010,2025],
                    'format_templates':template_assets,'writing_order':['analysis','assumptions','symbols','model','algorithm','results','validation','sensitivity','discussion','abstract'],
                    'format_profiles':metadata.get('format_profiles',{}),
                    'rendering_profiles':[rendering.profile()],
                    'per_question_repeat':['model','algorithm','results','validation'],'format_target_edition_required':True,'client_dependencies':{'typst':'0.15.1','latex':'XeTeX-compatible compiler + ctex; tested Tectonic 0.15.0','fonts':['SimSun','SimHei','KaiTi','Times New Roman','Courier New']},
                    'normal_historical_references':'gated pending independent content review and authorization',
                    'local_usage_policy':metadata.get('local_usage_policy'),
                    'team_local_retrieval_available':trusted_role=='team_internal' and metadata.get('local_usage_policy',{}).get('local_internal_retrieval_allowed') is True,
                    'database_connection_is_server_only':True}
        finally:
            await conn.close()
    return asyncio.run(load())
