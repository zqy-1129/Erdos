"""Hash-verified local draft consumer. Staging inspection is separate from model reference use."""
import argparse,hashlib,json,re,sys
from pathlib import Path
import content_runtime as rt
import stage03_fileio as fio
CONTENT_DIR=rt.CONTENT
DEFAULT_RELEASE='out/releases/content-candidate-codex-v3'
def load_json(path):return rt.load(path)
class ReleaseStore:
 def __init__(self,release_root,expected_integrity=None):
  self.root=fio.no_links(release_root)
  if expected_integrity:rt.checked(self.root/'integrity.json',expected_integrity)
  self.proof=rt.check_snapshot(self.root,full=False);self.catalog=self.doc('catalog.json');self._assets={};self._bindings={};self._family_index={};self._reference_index={};self._bundles={}
  ids=[x['competition_id'] for x in self.catalog['competitions']]
  if len(ids)!=len(set(ids)):raise ValueError('DUPLICATE_COMPETITION')
 def doc(self,rel):return rt.locked_json(self.root,rel,self.proof)
 def competition(self,cid):return next((c for c in self.catalog['competitions'] if c['competition_id']==cid),None)
 def _load(self,cid):
  if cid in self._assets:return
  if self.competition(cid) is None:raise ValueError('UNKNOWN_COMPETITION: '+cid)
  rt.identifier(cid);prefix='competitions/'+cid+'/'
  assets=self.doc(prefix+'assets.json');bindings=self.doc(prefix+'local_bindings.json');bundle=self.doc(prefix+'bundle.json');rt.validate(bundle)
  if assets!=bundle['assets'] or len(assets)!=len({x['asset_id'] for x in assets}) or set(bindings)!={x['asset_id'] for x in assets}:raise ValueError('ASSET_MAPPING')
  if any(x['competition_id']!=cid for x in assets):raise ValueError('WRONG_COMPETITION')
  fi=self.doc(prefix+'index/family_index.json');refs=self.doc(prefix+'index/reference_index.json')
  family=bundle['families'][0]
  if fi['family_id']!=family['family_id'] or fi['family_version']!=family['family_version'] or fi.get('ruleset_version')!=family['ruleset_version']:raise ValueError('FAMILY_INDEX_LOCK')
  self._assets[cid]={x['asset_id']:x for x in assets};self._bindings[cid]=bindings;self._family_index[cid]=fi;self._reference_index[cid]=refs;self._bundles[cid]=bundle
 def family_index(self,cid):self._load(cid);return self._family_index[cid]
 def references(self,cid):self._load(cid);return self._reference_index[cid]['references']
 def asset(self,cid,aid):self._load(cid);return self._assets[cid].get(aid)
 def _cid_of(self,aid):
  for comp in self.catalog['competitions']:
   cid=comp['competition_id'];self._load(cid)
   if aid in self._assets[cid]:return cid
  return None
 def resolve_path(self,cid,aid):
  self._load(cid);return fio.relative(self.root,'competitions/'+cid+'/'+self._bindings[cid][aid])
 def read(self,cid,aid):
  asset=self.asset(cid,aid)
  if asset is None:raise ValueError('UNKNOWN_ASSET')
  path=self.resolve_path(cid,aid);data=path.read_bytes()
  if hashlib.sha256(data).hexdigest()!=asset['sha256'] or len(data)!=asset['size_bytes']:raise ValueError('RESOURCE_HASH_SIZE: '+aid)
  rel='competitions/'+cid+'/'+self._bindings[cid][aid]
  if self.proof['outputs'].get(rel)!=asset['sha256']:raise ValueError('RESOURCE_MANIFEST_LOCK')
  if asset['media_type'] in ['application/zip','application/vnd.openxmlformats-officedocument.wordprocessingml.document']:rt.check_zip(data)
  return data
 def ref(self,cid,aid):return {k:self.asset(cid,aid)[k] for k in ['asset_id','version','sha256']}
 def gate(self,cid,aid,mode='local_only',purpose='consumer'):
  asset=self.asset(cid,aid)
  if purpose=='audit' and mode=='local_only':return
  if mode=='third_party' and not asset['license']['send_to_third_party_model']:raise ValueError('LICENSE_SCOPE: resource cannot be sent to a model')
  if asset['role'] in ['case_paper','case_profile','writing_profile']:
   if not asset['license']['internal_analysis']:raise ValueError('REFERENCE_NOT_READY: asset lacks analysis permission')
   if asset['role']=='case_profile':
    case=next(c for c in self._bundles[cid]['cases'] if c['case_id']==asset['owner_id'])
    if not case['license']['internal_analysis'] or case['review_status']!='approved' or case['resolution_status']!='resolved':raise ValueError('REFERENCE_NOT_READY: source case')
    if mode=='third_party' and not case['license']['send_to_third_party_model']:raise ValueError('LICENSE_SCOPE: source case')
