"""Fresh-checkout setup. Git code and the private immutable corpus are separate deliveries."""
import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).absolute().parent))
from cumcm_delivery import core, audit

RECEIPT = core.CONTENT_DIR/'config/cumcm_delivery/release_v3_receipt.json'


def trusted_release(path):
    root=core.no_links(path)
    receipt=core.load_json(RECEIPT)
    if core.sha256_of(root/'manifest.json')!=receipt['manifest_sha256']:
        raise ValueError('release manifest differs from the team-approved pin')
    manifest=audit.validate_release(root)
    if manifest['release_id']!=receipt['release_id']:
        raise ValueError('unexpected release identity')
    return root, manifest


def install_release(source):
    root,manifest=trusted_release(source)
    dest=core.CONTENT_DIR/'out/cumcm_delivery/releases'/manifest['release_id']
    if dest.exists():
        trusted_release(dest)
        print('Already installed; immutable package verified without rewriting')
        return dest
    core.output_path(dest)
    temp_root=core.CONTENT_DIR/'tmp'
    temp_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='release-install-',dir=str(temp_root)) as tmp:
        stage=Path(tmp)/manifest['release_id']
        shutil.copytree(str(root),str(stage))
        trusted_release(stage)
        dest.parent.mkdir(parents=True,exist_ok=True)
        # No replace/merge: preserve a concurrently installed version.
        os.rename(str(stage),str(dest))
    print('Private immutable release installed and all file bytes verified')
    return dest


def install_deps(schemas_only=False,wheelhouse=None):
    targets=[('requirements-schema-validation.txt','.deps/schema_validation')]
    if not schemas_only:targets.append(('requirements-local-api.txt','.runtime/py38'))
    for requirements,folder in targets:
        dest=core.CONTENT_DIR/folder
        if dest.exists() and any(dest.iterdir()):
            raise ValueError('dependency target is not empty; use a fresh checkout or a separately reviewed upgrade: '+folder)
        dest.mkdir(parents=True,exist_ok=True)
        command=[sys.executable,'-m','pip','install','--disable-pip-version-check','--target',str(dest),'-r',str(core.CONTENT_DIR/requirements)]
        if wheelhouse:command+=['--no-index','--find-links',str(core.no_links(wheelhouse))]
        subprocess.run(command,check=True)
    print('Pinned dependencies installed into ignored local targets; system environment unchanged')


def release_args():
    value=core.load_json(RECEIPT)
    return SimpleNamespace(release_id=value['release_id'],run_id='cumcm-2010-2025-codex-r003',target='local-audit',activate=False)


async def db_state(args):
    from cumcm_delivery import services
    conn=await services.pg()
    try:
        if not await conn.fetchval("SELECT to_regclass('content_de_codex.releases') IS NOT NULL"):return None
        row=await conn.fetchrow('SELECT status,manifest_sha256 FROM content_de_codex.releases WHERE release_id=$1',args.release_id)
        if row and row['manifest_sha256']!=core.load_json(RECEIPT)['manifest_sha256']:
            raise ValueError('database release identity conflict')
        return row['status'] if row else None
    finally:await conn.close()


def import_release():
    from cumcm_delivery import services,structured_store
    args=release_args();trusted_release(audit.release_dir(args))
    services.verify_model_pin(core.load_json(audit.release_dir(args)/'model_manifest.json'))
    # Importing precomputed vectors does not require a CUDA session. Query warms explicit CPU.
    services.model(query=True)
    client,bucket=services.s3()
    if bucket not in [b['Name'] for b in client.list_buckets()['Buckets']]:client.create_bucket(Bucket=bucket)
    status=asyncio.run(db_state(args))
    if status!='active':
        if status is None:audit.import_release(args)
        elif status not in ('staging','retired'):raise ValueError('unknown release state')
        if status!='retired':asyncio.run(structured_store.import_structured(args))
        audit.activate(args,'activate')
    print(audit.read_only_check(args,_activation_database_inputs=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('install-deps');p.add_argument('--schemas-only',action='store_true');p.add_argument('--wheelhouse',type=Path)
    p=sub.add_parser('init');p.add_argument('--pg-port',type=int,default=55433);p.add_argument('--s3-port',type=int,default=18333);p.add_argument('--api-port',type=int,default=18789)
    for command in ('verify-release','install-release'):
        p=sub.add_parser(command);p.add_argument('source',type=Path)
    sub.add_parser('import-release');sub.add_parser('check');p=sub.add_parser('start-api');p.add_argument('--lan-host',help='explicit RFC1918 address of this lab computer')
    args=parser.parse_args()
    if args.command=='install-deps':install_deps(args.schemas_only,args.wheelhouse)
    elif args.command=='init':
        from cumcm_delivery.local_config import create
        create(args.pg_port,args.s3_port,args.api_port)
    elif args.command=='verify-release':
        root,manifest=trusted_release(args.source);print('Verified {} sealed files'.format(len(manifest['files'])))
    elif args.command=='install-release':install_release(args.source)
    elif args.command=='import-release':import_release()
    elif args.command=='check':
        from cumcm_delivery import services
        release=release_args();trusted_release(audit.release_dir(release))
        result=audit.read_only_check(release,_activation_database_inputs=True)
        async def check_typed():
            from cumcm_delivery.structured_store import activation_guard
            conn=await services.pg()
            try:
                async with conn.transaction(readonly=True):await activation_guard(conn,release.release_id)
            finally:await conn.close()
        asyncio.run(check_typed());print(result)
    elif args.command=='start-api':
        from cumcm_delivery.api import main as start
        start(args.lan_host)


if __name__=='__main__':main()
