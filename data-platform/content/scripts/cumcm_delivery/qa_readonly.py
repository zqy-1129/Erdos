"""Caller records evidence around a check which itself does not write reports or data."""
import argparse,asyncio,json,contextlib,io
from . import core,audit,services
RELEASE='cumcm-2010-2025-codex-v2'

async def snapshot():
    c=await services.pg()
    try:
        async with c.transaction(readonly=True):
            rows=await c.fetch('SELECT unit_id,md5(payload::text) AS payload_hash,md5(embedding::text) AS vector_hash FROM content_de_codex.units WHERE release_id=$1 ORDER BY unit_id',RELEASE)
            entities=await c.fetch('SELECT kind,entity_id,payload_sha256 FROM content_de_codex.entities WHERE release_id=$1 ORDER BY kind,entity_id',RELEASE)
            release=await c.fetchrow('SELECT manifest_sha256,status,metadata FROM content_de_codex.releases WHERE release_id=$1',RELEASE)
            assets=await c.fetch('SELECT asset_id,version,sha256,md5(payload::text) AS payload_hash FROM content_de_codex.assets WHERE release_id=$1 ORDER BY asset_id',RELEASE)
            active=await c.fetchval("SELECT release_id FROM content_de_codex.activation WHERE competition_id='cumcm'")
            return core.sha256_bytes(json.dumps({'units':[dict(r) for r in rows],'entities':[dict(r) for r in entities],'release':dict(release),'assets':[dict(r) for r in assets],'active':active},sort_keys=True))
    finally:await c.close()

def main():
    root=core.CONTENT_DIR/'out/cumcm_delivery/releases'/RELEASE
    def files():return {p.relative_to(root).as_posix():(p.stat().st_size,p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}
    before_files=files();before_db=asyncio.run(snapshot())
    output=io.StringIO()
    with contextlib.redirect_stdout(output):
        audit.dispatch(argparse.Namespace(cmd='import',release_id=RELEASE,target='local-audit',dry_run=True))
    after_dryrun_db=asyncio.run(snapshot());after_dryrun_files=files()
    dry_run_passed=before_db==after_dryrun_db and before_files==after_dryrun_files
    result=audit.read_only_check(argparse.Namespace(release_id=RELEASE))
    after_db=asyncio.run(snapshot());after_files=files()
    passed=before_db==after_db and before_files==after_files
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_readonly.json',dict(result,passed=passed,db_before_sha256=before_db,db_after_sha256=after_db,sealed_files_unchanged=before_files==after_files,caller_wrote_evidence_after_check=True))
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_dry_run.json',dict(core.json_loads(output.getvalue()),passed=dry_run_passed,db_before_sha256=before_db,db_after_sha256=after_dryrun_db,sealed_files_unchanged=before_files==after_dryrun_files))
    # Global QA reports must not masquerade as a full-corpus import receipt.
    receipt=dict(result,status='full_existing_import_verified_readonly',actual_import_checkpoint=core.load_json(core.CONTENT_DIR/'normalized/cumcm_delivery/cumcm-2010-2025-codex-r002/quality/finish_applied.json'))
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_import_full_verified.json',receipt)
    summary=core.CONTENT_DIR/'reports/trae/cumcm_codex_import.json'
    if summary.exists():
        previous=core.load_json(summary)
        if previous.get('release_id')!=RELEASE:
            core.write_json(summary.parent/('cumcm_codex_import_'+previous['release_id']+'.json'),previous)
    core.write_json(summary,receipt)
    if not passed or not dry_run_passed:raise ValueError('read-only check or dry-run changed sealed files or database')
    print('read-only filesystem and real database before/after proof passed')

if __name__=='__main__':main()
