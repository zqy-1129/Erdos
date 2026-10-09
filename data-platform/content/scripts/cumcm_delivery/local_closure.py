"""Local-only continuation: durable checkpoints and conservative source reuse."""
import argparse, json, os,time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from . import core, audit

CONFIG = core.CONTENT_DIR/'config/cumcm_delivery/local_closure.json'

def config():
    return core.load_json(CONFIG)

def args():
    c=config()
    return argparse.Namespace(run_id=c['run_id'],release_id=c['release_id'],
        data_root='D:/Erdos_data',scope_config='config/cumcm_delivery/scope.json',
        target='local-audit',ocr=True,max_documents=0,activate=False)

@contextmanager
def state_lock(path):
    import msvcrt
    lock=core.output_path(path.with_suffix('.lock'));lock.parent.mkdir(parents=True,exist_ok=True)
    with open(str(lock),'a+b') as stream:
        if stream.tell()==0:stream.write(b'0');stream.flush()
        deadline=time.monotonic()+60
        while True:
            stream.seek(0)
            try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1);break
            except OSError:
                if time.monotonic()>deadline:raise
                time.sleep(.1)
        try:yield
        finally:stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)

def state(stage, status, evidence=None, details=None):
    rd=audit.run_dir(args());path=rd/'quality/local_closure_state.json'
    with state_lock(path):
        value=core.load_json(path) if path.exists() else {'scope':config()['scope'],'steps':{}}
        value['steps'][stage]={'status':status,'updated_at':core.now_utc_iso(),
            'evidence':evidence or [],'details':details or {}}
        core.write_json(path,value)

def initialize():
    c=config();rd=audit.run_dir(args())
    base=core.safe_relpath(core.CONTENT_DIR,'out/cumcm_delivery/releases/'+c['base_release_id'])
    seal=core.load_json(base/'SEALED.json')
    if core.sha256_of(base/'manifest.json')!=seal['manifest_sha256']:
        raise ValueError('base sealed manifest drift')
    manifest=core.load_json(base/'manifest.json')
    pin=rd/'quality/base_release_pin.json'
    if pin.exists():
        recorded=core.load_json(pin)
        if recorded['manifest_sha256']!=seal['manifest_sha256']:
            raise ValueError('base release pin changed')
        print('completed source reuse retained; derivative work left intact',flush=True)
        return
    selected=[e for e in manifest['files'] if e['path'].startswith('metadata/')
        and e['path'].endswith(('.json','.jsonl'))]
    def copy(entry):
        source=core.safe_relpath(base,entry['path'])
        relative=entry['path'][len('metadata/'):]
        # The release stores derived-source ledgers under sources; the CLI uses root.
        if relative in ('sources/derived_sources.jsonl','sources/converted_sources.jsonl'):
            relative=relative.split('/')[-1]
        target=rd/relative
        if target.exists():
            if core.sha256_of(target)!=entry['sha256']:
                raise ValueError('incomplete initialization contains changed bytes: '+relative)
            return
        audit.copy_verified(source,target,entry['sha256'])
    with ThreadPoolExecutor(max_workers=6) as pool:
        for n,_ in enumerate(pool.map(copy,selected),1):
            if n%2000==0:print('verified base metadata reused {}/{}'.format(n,len(selected)),flush=True)
    # Format archives live as assets rather than JSON metadata.
    for asset in core.load_json(base/'assets.json'):
        origin=next((o for o in asset['origins'] if o['role']=='format_template'),None)
        if origin:
            name='cumcm-'+origin['format']+'-v2.zip'
            audit.copy_verified(core.safe_relpath(base,asset['release_path']),rd/'templates'/name,asset['sha256'])
    core.write_json(rd/'quality/base_release_pin.json',{
        'release_id':c['base_release_id'],'manifest_sha256':seal['manifest_sha256'],
        'selected_metadata_files':len(selected),'reused_existing_real_parse':True,
        'originals_modified':False,'sealed_release_modified':False})
    state('initialize','pass',['quality/base_release_pin.json'])
    print('local continuation initialized',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['initialize'])
    command=parser.parse_args().command
    if command=='initialize':initialize()
