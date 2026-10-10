"""Pre-upload immutable original/derived assets while OCR runs. Full release import still verifies pins."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from . import core,services,audit

def main():
    args=argparse.Namespace(run_id='cumcm-2010-2025-codex-r002',data_root='D:/Erdos_data')
    rd=audit.run_dir(args)
    unique={r['sha256']:r for r in audit.all_sources(args)+core.load_jsonl(rd/'converted_sources.jsonl')}
    services.s3()
    def upload(row):
        key=services.put_verified(audit.source_path(row,args),row['sha256'])
        return {'sha256':row['sha256'],'object_key':key,'status':'uploaded_and_readback_sha256_verified'}
    got=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(upload,unique.values()):
            got.append(result)
            if len(got)%100==0:
                core.write_json(rd/'quality/object_preupload.json',{'expected':len(unique),'verified':len(got),'complete':False})
                print('object readback {}/{}'.format(len(got),len(unique)),flush=True)
    core.write_json(rd/'quality/object_preupload.json',{'expected':len(unique),'verified':len(got),'complete':True,'method':'real S3 GET bytes SHA256'})
    core.write_jsonl(rd/'quality/object_preupload_items.jsonl',got)
    print('source object preupload complete',flush=True)

if __name__=='__main__':main()
