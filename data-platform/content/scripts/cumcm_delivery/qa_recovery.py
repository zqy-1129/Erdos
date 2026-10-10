"""Interrupt the actual importer on an isolated 25-recipe release, then retry it.

No historical source is changed. Database crash is a terminated child process,
not a mocked successful transaction. Upload failure is injected after actual
immutable S3 verification; retry performs the same verification again.
"""
import argparse,asyncio,os,subprocess,sys,time
from . import core,audit,services,qa_publish

QA='cumcm-2010-2025-qa-recovery'
MARKER=services.RUNTIME/'local/qa-db-interrupt-ready.json'

async def state():
    connection=await services.pg()
    try:
        return {'release_rows':await connection.fetchval('SELECT count(*) FROM content_de_codex.releases WHERE release_id=$1',QA),
                'unit_rows':await connection.fetchval('SELECT count(*) FROM content_de_codex.units WHERE release_id=$1',QA),
                'active':await connection.fetchval("SELECT release_id FROM content_de_codex.activation WHERE competition_id='cumcm'")}
    finally:await connection.close()

def child():
    original_pg=services.pg
    class InterruptedConnection:
        def __init__(self,connection):self.connection=connection
        def __getattr__(self,name):return getattr(self.connection,name)
        async def executemany(self,statement,values):
            result=await self.connection.executemany(statement,values)
            if statement.startswith('INSERT INTO content_de_codex.units'):
                core.write_json(MARKER,{'release_id':QA,'real_unit_insert_finished_inside_uncommitted_transaction':True})
                await asyncio.sleep(300)
            return result
    async def wrapped():return InterruptedConnection(await original_pg())
    services.pg=wrapped
    audit.import_release(argparse.Namespace(release_id=QA,activate=False,target='local-audit'))

def main():
    if len(sys.argv)>1 and sys.argv[1]=='child':child();return
    qa_publish.build_qa(QA)
    args=argparse.Namespace(release_id=QA,activate=False,target='local-audit')
    before=asyncio.run(state())
    if before['release_rows'] or before['active']!=qa_publish.FULL:
        raise ValueError('recovery test needs unused fixture release and full active release')
    original_upload=services.put_verified
    def interrupted_upload(path,sha):
        original_upload(path,sha)
        raise RuntimeError('QA injected interruption after real object verification')
    services.put_verified=interrupted_upload
    try:
        try:audit.import_release(args);rejected=False
        except RuntimeError as exc:rejected='QA injected interruption' in str(exc)
    finally:services.put_verified=original_upload
    after_upload=asyncio.run(state())
    checks=[{'name':'actual_upload_stage_interruption_no_database_rows_or_activation','passed':rejected and after_upload==before}]
    if MARKER.exists():core.output_path(MARKER).unlink()
    env=dict(os.environ,PYTHONPATH=str(core.CONTENT_DIR/'scripts'))
    log=services.RUNTIME/'local/qa-recovery-child.log'
    with open(str(log),'wb') as output:
        process=subprocess.Popen([core.PY,'-m','cumcm_delivery.qa_recovery','child'],cwd=str(core.CONTENT_DIR),env=env,stdout=output,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+180
            while not MARKER.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.25)
            if not MARKER.exists():raise ValueError('actual importer did not reach uncommitted real units; inspect private child log')
            process.terminate();process.wait(timeout=30)
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=30)
    # The server must notice the terminated connection before the check.
    for _ in range(40):
        after_crash=asyncio.run(state())
        if after_crash==before:break
        time.sleep(.25)
    checks.append({'name':'terminated_real_import_transaction_rolls_back','passed':after_crash==before,'fixture_unit_count':25})
    audit.import_release(args)
    result=audit.read_only_check(args)
    after_retry=asyncio.run(state())
    checks.append({'name':'retry_reuses_verified_objects_and_finishes_staging_import','passed':after_retry['release_rows']==1 and after_retry['unit_rows']==25 and after_retry['active']==qa_publish.FULL})
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_recovery.json',{'checks':checks,'fixture_release_id':QA,'real_importer_process_terminated':True,'historical_entities_in_fixture':0,'full_corpus_process_crash_tested':False,'retry_check':result,'final_active_release_id':after_retry['active']})
    if not all(c['passed'] for c in checks):raise ValueError('actual recovery QA failed')
    print('3 actual importer interruption/retry checks passed; full corpus activation preserved')

if __name__=='__main__':main()
