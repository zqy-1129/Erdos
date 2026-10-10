"""Repair the verified CUMCM sample without overwriting Trae's v2 snapshot."""
import argparse,hashlib,json,sys
from pathlib import Path
import content_runtime as rt
import stage03_fileio as fio
from profile_extraction import extract_pdf_pages,detect_structure,empty_length,text_metrics,figure_candidates
def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage03',default='normalized/stage03/cumcm');p.add_argument('--out',default='normalized/stage04/cumcm_codex_v5');p.add_argument('--profile-version',type=int,default=4);a=p.parse_args(argv)
 if a.profile_version<1:raise ValueError('PROFILE_VERSION')
 s3=fio.no_links(a.stage03);out=fio.no_links(a.out);rt.disjoint_sources(s3,out);rt.disjoint_sources('D:/Erdos_data',out);manifest=rt.load(s3/'sample_manifest.json');b3=rt.load(s3/'bundle.json');reviews=rt.load(rt.CONTENT/'config/continuous_profile_review.json');bindings=manifest['asset_local_bindings'];problems=[];cases=[];assets=list(b3['assets']);newbindings=[]
 for x in bindings:newbindings.append(dict(asset_id=x['asset_id'],root_kind='stage03',local_rel=x['local_rel']))
 definitions={'cumcm-2018-B':(['任务1','任务2'],[['optimization'],['optimization','evaluation']],['scheduling','manufacturing']),
              'cumcm-2023-A':(['问题 1','问题 2','问题 3'],[['mechanism'],['optimization'],['optimization']],['energy']),
              'cumcm-2023-B':(['问题 1','问题 2','问题 3','问题 4'],[['mechanism'],['mechanism'],['optimization'],['optimization']],[])}
 for old in b3['problems']:
  pid=old['problem_id'];rel='problems/'+pid+'/extracted/prompt.zh.txt';text=fio.relative(s3,rel).read_text(encoding='utf8');rt.checked(fio.relative(s3,rel),manifest['outputs'][rel]);markers,types,domains=definitions[pid];subs=[];proof=[]
  positions=[text.find(m) for m in markers]
  if any(x<0 for x in positions):raise ValueError('MISSING_SUBPROBLEM_MARKER: '+pid)
  for i,(marker,kind,start) in enumerate(zip(markers,types,positions)):
   end=positions[i+1] if i+1<len(positions) else len(text);excerpt=text[start:end]
   sub=dict(subproblem_id=pid+':q'+str(i+1),problem_types=kind,method_tags=[],data_tags=['tabular']+(['spatial'] if pid!='cumcm-2018-B' else []),domain_tags=domains,confidence=None,label_status='pending_review',provenance=dict(kind='manual',evidence=dict(source_sha256=old['source_sha256'],locator_type='text_span',start=start,end=end),note='Task objective classification; algorithm not prescribed by question'),source=marker)
   subs.append(sub);proof.append(dict(subproblem_id=sub['subproblem_id'],text_sha256=manifest['outputs'][rel],span=[start,end],span_unit='Unicode characters; [start,end)',excerpt=excerpt,rationale='Requested '+','.join(kind)+'; methods left unspecified'))
  profile=dict(old,profile_version=a.profile_version,problem_types=sorted(set(t for row in types for t in row)),subproblems=subs,provenance=dict(kind='manual',evidence=None,note='Reviewed actual task goals; detailed text hash/span in sidecar'))
  rt.write(fio.relative(out,'problems/'+pid+'/profile.json'),profile);rt.write(fio.relative(out,'problems/'+pid+'/classification_evidence.json'),dict(problem_id=pid,source_sha256=old['source_sha256'],subproblems=proof));problems.append(profile)
 for old in b3['cases']:
  cid=old['case_id'];binding=next(x for x in bindings if x['asset_id']==old['source_asset_ref']['asset_id']);source=fio.relative(s3,binding['local_rel']);rt.checked(source,old['source_sha256']);pages,sha,version=extract_pdf_pages(source)
  if sha!=old['source_sha256']:raise ValueError('PDF_SOURCE_CHANGED')
  structure,toc=detect_structure(pages,sha,old['problem_id']);profile=dict(old,profile_version=a.profile_version,extractor_version='pypdfium2-'+version+'/profile_extraction-v3',structure=structure,length=empty_length(),writing=[],figures=[])
  checked_reviews=[]
  for r in reviews['records']:
   if r['case_id']!=cid:continue
   txt=pages[r['page']-1]
   if any(term not in txt for term in r['terms']):raise ValueError('VISUAL_REVIEW_TEXT_LOCK: '+cid)
   ev=dict(source_sha256=sha,locator_type='page',start=r['page'],end=r['page'])
   if r['kind']=='writing':profile['writing'].append(dict(section=r['section'],purpose=r['purpose'],organization=r['organization'],method_description=None,evidence=ev,kind='fact',confidence=None))
   else:profile['figures'].append(dict(figure_id=r['figure_id'],type=r['type'],purpose=r['purpose'],variables=r['variables'],caption_style=r['caption'],page=r['page'],confidence=None,evidence=ev,result_scope='historical_reference'))
   checked_reviews.append(dict(r,source_sha256=sha,page_text_sha256=hashlib.sha256(txt.encode('utf8')).hexdigest()))
  # Observable heading sequence, not a fabricated explanation of paragraphs.
  if structure:
   s=structure[0];profile['writing'].append(dict(section=s['section_type'],purpose='Observed section organization',organization='Detected explicit heading: '+s['section_title'],method_description=None,evidence=s['evidence'],kind='fact',confidence=None))
  croot=fio.relative(out,'cases/'+cid);rt.write(croot/'profile.json',profile);page_bytes=b''.join((json.dumps(dict(page=i+1,text=t,source_sha256=sha),ensure_ascii=False,allow_nan=False)+'\n').encode('utf8') for i,t in enumerate(pages));fio.write_exact(croot/'extracted/pages.jsonl',page_bytes)
  rt.write(croot/'extracted/diagnostics.json',dict(input_sha256=sha,output_sha256=hashlib.sha256(page_bytes).hexdigest(),tool='pypdfium2',version=version,total_pages=len(pages),quality='partial; formulas/digits not treated as facts',recognized_text_metrics=text_metrics(pages),toc_pages=toc))
  rt.write(croot/'evidence.json',dict(case_id=cid,source_sha256=sha,reviewed_facts=checked_reviews,figure_candidates=figure_candidates(pages),structure_status='numbered-heading candidates; pending review',length_status='original length unknown; recognized counts separate'))
  rel='cases/'+cid+'/profile.json';data=fio.relative(out,rel).read_bytes();aid=cid+'-case_profile' # Stable identity across content revisions.
  assets.append(dict(asset_id=aid,role='case_profile',competition_id='cumcm',owner_id=cid,version=a.profile_version,oss_key=rel,sha256=hashlib.sha256(data).hexdigest(),size_bytes=len(data),media_type='application/json',license_status='unknown',license=old['license'],license_evidence=old['license_evidence'],schema_version=1));newbindings.append(dict(asset_id=aid,root_kind='stage04',local_rel=rel));cases.append(profile)
 bundle=dict(problems=problems,cases=cases,assets=assets,is_fixture=False);rt.validate(bundle)
 for name,obj in [('bundle.json',bundle),('assets.json',assets),('resource_bindings.json',newbindings),('sample_manifest.json',dict(problem_ids=manifest['problem_ids'],case_ids=manifest['case_ids'],profile_version=a.profile_version))]:rt.write(out/name,obj)
 rt.seal(out,{'stage03_manifest':rt.checked(s3/'sample_manifest.json')['sha256'],'review_config':rt.checked(rt.CONTENT/'config/continuous_profile_review.json')['sha256']});rt.check_snapshot(out);print('Verified CUMCM profiles: 3 problems / 7 papers; partial measurements explicit');return 0
if __name__=='__main__':
 try:sys.exit(main())
 except (OSError,ValueError,KeyError) as e:print(str(e),file=sys.stderr);sys.exit(1)
