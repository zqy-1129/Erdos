"""Committed typed-field drift must be rejected, with exact fixture restoration."""
import argparse, asyncio
from . import audit, core, services

RELEASE = 'cumcm-2010-2025-qa-inc-v1'

async def exchange(table, field, keyfield, key, value=None, read=False):
    connection = await services.pg()
    try:
        if read:
            return await connection.fetchval('SELECT '+field+' FROM content_de_codex.'+table+' WHERE release_id=$1 AND '+keyfield+'=$2', RELEASE, key)
        await connection.execute('UPDATE content_de_codex.'+table+' SET '+field+'=$3 WHERE release_id=$1 AND '+keyfield+'=$2', RELEASE, key, value)
    finally:
        await connection.close()

def main():
    args = argparse.Namespace(release_id=RELEASE)
    root = audit.release_dir(args)
    problem = core.load_json(root/'metadata/catalog/problems.json')[0]['problem_id']
    unit = core.load_jsonl(root/'metadata/catalog/retrieval_units.jsonl')[0]['unit_id']
    cases = [
        ('problems', 'title', 'problem_id', problem, 'QA drift only', 'typed database scalar drift'),
        ('units', 'kind', 'unit_id', unit, 'qa_invalid_kind', 'typed database unit relation drift'),
        ('releases', 'metadata', 'release_id', RELEASE, '{"qa_drift_only":true}', 'database release metadata drift'),
    ]
    checks = []
    for table, field, keyfield, key, changed, expected in cases:
        original = asyncio.run(exchange(table, field, keyfield, key, read=True))
        try:
            asyncio.run(exchange(table, field, keyfield, key, changed))
            try:
                audit.read_only_check(args, _activation_database_inputs=True)
                rejected = False
            except ValueError as exc:
                rejected = expected in str(exc)
            checks.append({'name': table+'.'+field, 'rejected': rejected})
        finally:
            asyncio.run(exchange(table, field, keyfield, key, original))
        if asyncio.run(exchange(table, field, keyfield, key, read=True)) != original:
            raise ValueError('QA fixture was not exactly restored')
    restored = audit.read_only_check(args, _activation_database_inputs=True)
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_database_drift.json', {
        'checks': checks, 'all_passed': all(c['rejected'] for c in checks),
        'fixture_release_id': RELEASE, 'fixture_restored_and_verified': True,
        'full_release_modified': False, 'restored_check': restored,
    })
    if not all(c['rejected'] for c in checks):
        raise ValueError('database typed-field drift was accepted')
    print('3 committed metadata/typed-field drift cases rejected; fixture restored', flush=True)

if __name__ == '__main__':
    main()