def emit(obj):print(json.dumps(obj,ensure_ascii=False,indent=2))
def cmd_list(store,args):emit(dict(release_id=store.catalog['release_id'],count=len(store.catalog['competitions']),competitions=store.catalog['competitions']));return 0
def startup(store,cid,fmt,mode):
 fi=store.family_index(cid)
 if fmt not in fi['formats']:raise ValueError('FORMAT_UNAVAILABLE')
 required={k:fi['start_resources'][k] for k in ['ruleset','base_outline','writing_policy','figure_style','table_style']};required['layout']=fi['start_resources']['layouts'][fmt]
 refs={};local=[]
 for name,rel in required.items():
  ids=[aid for aid,binding in store._bindings[cid].items() if binding==rel]
  if len(ids)!=1:raise ValueError('STARTUP_BINDING')
  aid=ids[0];store.gate(cid,aid,mode);store.read(cid,aid);refs[name]=store.ref(cid,aid);local.append(dict(asset_ref=refs[name],relative_path=rel,verified=True))
 return fi,refs,local
def cmd_select(store,args):
 fi,refs,resources=startup(store,args.cid,args.format or store.family_index(args.cid)['formats'][0],getattr(args,'model_execution','local_only'))
 emit(dict(fi,format=args.format,layout=fi['start_resources']['layouts'].get(args.format),verified_resources=resources));return 0
def cmd_search(store,args):
 cid=args.cid;store._load(cid);tags=sorted(set(t.strip() for t in args.tags.split(',') if t.strip()))
 if not tags:raise ValueError('EMPTY_TAGS')
 mode=getattr(args,'model_execution','local_only');results=[];cases={c['case_id']:c for c in store._bundles[cid]['cases']}
 for row in store.references(cid):
  matched=sorted(set(tags)&set(row['tags']))
  if not matched or (args.tags_all and len(matched)!=len(tags)):continue
  c=cases.get(row['case_id']);ref=row.get('profile_asset_ref')
  if c is None or ref is None:raise ValueError('REFERENCE_INDEX_LOCK')
  if row['competition_id']!=cid or row['problem_id']!=c['problem_id'] or row['source_sha256']!=c['source_sha256'] or row['profile_version']!=c['profile_version'] or row['license']!=c['license']:raise ValueError('REFERENCE_INDEX_LOCK')
  if not c['license']['internal_analysis'] or c['review_status']!='approved' or c['resolution_status']!='resolved':continue
  try:store.gate(cid,ref['asset_id'],mode)
  except ValueError:continue
  if ref!=store.ref(cid,ref['asset_id']):raise ValueError('PROFILE_LOCK')
  profile=rt.parse(store.read(cid,ref['asset_id']).decode('utf8'))
  if profile!=c:raise ValueError('PROFILE_CONTENT_LOCK')
  results.append(dict(case_id=c['case_id'],problem_id=c['problem_id'],profile_version=c['profile_version'],source_sha256=c['source_sha256'],profile_asset_ref=ref,matched_tag_count=len(matched),matched_tags=matched,tags=row['tags']))
 results.sort(key=lambda r:(-r['matched_tag_count'],r['case_id']))
 if results:emit(dict(competition_id=cid,result='ok',matched_references=results,ranking='distinct matching labels desc, case_id asc'));return 0
 emit(dict(competition_id=cid,result='no_reference',reason='No reviewed, permitted reference matches; unknown labels never manufacture a match',fallback='generic_framework',fallback_note='Draft framework; competition rules unverified'));return 1
