"""Private PG snapshot and real isolated restore; never drops the working database."""
import asyncio,json,subprocess
from . import core,audit,services,local_closure,structured_store

def command(*args):
    p=subprocess.run(['docker',*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=900)
    if p.returncode:raise RuntimeError('local backup/restore command failed: '+p.stderr.decode('utf-8','replace')[-1500:])
    return p.stdout

async def inspect_restored(name):
    audit.deps();import asyncpg
    cfg=dict(services.configuration()['postgres']);cfg['database']=name;cfg.pop('dbname')
    connection=await asyncpg.connect(**cfg);release=local_closure.config()['release_id']
    try:
        async with connection.transaction(readonly=True):
            await structured_store.activation_guard(connection,release)
            active=await connection.fetchval("SELECT release_id FROM content_de_codex.activation WHERE competition_id='cumcm'")
            if active!=release:raise ValueError('restored active release differs')
            original=await services.pg()
            try:
                tables=['releases','assets','units','entities','pages','blocks','sections','section_blocks','visual_components','formula_regions']
                counts={}
                for table in tables:
                    a=await original.fetchval('SELECT count(*) FROM content_de_codex.'+table)
                    b=await connection.fetchval('SELECT count(*) FROM content_de_codex.'+table)
                    if a!=b:raise ValueError('restored row count differs: '+table)
                    counts[table]=b
                for table in ('tasks','assets','results','nodes'):
                    a=await original.fetchval('SELECT count(*) FROM content_tasks_local.'+table)
                    b=await connection.fetchval('SELECT count(*) FROM content_tasks_local.'+table)
                    if a!=b:raise ValueError('restored task count differs: '+table)
                    counts['tasks.'+table]=b
                for table in ('problems','templates','cases'):
                    query='SELECT row_to_json(t)::text AS payload FROM content_legacy_local_v3.'+table+' t ORDER BY business_id'
                    a=[r['payload'] for r in await original.fetch(query)];b=[r['payload'] for r in await connection.fetch(query)]
                    if a!=b:raise ValueError('restored legacy exact fields differ: '+table)
                    counts['legacy.'+table]=len(b)
            finally:await original.close()
            return {'active_release':active,'all_typed_fingerprints_passed':True,'all_checked_table_counts_equal':True,'counts':counts}
    finally:await connection.close()

def main():
    cfg=services.configuration()['postgres'];user=cfg['user'];database=cfg['dbname'];container=services.configuration().get('postgres_container','cumcm-codex-postgres')
    if database!='cumcm_codex':raise ValueError('unexpected working database; no backup operation')
    suffix=core.now_utc_iso().replace('-','').replace(':','').split('.')[0].replace('T','_').replace('Z','').lower()
    restored='cumcm_restore_check_'+suffix
    folder=audit.RUNTIME/'local/backups';folder.mkdir(parents=True,exist_ok=True)
    path=folder/('cumcm-v3-'+suffix+'.dump');container_path='/tmp/'+path.name
    existing=command('exec',container,'psql','-U',user,'-d',database,'-Atc',"SELECT datname FROM pg_database WHERE datname='"+restored+"'")
    if existing.strip() or path.exists():raise ValueError('restore/backup identity already exists; never overwrite')
    command('exec',container,'pg_dump','-U',user,'-d',database,'-Fc','-f',container_path)
    command('cp',container+':'+container_path,str(path))
    print('Private local PG dump created; restoring into isolated '+restored,flush=True)
    command('exec',container,'createdb','-U',user,restored)
    command('exec',container,'pg_restore','-U',user,'--no-owner','--exit-on-error','-d',restored,container_path)
    report={'passed':True,'private_dump':str(path),'dump_sha256':core.sha256_of(path),'dump_bytes':path.stat().st_size,
        'restored_database':restored,'working_database_modified':False,'restored_database_kept_for_inspection':True,
        'verification':asyncio.run(inspect_restored(restored)),
        's3_recovery_scope':'Immutable release contains all object bytes for re-import; no claim of a fresh full S3 restore in this check.'}
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_local_backup_restore.json',report);print(report,flush=True)

if __name__=='__main__':main()
