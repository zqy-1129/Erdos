"""Upload completed immutable crop files early; final import verifies the sealed release again."""
from concurrent.futures import ThreadPoolExecutor
from . import core,services

def main():
    root=core.CONTENT_DIR/'normalized/cumcm_delivery/cumcm-2010-2025-codex-r002'
    paths=list((root/'parsed').glob('*/crops/*.png'));services.s3()
    def upload(path):
        sha=core.sha256_of(path);services.put_verified(path,sha);return sha
    shas=[]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for count,sha in enumerate(pool.map(upload,paths),1):
            shas.append(sha)
            if count%1000==0:print('real crop objects verified {}/{}'.format(count,len(paths)),flush=True)
    core.write_json(root/'quality/crop_preupload.json',{'input_crop_files_at_start':len(paths),'verified':len(shas),'unique_crop_objects':len(set(shas)),'complete_for_start_snapshot':True,'final_release_import_rechecks_all_assets':True})
    print('crop object preupload completed',flush=True)

if __name__=='__main__':main()