def cmd_get(store,args):
 cid=store._cid_of(args.asset_id)
 if cid is None:return 2
 asset=store.asset(cid,args.asset_id)
 if args.version is not None and asset['version']!=args.version:return 2
 store.gate(cid,args.asset_id,getattr(args,'model_execution','local_only'),getattr(args,'purpose','consumer'));data=store.read(cid,args.asset_id)
 cache=fio.no_links(args.cache_dir or rt.CONTENT/'out/cache')
 for base in [store.root,rt.CONTENT/'normalized',rt.CONTENT/'out/pack',Path('D:/Erdos_data')]:rt.disjoint_sources(base,cache)
 cache.mkdir(parents=True,exist_ok=True)
 name=hashlib.sha256((args.asset_id+str(asset['version'])+asset['sha256']).encode()).hexdigest();path=fio.relative(cache,name)
 source='package'
 if path.exists():
  cached=path.read_bytes()
  if hashlib.sha256(cached).hexdigest()==asset['sha256'] and len(cached)==asset['size_bytes']:data=cached;source='cache'
  else:raise ValueError('CACHE_HASH: preserved corrupt cache; remove explicitly before retry')
 else:fio.write_exact(path,data)
 if args.output:
  target=fio.no_links(args.output)
  for base in [store.root,rt.CONTENT/'normalized',rt.CONTENT/'out/pack',Path('D:/Erdos_data')]:
   try:
    if __import__('os').path.commonpath([str(target),str(fio.no_links(base))])==str(fio.no_links(base)):raise ValueError('PROTECTED_OUTPUT')
   except ValueError as exc:
    if str(exc)=='PROTECTED_OUTPUT':raise
  fio.write_exact(target,data)
 emit(dict(asset_id=args.asset_id,version=asset['version'],sha256=asset['sha256'],size_bytes=len(data),role=asset['role'],verified=True,source=source));return 0
