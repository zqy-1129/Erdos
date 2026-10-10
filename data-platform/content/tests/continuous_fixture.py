"""Sealed synthetic licensed profiles. Real source permissions are never changed."""
import copy,hashlib
import content_runtime as rt
import stage03_fileio as fio
from consumer_v2 import ReleaseStore
def build(root,rows):
 store=ReleaseStore(rt.CONTENT/'out/releases/content-candidate-codex-v2');store._load('cumcm');original=store._bundles['cumcm'];cid='cumcm';cr=root/'competitions'/cid
 bundle={k:copy.deepcopy(original[k]) for k in ['families','rulesets','outlines','figure_styles','table_styles','writing_profiles']};bundle.update(assets=[],problems=[],cases=[],is_fixture=True);bindings={}
 def copy_asset(asset):
  a=copy.deepcopy(asset);rel=store._bindings[cid][a['asset_id']];fio.write_exact(fio.relative(cr,rel),store.read(cid,a['asset_id']));bundle['assets'].append(a);bindings[a['asset_id']]=rel
 for asset in original['assets']:
  if asset['role'] not in ['case_profile','case_paper','problem','attachment']:copy_asset(asset)
 parent=copy.deepcopy(next(p for p in original['problems'] if p['problem_id']=='cumcm-2023-A'));parent['is_fixture']=True;bundle['problems']=[parent];copy_asset(store.asset(cid,parent['source_asset_ref']['asset_id']))
 refs=[];evidence=dict(record_id='synthetic-owner-permission',relative_path='fixture_permission.json');rt.write(cr/'fixture_permission.json',dict(is_fixture=True,owner='test fixture creator',scope='synthetic paper/profile only'))
 for row in rows:
  case=copy.deepcopy(next(c for c in original['cases'] if c['problem_id']=='cumcm-2023-A'));case.update(case_id=row['case_id'],problem_id=parent['problem_id'],review_status='approved',license=row['license'],license_evidence=evidence,is_fixture=True,structure=[],writing=[],figures=[],source_sha256=None,source_asset_ref=None,extractor_version='synthetic-test-fixture')
  paper=b'Synthetic unit test paper '+row['case_id'].encode('utf8');sha=hashlib.sha256(paper).hexdigest();aid=row['case_id']+'-paper';case['source_sha256']=sha;case['source_asset_ref']=dict(asset_id=aid,version=1,sha256=sha)
  def asset(aid,role,data,version):
   rel='files/'+aid+'/v'+str(version)+('/profile.json' if role=='case_profile' else '/paper.txt');fio.write_exact(fio.relative(cr,rel),data);a=dict(asset_id=aid,role=role,competition_id=cid,owner_id=case['case_id'],version=version,oss_key=rel,sha256=hashlib.sha256(data).hexdigest(),size_bytes=len(data),media_type='application/json' if role=='case_profile' else 'text/plain',license_status='audited',license=case['license'],license_evidence=evidence,schema_version=1);bundle['assets'].append(a);bindings[aid]=rel;return {k:a[k] for k in ['asset_id','version','sha256']}
  asset(aid,'case_paper',paper,1);profile_ref=asset(row['case_id']+'-profile','case_profile',rt.json_bytes(case),case['profile_version']);bundle['cases'].append(case);refs.append(dict(row,competition_id=cid,profile_version=case['profile_version'],source_sha256=sha,profile_asset_ref=profile_ref,review_status='approved'))
 rt.validate(bundle);rt.write(root/'catalog.json',dict(store.catalog,release_id='synthetic-fixture',competitions=[dict(store.catalog['competitions'][11],competition_id=cid,reference_count=len(rows))],is_fixture=True))
 for name,obj in [('bundle.json',bundle),('assets.json',bundle['assets']),('local_bindings.json',bindings),('index/family_index.json',store.family_index(cid)),('index/reference_index.json',dict(competition_id=cid,references=refs))]:rt.write(cr/name,obj)
 rt.seal(root,metadata={'is_fixture':True})
