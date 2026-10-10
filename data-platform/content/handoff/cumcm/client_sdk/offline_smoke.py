import importlib.util,json,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('offline',Path(__file__).parent/'offline_sdk.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
r=m.OfflineRelease(sys.argv[1],sys.argv[2]);b=r.bootstrap();c=r.components('00474738e7498bbb507905b2d88aabad4c55103aafdf88fc6635af043f5a3f88',9)
f=next(f for f in c['formula_regions'] if f.get('transcription_verified'));a=f['crop_ref'];r.fetch(a['asset_id'],a['version'],a['sha256'])
print(json.dumps({'release_id':b['release_id'],'pinned':b['manifest_sha256']==sys.argv[2],'stdlib_only':not any(x in sys.modules for x in ('torch','asyncpg','boto3','onnxruntime','cumcm_delivery'))}))
