"""Independent HTTP consumer checks use actual PG and S3, no direct raw source reads."""
import asyncio,json,time,urllib.request,urllib.error
from . import core,services,consumer,api
RELEASE='cumcm-2010-2025-codex-v2'

def main():
    cfg=api.configure();base='http://127.0.0.1:'+str(cfg['port']);checks=[]
    def decoded(body):
        envelope=core.json_loads(body.decode())
        if not all(k in envelope for k in ('code','message','data','request_id','timestamp')):raise ValueError('business response envelope missing')
        if envelope['code'] != 0:
            raise ValueError('HTTP consumer request failed: '+str(envelope['code'])+' '+envelope['message'])
        return envelope['data']
    def request(path,role=None,value=None):
        headers={}
        if role:headers['Authorization']='Bearer '+cfg['audit_token' if role=='internal_audit' else 'consumer_token']
        data=None
        if value is not None:
            data=json.dumps(value,ensure_ascii=False).encode();headers['Content-Type']='application/json'
        req=urllib.request.Request(base+path,data=data,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=90) as response:return response.status,dict(response.headers),response.read()
        except urllib.error.HTTPError as exc:return exc.code,dict(exc.headers),exc.read()
    status,_,body=request('/health');checks.append({'name':'health','passed':status==200})
    status,_,body=request('/v1/content/competitions/cumcm/bootstrap','consumer')
    current=decoded(body)
    checks.append({'name':'competition_selection_resolves_active_release','passed':status==200 and current.get('release_id')==RELEASE and bool(current.get('manifest_sha256'))})
    profiles=current.get('rendering_profiles',[])
    checks.append({'name':'figure_style_reference_resolves_pinned_profile','passed':len(profiles)==1 and profiles[0]['profile_id']=='cumcm-figure-style' and profiles[0]['version']==1 and bool(profiles[0]['canonical_sha256'])})
    for suffix,name in [('?release_id=','explicit_empty_release_pin_rejected'),('?unsupported=','unsupported_empty_query_rejected')]:
        status,_,_=request('/v1/content/competitions/cumcm/bootstrap'+suffix,'consumer')
        checks.append({'name':name,'passed':status==400})
    status,_,_=request('/v1/content/competitions/cumcm/bootstrap?release_id=cumcm-2010-2025-qa-rollback','consumer')
    checks.append({'name':'retired_qa_fixture_remains_unavailable_to_consumer','passed':status==403})
    path='/v1/content/competitions/cumcm/bootstrap?release_id='+RELEASE
    status,_,_=request(path);checks.append({'name':'anonymous_denied','passed':status==401})
    start=time.monotonic()
    status,_,body=request(path,'consumer');bootstrap=decoded(body)
    bootstrap_resource={'response_bytes':len(body),'elapsed_seconds':time.monotonic()-start,'historical_corpus_bytes_downloaded':0,'format_bundle_count':len(bootstrap.get('format_templates',[]))}
    checks.append({'name':'bootstrap_two_format_bundles','passed':status==200 and len(bootstrap.get('format_templates',[]))==2})
    used=[]
    for asset in bootstrap['format_templates']:
        path='/v1/content/assets/'+asset['asset_id']+'?release_id='+RELEASE+'&version='+str(asset['version'])
        status,headers,data=request(path,'consumer')
        sha=core.sha256_bytes(data)
        checks.append({'name':'download_'+asset['format'],'passed':status==200 and sha==asset['sha256'] and headers.get('X-Content-SHA256')==sha,'bytes':len(data)})
        used.append({'asset_id':asset['asset_id'],'version':asset['version'],'sha256':sha,'bytes':len(data)})
        status,_,_=request(path.replace('&version=1','&version=2'),'consumer')
        checks.append({'name':'wrong_asset_version_'+asset['format'],'passed':status==400})
    value=core.load_json(core.CONTENT_DIR/'handoff/cumcm/examples/codex_retrieve.json')
    fixture='cumcm-2010-2025-qa-inc-v1'
    path='/v1/content/competitions/cumcm/bootstrap?release_id='+fixture
    status,_,_=request(path,'consumer');checks.append({'name':'staging_fixture_bootstrap_denied','passed':status==403})
    status,_,body=request(path,'internal_audit');checks.append({'name':'trusted_audit_fixture_bootstrap_allowed','passed':status==200 and decoded(body)['release_id']==fixture})
    fixture_request=core.json_loads(json.dumps(value));fixture_request['release_id']=fixture
    status,_,_=request('/v1/content/retrieve','consumer',fixture_request)
    checks.append({'name':'staging_fixture_retrieval_denied','passed':status==403})
    asset=bootstrap['format_templates'][0]
    path='/v1/content/assets/'+asset['asset_id']+'?release_id='+fixture+'&version='+str(asset['version'])
    status,_,_=request(path,'consumer');checks.append({'name':'staging_fixture_asset_denied','passed':status==403})
    plotting=core.json_loads(json.dumps(value))
    plotting['subproblem']={'goal':'机理几何示意图：定义对象坐标、对象定义和几何关系，展示当前计算模型。','problem_types':['mechanism']}
    plotting['token_budget']=8000
    status,_,body=request('/v1/content/retrieve','consumer',plotting)
    plot_capsule=decoded(body)
    checks.append({'name':'figure_capsule_contains_resolved_profile_within_budget','passed':status==200 and bool(plot_capsule['figure_recipes']) and plot_capsule['budget']['actual']<=8000 and all(r.get('rendering_profile',{}).get('canonical_sha256')==profiles[0]['canonical_sha256'] for r in plot_capsule['figure_recipes'])})
    status,_,body=request('/v1/content/retrieve','consumer',value);capsule=decoded(body)
    checks.append({'name':'normal_purpose_gate_and_budget','passed':status==200 and capsule['historical_references']==[] and capsule['budget']['actual']<=value['token_budget'],'status':capsule.get('status')})
    value['usage_purpose']='internal_audit'
    status,_,_=request('/v1/content/retrieve','consumer',value);checks.append({'name':'consumer_cannot_self_assign_audit','passed':status==403})
    value['stage']='analysis';value['subproblem']['problem_types']=[]
    status,_,body=request('/v1/content/retrieve','internal_audit',value);capsule=decoded(body)
    checks.append({'name':'trusted_audit_real_historical_results','passed':status==200 and bool(capsule.get('historical_references')) and capsule['budget']['actual']<=value['token_budget'] and all(r.get('compliance_note') for r in capsule['historical_references'])})
    if capsule.get('historical_references'):
        ref=capsule['historical_references'][0]['source_ref']
        path='/v1/content/assets/'+ref['asset_id']+'?release_id='+RELEASE+'&version='+str(ref['version'])
        status,_,_=request(path,'consumer');checks.append({'name':'original_historical_download_denied_to_consumer','passed':status==403})
        status,_,data=request(path,'internal_audit');checks.append({'name':'audit_original_download_hash','passed':status==200 and core.sha256_bytes(data)==ref['sha256']})
    asset=bootstrap['format_templates'][0]
    cache='qa/'+asset['asset_id']+'-v1.zip'
    asyncio.run(consumer.fetch_async(RELEASE,asset['asset_id'],1,cache))
    cached=core.safe_relpath(core.CONTENT_DIR/'cache/cumcm',cache)
    original=cached.read_bytes()
    core.write_bytes(cached,b'corrupted cache fixture')
    try:
        try:asyncio.run(consumer.fetch_async(RELEASE,asset['asset_id'],1,cache));rejected=False
        except ValueError:rejected=True
        checks.append({'name':'corrupt_cache_never_reused','passed':rejected})
    finally:core.write_bytes(cached,original)
    try:
        asyncio.run(consumer.fetch_async(RELEASE,asset['asset_id'],1,'../../escape'));rejected=False
    except ValueError:rejected=True
    checks.append({'name':'download_cache_traversal_denied','passed':rejected})
    report={'release_id':RELEASE,'checks':checks,'used_resources':used,'bootstrap_resources':bootstrap_resource,'data_access':'HTTP API + PG/S3; no D:/Erdos_data reads','cloud_deployed':False,'credentials_in_report':False}
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_consumer.json',report)
    if not all(c['passed'] for c in checks):raise ValueError('consumer integration failed; see report')
    print('{} actual consumer checks passed'.format(len(checks)))

if __name__=='__main__':main()
