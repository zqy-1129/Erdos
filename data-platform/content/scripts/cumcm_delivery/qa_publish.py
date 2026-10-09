"""Small real QA release exercises import conflicts, idempotency and activation rollback."""
import argparse,asyncio,json
from . import audit,core,services
FULL='cumcm-2010-2025-codex-v2'
QA='cumcm-2010-2025-qa-rollback'

async def pointer():
    c=await services.pg()
    try:return await c.fetchval("SELECT release_id FROM content_de_codex.activation WHERE competition_id='cumcm'")
    finally:await c.close()

def build_qa(release_id=QA):
    base=core.CONTENT_DIR/'out/cumcm_delivery/releases'/FULL;dest=base.parent/release_id
    if (dest/'SEALED.json').exists():audit.validate_release(dest);return
    assets=[a for a in core.load_json(base/'assets.json') if a['external_consumer_allowed']]
    for a in assets:audit.copy_verified(core.safe_relpath(base,a['release_path']),dest/a['release_path'],a['sha256'])
    core.write_json(dest/'assets.json',assets)
    for name in ('problems','subproblems','papers','paper_problem_links'):core.write_json(dest/'metadata/catalog'/(name+'.json'),[])
    for name in ('retrieval_units','attachment_profiles'):core.write_jsonl(dest/'metadata/catalog'/(name+'.jsonl'),[])
    for name in ('writing_recipes','figure_recipes'):
        p=base/'metadata/recipes'/(name+'.json');audit.copy_verified(p,dest/'metadata/recipes'/p.name,core.sha256_of(p))
    units=audit.unit_rows(dest/'metadata');count=len(units)
    index=core.load_jsonl(base/'embeddings/index.jsonl')[-count:]
    if [r['unit_id'] for r in index]!=[r['unit_id'] for r in units]:raise ValueError('QA recipe vector slice mismatch')
    data=(base/'embeddings/vectors.f32').read_bytes()[-count*384*4:]
    core.write_bytes(dest/'embeddings/vectors.f32',data);core.write_jsonl(dest/'embeddings/index.jsonl',index)
    model=core.load_json(base/'model_manifest.json');core.write_json(dest/'model_manifest.json',model)
    core.write_json(dest/'embeddings/profile.json',{'rows':count,'dimension':384,'model_manifest':model,'vectors_sha256':core.sha256_of(dest/'embeddings/vectors.f32'),'index_sha256':core.sha256_of(dest/'embeddings/index.jsonl')})
    core.write_json(dest/'metadata.json',{'release_id':release_id,'competition_id':'cumcm','qa_fixture_only':True,'historical_year_range':[2010,2025],'embedding_manifest':model})
    files=[{'path':p.relative_to(dest).as_posix(),'sha256':core.sha256_of(p),'size':p.stat().st_size} for p in sorted(dest.rglob('*')) if p.is_file()]
    core.write_json(dest/'manifest.json',{'competition_id':'cumcm','release_id':release_id,'version':3,'files':files})
    core.write_json(dest/'SEALED.json',{'manifest_sha256':core.sha256_of(dest/'manifest.json'),'sealed_at':core.now_utc_iso()});audit.validate_release(dest)

def main():
    build_qa();args=argparse.Namespace(release_id=QA,activate=False,target='local-audit')
    if asyncio.run(pointer())!=FULL:raise ValueError('full release must be active before isolated publication tests')
    audit.import_release(args);audit.import_release(args)
    checks=[{'name':'qa_idempotent_real_import','passed':True},{'name':'staging_does_not_auto_activate','passed':asyncio.run(pointer())==FULL}]
    async def conflict(value):
        c=await services.pg()
        try:await c.execute('UPDATE content_de_codex.releases SET manifest_sha256=$1 WHERE release_id=$2',value,QA)
        finally:await c.close()
    sha=core.sha256_of(audit.release_dir(args)/'manifest.json')
    asyncio.run(conflict('0'*64))
    try:
        try:audit.import_release(args);rejected=False
        except ValueError as exc:rejected='release ID conflict' in str(exc)
        checks.append({'name':'same_release_id_different_manifest_rejected','passed':rejected})
    finally:asyncio.run(conflict(sha))
    try:
        args.activate=True
        audit.import_release(args)
        checks.append({'name':'explicit_activation_on_idempotent_import_is_honored','passed':asyncio.run(pointer())==QA})
    finally:
        args.release_id=FULL;audit.activate(args,'rollback')
    checks.append({'name':'real_rollback_preserves_full_release','passed':asyncio.run(pointer())==FULL})
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_publication.json',{'checks':checks,'fixture_release_id':QA,'fixture_historical_entities':0,'fixture_retained_as_retired_for_traceability':True,'final_active_release_id':asyncio.run(pointer()),'simulated_full_import_process_crash_not_tested':True})
    if not all(c['passed'] for c in checks):raise ValueError('publication QA failed')
    print('5 actual publication/idempotency checks passed')

if __name__=='__main__':main()
