"""Allowlisted integration-only package: no corpus, weights, fonts or credentials."""
import zipfile
from . import core,audit,local_closure

def main():
    handoff=core.CONTENT_DIR/'handoff/cumcm';folder=handoff/'client_sdk';folder.mkdir(parents=True,exist_ok=True)
    for name in ('online_sdk.py','offline_sdk.py','local_ai_sdk.py'):
        core.write_bytes(folder/name,(core.CONTENT_DIR/'scripts/cumcm_delivery'/name).read_bytes())
    files={name:folder/name for name in ('online_sdk.py','offline_sdk.py','local_ai_sdk.py','offline_smoke.py')}
    files['README.md']=handoff/'LOCAL_V3.md'
    files['BACKUP.md']=handoff/'BACKUP_V3.md'
    for version in ('v3','v4'):
        for p in sorted((core.CONTENT_DIR/'schemas/cumcm'/version).rglob('*.json')):
            files['schemas/'+version+'/'+p.relative_to(core.CONTENT_DIR/'schemas/cumcm'/version).as_posix()]=p
    a=local_closure.args();seal=core.load_json(core.CONTENT_DIR/'config/cumcm_delivery/release_v3_receipt.json')
    lock={'release_id':a.release_id,'manifest_sha256':seal['manifest_sha256'],
        'scope':'integration code/contracts only; authorized corpus installed separately',
        'files':[{'path':name,'sha256':core.sha256_of(p),'size':p.stat().st_size} for name,p in sorted(files.items())]}
    core.write_json(folder/'SDK_MANIFEST.json',lock);files['SDK_MANIFEST.json']=folder/'SDK_MANIFEST.json'
    archive=handoff/'cumcm-local-client-sdk-v3.zip'
    with zipfile.ZipFile(str(archive),'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name,p in sorted(files.items()):z.writestr(name,p.read_bytes())
    with zipfile.ZipFile(str(archive)) as z:
        if set(z.namelist())!=set(files):raise ValueError('unexpected SDK package entry')
        for name,p in files.items():
            if core.sha256_bytes(z.read(name))!=core.sha256_of(p):raise ValueError('SDK archive bytes drift')
    report={'package':str(archive),'sha256':core.sha256_of(archive),'size':archive.stat().st_size,
        'files':len(files),'contains_corpus_or_credentials_or_models_or_fonts':False,'release_pin':seal['manifest_sha256']}
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_local_sdk_package.json',report);print(report)

if __name__=='__main__':main()
