"""Rollback-only negative probes against real typed PDF tables."""
import asyncio
from . import core,services,local_closure,structured_store

async def run():
    release=local_closure.config()['release_id'];conn=await services.pg();checks=[]
    try:
        await structured_store.activation_guard(conn,release)
        source_roundtrip=await structured_store.verify_components_against_source(conn,release)
        outer=conn.transaction();await outer.start()
        try:
            probes=[
                ('positive_page_width',"UPDATE content_de_codex.pages SET width=-1 WHERE release_id=$1 AND (source_sha256,page)=(SELECT source_sha256,page FROM content_de_codex.pages WHERE release_id=$1 LIMIT 1)"),
                ('block_page_foreign_key',"UPDATE content_de_codex.blocks SET page=99999 WHERE release_id=$1 AND block_id=(SELECT block_id FROM content_de_codex.blocks WHERE release_id=$1 LIMIT 1)"),
                ('visual_anchor_foreign_key',"UPDATE content_de_codex.visual_components SET anchor_block_id='qa-nonexistent-anchor' WHERE release_id=$1 AND component_id=(SELECT component_id FROM content_de_codex.visual_components WHERE release_id=$1 LIMIT 1)"),
                ('formula_asset_foreign_key',"UPDATE content_de_codex.formula_regions SET crop_asset_id='asset-'||repeat('0',64),crop_version=1 WHERE release_id=$1 AND formula_region_id=(SELECT formula_region_id FROM content_de_codex.formula_regions WHERE release_id=$1 LIMIT 1)"),
                ('formula_asset_version_pair',"UPDATE content_de_codex.formula_regions SET crop_asset_id='asset-'||repeat('0',64),crop_version=NULL WHERE release_id=$1 AND formula_region_id=(SELECT formula_region_id FROM content_de_codex.formula_regions WHERE release_id=$1 LIMIT 1)"),
                ('section_nonnegative_length',"UPDATE content_de_codex.sections SET recognized_chars=-1 WHERE release_id=$1 AND section_id=(SELECT section_id FROM content_de_codex.sections WHERE release_id=$1 LIMIT 1)"),
                ('section_parent_foreign_key',"UPDATE content_de_codex.sections SET parent_id='qa-nonexistent-parent' WHERE release_id=$1 AND section_id=(SELECT section_id FROM content_de_codex.sections WHERE release_id=$1 LIMIT 1)"),
                ('exclusive_section_membership',"INSERT INTO content_de_codex.section_blocks SELECT * FROM content_de_codex.section_blocks WHERE release_id=$1 LIMIT 1"),
            ]
            for name,sql in probes:
                savepoint=conn.transaction();await savepoint.start()
                try:
                    await conn.execute(sql,release)
                    await conn.execute('SET CONSTRAINTS ALL IMMEDIATE')
                    rejected=False;code=None
                except Exception as exc:rejected=True;code=getattr(exc,'sqlstate',None)
                finally:await savepoint.rollback()
                checks.append({'name':name,'passed':rejected,'sqlstate':code})
            for table,field in [('pages','width'),('formula_regions','latex_candidate')]:
                savepoint=conn.transaction();await savepoint.start()
                try:
                    if table=='pages':
                        await conn.execute('UPDATE content_de_codex.pages SET width=width+1 WHERE release_id=$1 AND (source_sha256,page)=(SELECT source_sha256,page FROM content_de_codex.pages WHERE release_id=$1 LIMIT 1)',release)
                    else:
                        await conn.execute("UPDATE content_de_codex.formula_regions SET latex_candidate='qa deliberate scalar drift' WHERE release_id=$1 AND formula_region_id=(SELECT formula_region_id FROM content_de_codex.formula_regions WHERE release_id=$1 LIMIT 1)",release)
                    try:await structured_store.activation_guard(conn,release);rejected=False
                    except ValueError:rejected=True
                    checks.append({'name':'typed_'+table+'_'+field+'_drift_blocks_activation','passed':rejected})
                finally:await savepoint.rollback()
        finally:await outer.rollback()
        await structured_store.activation_guard(conn,release)
        report={'release_id':release,'checks':checks,'all_passed':all(c['passed'] for c in checks),
                'real_postgresql':True,'mutations_rolled_back':True,'all_fingerprints_restored':True,'all_component_fields_compared_with_source':source_roundtrip}
        core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_local_structured_constraints.json',report)
        print(report,flush=True)
        if not report['all_passed']:raise ValueError('real structured constraint acceptance failed')
    finally:await conn.close()

if __name__=='__main__':asyncio.run(run())