def cmd_outline(store,args):
 cid=args.cid;fi=store.family_index(cid);fmt=args.format or fi['formats'][0];mode=getattr(args,'model_execution','local_only');fi,refs,local=startup(store,cid,fmt,mode)
 task_bytes=fio.no_links(args.task_json).read_bytes() if getattr(args,'task_json',None) else None;task=rt.parse(task_bytes.decode('utf8')) if task_bytes else None
 if task is not None:
  if not isinstance(task,dict) or not isinstance(task.get('subproblems'),list) or not isinstance(task.get('task_id'),str):raise ValueError('TASK_INPUT')
  if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,200}',task['task_id']):raise ValueError('TASK_ID')
  ids=[];taxonomy=rt.load(rt.CONTENT/'config/taxonomy_v1.json')
  for row in task['subproblems']:
   if not isinstance(row,dict) or not isinstance(row.get('subproblem_id'),str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,200}',row['subproblem_id']):raise ValueError('TASK_SUBPROBLEM')
   ids.append(row['subproblem_id'])
   for key in ['problem_types','method_tags','data_tags','domain_tags']:
    values=row.get(key,[]);entries=taxonomy[key]['entries'] if key=='method_tags' else taxonomy[key];allowed={r['code'] for r in entries}
    if not isinstance(values,list) or any(not isinstance(v,str) or v not in allowed for v in values):raise ValueError('TASK_LABEL: '+key)
  if len(ids)!=len(set(ids)):raise ValueError('TASK_DUPLICATE_SUBPROBLEM')
  if 'is_fixture' in task and not isinstance(task['is_fixture'],bool):raise ValueError('TASK_FIXTURE_FLAG')
 n=len(task['subproblems']) if task else int(args.subproblems)
 if n<1 or n>100:raise ValueError('SUBPROBLEM_COUNT: 1..100')
 if task and task.get('competition_id',cid)!=cid:raise ValueError('TASK_COMPETITION')
 sections=rt.parse(store.read(cid,refs['base_outline']['asset_id']).decode('utf8'))['sections'];dynamic=[]
 def append(sec,i):dynamic.append(dict(section_type=sec['section_type'],title=sec['title']+((' / Q'+str(i+1)) if i is not None else ''),subproblem_index=i+1 if i is not None else None,subproblem_id=task['subproblems'][i]['subproblem_id'] if task and i is not None else None,optional=sec['optional'],purpose='Fill from current task methods, results and evidence',inputs_required=['current_task_model','current_task_results','current_task_validation']))
 emitted=False
 for sec in sections:
  if sec['repeat']=='per_subproblem':
   if emitted:continue
   for i in range(n):
    for block in sections:
     if block['repeat']=='per_subproblem':append(block,i)
   emitted=True
  else:append(sec,None)
 unit='zh_chars' if fi['language']=='zh' else 'en_words';budget=getattr(args,'budget',None)
 if budget is not None and budget<1:raise ValueError('LENGTH_BUDGET')
 length=dict(unit=unit,target=budget,basis='explicit_user_budget' if budget else 'unspecified',verified=False,section_budgets=[])
 if budget:
  weights=[3 if s['section_type'] in ['model','solve'] else 1 for s in dynamic];total=sum(weights);remaining=budget
  for i,(sec,w) in enumerate(zip(dynamic,weights)):
   amount=remaining if i==len(dynamic)-1 else int(budget*w/total);remaining-=amount;length['section_budgets'].append(dict(section_index=i,amount=amount,unit=unit))
 requested=[]
 for sub in (task or {}).get('subproblems',[]):
  types=sub.get('problem_types',[]);kind='comparison' if 'evaluation' in types else 'objective_or_feasible_region' if 'optimization' in types else 'fit_with_residuals' if set(types)&{'prediction','statistical_analysis'} else 'mechanism_diagram' if 'mechanism' in types else 'confusion_matrix' if 'classification' in types else 'unspecified'
  requested.append(dict(subproblem_id=sub['subproblem_id'],kind=kind,requires=['current_task_variables','units','computed_results'],data_scope='current_task',status='needs_task_analysis' if kind=='unspecified' else 'needs_current_results'))
 context=dict(task_id=task.get('task_id') if task else cid+'-fixture-'+str(n)+'q',competition_id=cid,family_id=fi['family_id'],family_version=fi['family_version'],format=fmt,ruleset_id=fi['ruleset_id'],ruleset_version=fi['ruleset_version'],ruleset_status=fi['ruleset_status'],rules_compliance='unverified',local_resources=local,candidate_profiles=[],style_ref=refs['figure_style'],schema_version=1,model_execution=mode,is_fixture=not bool(task) or bool(task.get('is_fixture',False)))
 if task_bytes:context['extensions']={'task_input_sha256':hashlib.sha256(task_bytes).hexdigest()}
 b=dict(store._bundles[cid]);b['task_contexts']=[context];rt.validate(b)
 emit(dict(task_context=context,dynamic_outline=dynamic,length_budget=length,figure_requirements=requested,note='Local draft; reference and third-party permissions enforced independently'));return 0
def build_parser():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release',default=DEFAULT_RELEASE);p.add_argument('--expected-integrity');sub=p.add_subparsers(dest='cmd',required=True);sub.add_parser('list')
 for name in ['select','search','outline']:
  q=sub.add_parser(name);q.add_argument('cid');q.add_argument('--model-execution',choices=['local_only','third_party'],default='local_only')
  if name=='search':q.add_argument('--tags',required=True);q.add_argument('--tags-all',action='store_true')
  else:q.add_argument('--format',choices=['latex','docx'])
  if name=='outline':q.add_argument('--subproblems',type=int,default=3);q.add_argument('--task-json');q.add_argument('--budget',type=int)
 q=sub.add_parser('get');q.add_argument('asset_id');q.add_argument('--version',type=int);q.add_argument('-c','--cache-dir');q.add_argument('-o','--output');q.add_argument('--model-execution',choices=['local_only','third_party'],default='local_only');q.add_argument('--purpose',choices=['consumer','audit'],default='consumer')
 return p
def main(argv=None):
 args=build_parser().parse_args(argv)
 try:
  store=ReleaseStore(args.release,args.expected_integrity);return {'list':cmd_list,'select':cmd_select,'search':cmd_search,'get':cmd_get,'outline':cmd_outline}[args.cmd](store,args)
 except (ValueError,OSError,KeyError,TypeError,StopIteration) as exc:print(str(exc),file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
