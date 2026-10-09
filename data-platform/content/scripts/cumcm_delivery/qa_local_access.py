"""Real HTTP/PG/S3 and standalone client checks for the new local release."""
import asyncio,copy,json,subprocess,sys,urllib.error,urllib.request,zipfile
from . import core,audit,local_closure,services,api,consumer,source_components,structured_store
from .offline_sdk import OfflineRelease
from .online_sdk import ContentClient

SOURCE='00474738e7498bbb507905b2d88aabad4c55103aafdf88fc6635af043f5a3f88'

def main():
    cfg=api.configure();release=local_closure.config()['release_id'];root=audit.release_dir(local_closure.args());pin=core.sha256_of(root/'manifest.json');checks=[]
    def http(value,role='team_internal',path='/v1/content/retrieve'):
        key={'team_internal':'team_token','consumer':'consumer_token','internal_audit':'audit_token'}[role]
        req=urllib.request.Request('http://127.0.0.1:18789'+path,data=json.dumps(value,ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+cfg[key],'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=180) as response:return response.status,core.json_loads(response.read().decode())
        except urllib.error.HTTPError as exc:return exc.code,core.json_loads(exc.read().decode())
    client=ContentClient(cfg['team_token']);boot=client.bootstrap(release)
    checks.append({'name':'published_team_bootstrap','passed':boot['team_local_retrieval_available'] and boot['manifest_sha256']==pin})
    value={'stage':'model','subproblem':{'goal':'CT椭圆投影参数标定模型','problem_types':[]},'usage_purpose':'team_internal','top_k':5,'token_budget':6000}
    cap=client.retrieve(value);checks.append({'name':'real_team_historical_retrieval','passed':bool(cap['historical_references']) and cap['budget']['actual']<=6000})
    component=client.components(SOURCE,9);checks.append({'name':'source_components_and_formula_evidence','passed':len(component['formula_regions'])>=2 and component['full_page_evidence']['scope']=='complete_page'})
    formula=next(f for f in component['formula_regions'] if f.get('transcription_verified'))
    image=client.fetch(formula['crop_ref']['asset_id'],1,formula['crop_ref']['sha256'])
    checks.append({'name':'real_pinned_formula_crop_download','passed':core.sha256_bytes(image)==formula['crop_ref']['sha256']})
    from .local_ai_sdk import LocalAIClient
    from pathlib import Path
    ai_root=Path('D:/Erdos_LocalAI');text_cfg=core.load_json(ai_root/'api.json');formula_cfg=core.load_json(ai_root/'formula-api.json')
    ai=LocalAIClient(text_cfg['token'],formula_cfg['token'],core.load_json(ai_root/'model_lock.json')['model_revisions'])
    transcription=ai.transcribe_formula(image,formula['crop_ref']['sha256'])
    checks.append({'name':'real_downloaded_crop_on_demand_local_transcription','passed':bool(transcription['components'][0]['latex_candidate']) and not transcription['components'][0]['semantics_verified']})
    offline=OfflineRelease(root,pin);oc=offline.components(SOURCE,9)
    checks.append({'name':'offline_online_source_component_parity','passed':all(component[k]==oc[k] for k in ('full_page_evidence','visual_components','formula_regions','reading_order'))})
    checks.append({'name':'offline_online_asset_byte_parity','passed':offline.fetch(formula['crop_ref']['asset_id'],1,formula['crop_ref']['sha256'])==image})
    ocap=offline.retrieve(dict(value,competition_id='cumcm',release_id=release,token_budget=16000))
    checks.append({'name':'explicit_offline_lexical_fallback','passed':bool(ocap['historical_references']) and all(h['retrieval_mode']=='offline_lexical_fallback' for h in ocap['historical_references'])})
    request=dict(value,competition_id='cumcm',release_id=release)
    status,_=http(request,'consumer');checks.append({'name':'consumer_cannot_self_grant_team_purpose','passed':status==403})
    status,_=http(dict(request,release_id='cumcm-2010-2025-codex-v2'));checks.append({'name':'old_release_cannot_inherit_local_rights','passed':status==403})
    status,body=http(dict(request,usage_purpose='third_party_model'));checks.append({'name':'third_party_purpose_never_gets_raw_history','passed':status==200 and not body['data']['historical_references'] and all('source_backed_observations' not in r for r in body['data']['writing_recipes'])})
    comp={'release_id':release,'source_sha256':SOURCE,'page':9,'usage_purpose':'team_internal'}
    status,_=http(comp,'consumer','/v1/content/components');checks.append({'name':'ordinary_role_cannot_fetch_private_components','passed':status==403})
    status,_=http(dict(comp,page=True),path='/v1/content/components');checks.append({'name':'boolean_page_rejected','passed':status==400})
    status,_=http(dict(comp,source_sha256='../escape'),path='/v1/content/components');checks.append({'name':'component_path_traversal_rejected','passed':status==400})
    # Committed drift needs a recoverable exact-field restore for an independent HTTP connection.
    async def committed_drift():
        conn=await services.pg()
        try:
            original=await conn.fetchval('SELECT payload FROM content_de_codex.pages WHERE release_id=$1 AND source_sha256=$2 AND page=9',release,SOURCE)
            try:
                await conn.execute("UPDATE content_de_codex.pages SET payload=jsonb_set(payload,'{text}','\"deliberate drift probe\"'::jsonb) WHERE release_id=$1 AND source_sha256=$2 AND page=9",release,SOURCE)
                status,_=http(comp,path='/v1/content/components');checks.append({'name':'committed_page_payload_drift_rejected','passed':status==400})
            finally:await conn.execute('UPDATE content_de_codex.pages SET payload=$3::jsonb WHERE release_id=$1 AND source_sha256=$2 AND page=9',release,SOURCE,original)
            await structured_store.activation_guard(conn,release)
            checks.append({'name':'all_typed_source_fingerprints_restored','passed':True})
        finally:await conn.close()
    asyncio.run(committed_drift())
    # The copied SDK really runs with -I, no project package, raw data, PG, S3 or ML import.
    folder=core.CONTENT_DIR/'handoff/cumcm/client_sdk';folder.mkdir(parents=True,exist_ok=True)
    for name in ('offline_sdk.py','online_sdk.py','local_ai_sdk.py'):core.write_bytes(folder/name,(core.CONTENT_DIR/'scripts/cumcm_delivery'/name).read_bytes())
    script=folder/'offline_smoke.py';source="import importlib.util,json,sys\nfrom pathlib import Path\nspec=importlib.util.spec_from_file_location('offline',Path(__file__).parent/'offline_sdk.py')\nm=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\nr=m.OfflineRelease(sys.argv[1],sys.argv[2]);b=r.bootstrap();c=r.components('"+SOURCE+"',9)\nf=next(f for f in c['formula_regions'] if f.get('transcription_verified'));a=f['crop_ref'];r.fetch(a['asset_id'],a['version'],a['sha256'])\nprint(json.dumps({'release_id':b['release_id'],'pinned':b['manifest_sha256']==sys.argv[2],'stdlib_only':not any(x in sys.modules for x in ('torch','asyncpg','boto3','onnxruntime','cumcm_delivery'))}))\n"
    core.write_bytes(script,source.encode());p=subprocess.run([core.PY,'-I','-X','utf8',str(script),str(root),pin],cwd=str(folder),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
    result=core.json_loads(p.stdout.decode()) if p.returncode==0 else {}
    checks.append({'name':'independent_stdlib_sdk_process','passed':p.returncode==0 and result.get('pinned') and result.get('stdlib_only')})
    report={'release_id':release,'manifest_sha256':pin,'checks':checks,'all_passed':all(c['passed'] for c in checks),'no_secrets_in_report':True,'production_gateway_and_product_exe_integrated':False}
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_local_access_acceptance.json',report)
    if not report['all_passed']:raise ValueError('local access checks failed: '+str([c['name'] for c in checks if not c['passed']]))
    local_closure.state('real_local_and_offline_access','pass',['reports/trae/cumcm_local_access_acceptance.json']);print('Real local access and independent offline SDK checks passed: '+str(len(checks)))

if __name__=='__main__':main()
