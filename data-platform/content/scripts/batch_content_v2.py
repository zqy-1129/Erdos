"""Read-only source extraction with content-addressed checkpoints and honest pending identities."""
import argparse,collections,hashlib,os,sys,zipfile,xml.etree.ElementTree as ET
from pathlib import Path
import content_runtime as rt
import stage03_fileio as fio
from profile_extraction import extract_pdf_pages,detect_structure,text_metrics,empty_length,figure_candidates
def blank_license():return dict(internal_analysis=False,distribute_original=False,distribute_profile=False,send_to_third_party_model=False)
def extract(record,source,cache,seed_problem=None):
 sha=record['sha256'];ext=record['extension'];fingerprint=dict(source_sha256=sha,extension=ext,extractor_policy='extract-v3',seed_text_sha256=(seed_problem or {}).get('text',{}).get('sha256'));key=hashlib.sha256(rt.json_bytes(fingerprint)).hexdigest();folder=fio.relative(cache,'extract-v3/'+key);proof=folder/'integrity.json'
 if source.name.startswith('~$'):
  fingerprint['source_exclusion']='office_lock';key=hashlib.sha256(rt.json_bytes(fingerprint)).hexdigest();folder=fio.relative(cache,'extract-v3/'+key);proof=folder/'integrity.json'
 if proof.exists():rt.check_snapshot(folder);return rt.load(folder/'result.json'),folder,True
 # Migrate only compatible, hash-checked results from the first complete extraction.
 legacy=fio.relative(cache,'extract-v2/'+sha)
 if (legacy/'integrity.json').exists():
  rt.check_snapshot(legacy);old=rt.load(legacy/'result.json');expected='reused_verified_text' if fingerprint['seed_text_sha256'] else {'.pdf':'text_layer','.docx':'docx_text','.doc':'needs_conversion'}.get(ext,'text' if ext in ['.txt','.csv','.tex','.typ','.md'] else 'source_only')
  if fingerprint.get('source_exclusion'):expected='source_only'
  if old['kind']==expected and old['source_sha256']==sha and old['source_bytes']==record['size']:
   rt.checked(source,sha,record['size'])
   if (legacy/'pages.jsonl').exists():fio.write_exact(folder/'pages.jsonl',(legacy/'pages.jsonl').read_bytes())
   rt.write(folder/'result.json',old);rt.seal(folder,fingerprint,{'migrated_verified_cache':True});return old,folder,True
 pages=[];ext=record['extension'];kind='source_only';tool=None;error=None
 if source.name.startswith('~$'):
  kind='source_only';tool='Excluded Office lock file; not a document'
 elif seed_problem and seed_problem.get('text'):
  textpath=rt.CONTENT.parent.parent/seed_problem['text']['repo_path'];rt.checked(textpath,seed_problem['text']['sha256']);pages=[textpath.read_text(encoding='utf8')];kind='reused_verified_text';tool='historical extraction; original tool version unknown'
 elif ext=='.pdf':
  pages,actual,ver=extract_pdf_pages(source)
  if actual!=sha:raise ValueError('SOURCE_CHANGED_DURING_PDF')
  tool='pypdfium2-'+ver;kind='text_layer'
 elif ext=='.docx':
  data=source.read_bytes();rt.check_zip(data)
  from io import BytesIO
  with zipfile.ZipFile(BytesIO(data)) as z:
   xml=ET.fromstring(z.read('word/document.xml'));pages=['\n'.join(''.join(n.itertext()) for n in xml.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'))]
  tool='stdlib OOXML text; page boundaries unknown';kind='docx_text'
 elif ext in ['.txt','.csv','.tex','.typ','.md']:
  data=source.read_bytes()
  for encoding in ['utf-8-sig','gb18030']:
   try:text=data.decode(encoding);break
   except UnicodeError:text=None
  if text is not None:pages=[text];kind='text';tool='stdlib decode '+encoding
  else:kind='decode_failed'
 elif ext=='.doc':kind='needs_conversion'
 else:kind='source_only'
 rt.checked(source,sha,record['size'])
 if pages:
  lines=[dict(page=i+1 if ext=='.pdf' else None,segment=i+1,text=t,quality='needs_ocr' if not t.strip() else 'partial_unreviewed') for i,t in enumerate(pages)]
  data=b''.join((__import__('json').dumps(x,ensure_ascii=False,allow_nan=False)+'\n').encode('utf8') for x in lines);fio.write_exact(folder/'pages.jsonl',data)
  state='needs_ocr' if not any(t.strip() for t in pages) else 'extracted'
 else:state=kind
 result=dict(source_sha256=sha,status=state,kind=kind,tool=tool,segments=len(pages),page_count=len(pages) if ext=='.pdf' else None,text_metrics=text_metrics(pages) if pages else None,source_bytes=record['size'])
 rt.write(folder/'result.json',result);rt.seal(folder,fingerprint);return result,folder,False
def source_asset(aid,role,cid,owner,record):
 return dict(asset_id=aid,role=role,competition_id=cid,owner_id=owner,version=1,oss_key=record['relative_path'],sha256=record['sha256'],size_bytes=record['size'],media_type='application/pdf' if record['extension']=='.pdf' else 'application/octet-stream',license_status='unknown',license=blank_license(),license_evidence=None,schema_version=1)
def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data-root',default='D:/Erdos_data');p.add_argument('--inventory',default='out/inventory/codex_continuous');p.add_argument('--out',default='normalized/competitions_codex_v5');p.add_argument('--cache',default='out/processing_cache');p.add_argument('--stage04',default='normalized/stage04/cumcm_codex_v4');p.add_argument('--previous');p.add_argument('--max-documents',type=int);p.add_argument('--resume',action='store_true');a=p.parse_args(argv)
 source_root=fio.no_links(a.data_root);inv=fio.no_links(a.inventory);out=fio.no_links(a.out);cache=fio.no_links(a.cache)
 rt.disjoint_sources(source_root,out);rt.disjoint_sources(source_root,cache)
 records=[rt.parse(line) for line in (inv/'source_files.jsonl').read_text(encoding='utf8').splitlines() if line.strip()];registry=rt.load(rt.CONTENT/'config/competition_registry.json');rt.check_snapshot(a.stage04);s4=rt.load(Path(a.stage04)/'bundle.json')
 oldp=rt.load(rt.CONTENT/'out/pack/problems_seed_v0_sources.json')['items'];oldc=rt.load(rt.CONTENT/'out/pack/cases_seed_v0_sources.json')['items'];seedp={x['source']['rel']:x for x in oldp};seedc={x['source']['rel']:x for x in oldc};identities=rt.load(rt.CONTENT/'config/continuous_identity_review.json') if (rt.CONTENT/'config/continuous_identity_review.json').exists() else {'problems':[],'cases':[]}
 old_records={x['business_id']:x for x in rt.load(rt.CONTENT/'out/pack/problems.json')};knownp={x['problem_id']:x for x in s4['problems']};knownc={x['case_id']:x for x in s4['cases']};rows=[];processed={};reused=0;attempted=0;ledger={}
 previous_rows=[];previous_bundle={'problems':[],'cases':[],'assets':[]}
 if a.previous:
  rt.check_snapshot(a.previous);ledger=rt.load(Path(a.previous)/'source_identity_ledger.json');previous_bundle=rt.load(Path(a.previous)/'bundle.json')
  if (Path(a.previous)/'source_records.json').exists():previous_rows=rt.load(Path(a.previous)/'source_records.json')
  else:
   for file in (Path(a.previous)/'competitions').glob('*/sources.json'):previous_rows+=rt.load(file)
   # The first snapshot omitted shared sources from per-competition lists.
   if rt.load(Path(a.previous)/'integrity.json')['inputs'].get('inventory_sha256')==rt.checked(inv/'source_files.jsonl')['sha256']:
    existing={r['relative_path'] for r in previous_rows}
    for r in records:
     cid=r['competition_ids'][0] if r['competition_ids'] else 'shared';key=cid+'|'+r['relative_path']
     if r['relative_path'] not in existing and key in ledger:previous_rows.append(dict(source_id=ledger[key],relative_path=r['relative_path'],competition_ids=r['competition_ids'],sha256=r['sha256'],extension=r['extension']))
  previous_rows=list({r['source_id']:r for r in previous_rows}.values())
 previous_by_path={r['relative_path']:r for r in previous_rows};current_paths={r['relative_path'] for r in records};rename_candidates=collections.defaultdict(list)
 for r in previous_rows:
  if r['relative_path'] not in current_paths:rename_candidates[(tuple(r['competition_ids']),r['sha256'],r['extension'])].append(r)
 rename_map={};delta=[]
 prior_assets={x['asset_id']:x for x in previous_bundle['assets']};prior_problems={x['problem_id']:x for x in previous_bundle['problems']};prior_cases={x['case_id']:x for x in previous_bundle['cases']};mapping=[]
 for record in records:
  rel=record['relative_path'];cid=record['competition_ids'][0] if record['competition_ids'] else 'shared';key=cid+'|'+rel;sid=ledger.get(key,cid+'-src-'+hashlib.sha256(rel.encode('utf8')).hexdigest()[:16]);old=previous_by_path.get(rel);matches=rename_candidates.get((tuple(record['competition_ids']),record['sha256'],record['extension']),[])
  if old:change='unchanged' if old['sha256']==record['sha256'] else 'changed'
  elif len(matches)==1 and matches[0]['source_id'] not in rename_map.values():
   old=matches[0];sid=old['source_id'];rename_map[old['relative_path']]=sid;change='renamed'
  else:change='added'
  delta.append(dict(source_id=sid,change=change,relative_path=rel,previous_relative_path=old['relative_path'] if old else None,previous_sha256=old['sha256'] if old else None,sha256=record['sha256']));ledger[key]=sid
  row=dict(source_id=sid,competition_ids=record['competition_ids'],relative_path=rel,sha256=record['sha256'],size=record['size'],extension=record['extension'],scan_status=record['status'],source_root=record['source_root'],resource_type_hint=record['resource_type_hint'],identity_status='pending_content_review')
  if record['status']!='ok':row['processing_status']=record['status'];rows.append(row);continue
  if a.max_documents is not None and attempted>=a.max_documents:row['processing_status']='queued_budget';rows.append(row);continue
  attempted+=1
  try:
   source=fio.relative(source_root,rel);rt.checked(source,record['sha256'],record['size']);result,folder,hit=extract(record,source,cache,seedp.get(rel));reused+=int(hit)
   row.update(processing_status=result['status'],extraction_kind=result['kind'],page_count=result['page_count'],recognized_text_metrics=result['text_metrics']);processed[rel]=(record,result,folder)
   if (folder/'pages.jsonl').is_file():
    dest='sources/'+sid+'/pages.jsonl';copied=fio.copy_verified(folder/'pages.jsonl',fio.relative(out,dest))
    if copied['conflict']:raise ValueError('EXTRACTION_OUTPUT_CONFLICT')
    row['text_rel']=dest;row['text_sha256']=rt.checked(fio.relative(out,dest))['sha256']
   mapping.append(dict(source_id=sid,business_id=seedp.get(rel,seedc.get(rel,{})).get('business_id'),relative_path=rel,source_sha256=record['sha256']))
  except (OSError,ValueError,RuntimeError,zipfile.BadZipFile) as e:row.update(processing_status='failed',error=str(e))
  rows.append(row)
  if attempted and attempted%25==0:
   state=dict(attempted=attempted,reused=reused,last_source=rel,inventory_sha256=rt.checked(inv/'source_files.jsonl')['sha256']);(cache/'checkpoint.json').parent.mkdir(parents=True,exist_ok=True);(cache/'checkpoint.json').write_bytes(rt.json_bytes(state));print('Processed '+str(attempted)+' sources; cache reused '+str(reused),flush=True)
 problems=[];cases=[];assets=[];bindings=[];identityproof=[];byrel={x['relative_path']:x for x in records}
 path_rewrites={r['previous_relative_path']:r['relative_path'] for r in delta if r['change']=='renamed'}
 for old in previous_rows:
  if old['relative_path'] not in current_paths and old['relative_path'] not in rename_map:delta.append(dict(source_id=old['source_id'],change='removed',relative_path=None,previous_relative_path=old['relative_path'],previous_sha256=old['sha256'],sha256=None))
 def add_original(role,cid,owner,record):
  previous_rel=next((old for old,new in path_rewrites.items() if new==record['relative_path']),record['relative_path']);aid=owner+'-'+role+'-'+hashlib.sha256(previous_rel.encode('utf8')).hexdigest()[:12];asset=source_asset(aid,role,cid,owner,record);prior=prior_assets.get(aid)
  if prior:asset['version']=prior['version']+int(prior['sha256']!=asset['sha256'])
  assets.append(asset);bindings.append(dict(asset_id=aid,root_kind='data',local_rel=record['relative_path']));return {k:asset[k] for k in ['asset_id','version','sha256']}
 # Existing IDs are reused, but an old parent hint alone never confirms a relationship.
 parent_sources={x['business_id']:x['source']['rel'] for x in oldp}
 parent_sources.update({x['problem_id']:x['source_rel'] for x in identities['problems']})
 parent_sources={pid:path_rewrites.get(rel,rel) for pid,rel in parent_sources.items()}
 for pid,rel in parent_sources.items():
  if pid in knownp or rel not in processed:continue
  rec,result,folder=processed[rel]
  if result['status']!='extracted':continue
  cid=rec['competition_ids'][0];ref=add_original('problem',cid,pid,rec);pages=[rt.parse(l)['text'] for l in (folder/'pages.jsonl').read_text(encoding='utf8').splitlines()]
  review=next((x for x in identities['problems'] if x['problem_id']==pid),None)
  if review:
   if rec['sha256']!=review['source_sha256'] or any(term.lower() not in '\n'.join(pages).lower() for term in review['terms']):raise ValueError('PROBLEM_IDENTITY_REVIEW_LOCK')
   identityproof.append(review)
  prof=dict(problem_id=pid,competition_id=cid,problem_types=['unknown'],confidence=None,label_status='unknown',provenance=dict(kind='import',evidence=None,note='Stable source identity; task classification pending'),subproblems=[],source_sha256=rec['sha256'],source_asset_ref=ref,schema_version=1,profile_version=1,review_status='pending_review')
  if pid in prior_problems:
   prev=prior_problems[pid];prof['profile_version']=prev['profile_version'];prof['profile_version']+=int(prof!=prev)
  problems.append(prof);rt.write(fio.relative(out,'competitions/'+cid+'/problems/'+pid+'/profile.json'),prof)
  seed=next((x for x in oldp if x['business_id']==pid),None)
  for attachment in (seed or {}).get('attachments',[]):
   r=byrel.get(path_rewrites.get(attachment['rel'],attachment['rel']))
   if r and r['status']=='ok' and r['sha256']==attachment['sha256']:add_original('attachment',cid,pid,r)
 pmap={x['problem_id']:x for x in problems};pmap.update(knownp)
 case_sources={x['business_id']:x['source']['rel'] for x in oldc};case_sources.update({x['case_id']:x['source_rel'] for x in identities['cases']})
 case_sources={bid:path_rewrites.get(rel,rel) for bid,rel in case_sources.items()}
 for caseid,rel in case_sources.items():
  if caseid in knownc or rel not in processed:continue
  rec,result,folder=processed[rel]
  if result['status']!='extracted' or rec['extension']!='.pdf':continue
  cid=rec['competition_ids'][0];ref=add_original('case_paper',cid,caseid,rec);pages=[rt.parse(l)['text'] for l in (folder/'pages.jsonl').read_text(encoding='utf8').splitlines()];sha=rec['sha256'];parent=None;review=next((x for x in identities['cases'] if x['case_id']==caseid),None)
  if review:
   if review['problem_id'] not in pmap or review['source_sha256']!=sha or any(term.lower() not in pages[review['page']-1].lower() for term in review['terms']):raise ValueError('CASE_IDENTITY_REVIEW_LOCK')
   parent=review['problem_id'];identityproof.append(review)
  structure,toc=detect_structure(pages,sha,parent)
  valid_subs={s['subproblem_id'] for s in pmap[parent]['subproblems']} if parent else set()
  for section in structure:
   if section['subproblem_ref'] not in valid_subs:section['subproblem_ref']=None
  prof=dict(case_id=caseid,competition_id=cid,problem_id=parent,resolution_status='resolved' if parent else 'unresolved',publication_status='staging',source_sha256=sha,source_asset_ref=ref,extractor_version=result['tool']+'/batch-v2',schema_version=1,profile_version=1,review_status='pending_review',license=blank_license(),license_evidence=None,structure=structure,writing=[],length=empty_length(),figures=[])
  if structure:
   first=structure[0];prof['writing']=[dict(section=first['section_type'],purpose='Observed section order',organization=first['section_title'],method_description=None,evidence=first['evidence'],kind='fact',confidence=None)]
  if caseid in prior_cases:
   prev=prior_cases[caseid];prof['profile_version']=prev['profile_version'];prof['profile_version']+=int(prof!=prev)
  relprof='competitions/'+cid+'/cases/'+caseid+'/profile.json';rt.write(fio.relative(out,relprof),prof);rt.write(fio.relative(out,'competitions/'+cid+'/cases/'+caseid+'/evidence.json'),dict(source_sha256=sha,figure_candidates=figure_candidates(pages),recognized_metrics=text_metrics(pages),identity=review,quality='pending review'))
  data=fio.relative(out,relprof).read_bytes();aid=caseid+'-case_profile';assets.append(dict(asset_id=aid,role='case_profile',competition_id=cid,owner_id=caseid,version=prof['profile_version'],oss_key=relprof,sha256=hashlib.sha256(data).hexdigest(),size_bytes=len(data),media_type='application/json',license_status='unknown',license=blank_license(),license_evidence=None,schema_version=1));bindings.append(dict(asset_id=aid,root_kind='aggregate',local_rel=relprof));cases.append(prof)
 bundle=dict(problems=problems,cases=cases,assets=assets,is_fixture=False);rt.validate(bundle);coverage=[]
 by_asset={x['asset_id']:x for x in assets};business_by_source={b['local_rel']:by_asset[b['asset_id']]['owner_id'] for b in bindings if b['root_kind']=='data' and by_asset[b['asset_id']]['role'] in ['problem','case_paper']}
 for r in mapping:
  if r['relative_path'] in business_by_source:r['business_id']=business_by_source[r['relative_path']]
 for comp in registry['competitions']:
  cid=comp['competition_id'];subset=[x for x in rows if cid in x['competition_ids']];counts=dict(collections.Counter(x['processing_status'] for x in subset));ps=[x for x in problems if x['competition_id']==cid]+[x for x in knownp.values() if x['competition_id']==cid];cs=[x for x in cases if x['competition_id']==cid]+[x for x in knownc.values() if x['competition_id']==cid]
  coverage.append(dict(competition_id=cid,display_name=comp['display_name'],scanned_files=len(subset),stable_files=sum(x['scan_status']=='ok' for x in subset),processing=counts,problem_profiles=len(ps),case_profiles=len(cs),resolved_cases=sum(x['resolution_status']=='resolved' for x in cs),pending_identity_cases=sum(x['resolution_status']=='unresolved' for x in cs),manual_reviewed_full=0,licensed_references=0))
  rt.write(fio.relative(out,'competitions/'+cid+'/sources.json'),subset);rt.write(fio.relative(out,'competitions/'+cid+'/index.json'),dict(problem_ids=[x['problem_id'] for x in ps],case_ids=[x['case_id'] for x in cs],problem_type_index={t:[x['problem_id'] for x in ps if t in x['problem_types']] for t in ['optimization','prediction','evaluation','mechanism','classification','statistical_analysis','unknown']}))
 for name,obj in [('bundle.json',bundle),('resource_bindings.json',bindings),('source_records.json',rows),('source_identity_ledger.json',ledger),('source_to_business_mapping.json',mapping),('source_changes.json',dict(previous_snapshot=a.previous,changes=delta,counts=dict(collections.Counter(r['change'] for r in delta)))),('identity_review_evidence.json',identityproof),('nineteen_competition_coverage.json',dict(competitions=coverage,source_status_counts=dict(collections.Counter(x['processing_status'] for x in rows)),scan_files=len(records),processed_attempts=attempted,cache_reused=reused)),('processing_queue.json',[x for x in rows if x['processing_status'] not in ['extracted','source_only']])]:rt.write(out/name,obj)
 rt.seal(out,{'inventory_sha256':rt.checked(inv/'source_files.jsonl')['sha256']});rt.check_snapshot(out);print('Batch snapshot complete: '+str(len(records))+' sources / '+str(len(problems))+' problem profiles / '+str(len(cases))+' case profiles',flush=True);return 0
if __name__=='__main__':
 try:sys.exit(main())
 except (OSError,ValueError,KeyError) as e:print(str(e),file=sys.stderr);sys.exit(1)
