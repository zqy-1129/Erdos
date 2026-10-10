"""Self-contained, immutable staging release builder; hashes cover metadata and resources."""
import argparse,sys
from pathlib import Path
import content_runtime as rt
from stage03_fileio import relative,copy_verified,no_links
FAMILY_ROLES={'ruleset','template_outline','writing_policy','figure_style','table_style','template_layout'}
def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__)
 for k,v in [('release-id','content-candidate-codex-v3'),('stage05','normalized/stage05_codex_v3'),('stage04','normalized/stage04/cumcm_codex_v4'),('out','out/releases'),('data-root','D:/Erdos_data')]:p.add_argument('--'+k,default=v)
 p.add_argument('--aggregate');p.add_argument('--version',type=int,default=3);a=p.parse_args(argv);rt.identifier(a.release_id)
 if a.version<1:raise ValueError('VERSION')
 s5=no_links(a.stage05);s4=no_links(a.stage04);root=relative(a.out,a.release_id)
 rt.disjoint_sources(a.data_root,root);rt.disjoint_sources(s5,root);rt.disjoint_sources(s4,root)
 rt.check_snapshot(s5);rt.check_snapshot(s4);b5=rt.load(s5/'bundle.json');b4=rt.load(s4/'bundle.json');rt.validate(b5);rt.validate(b4)
 sources={x['asset_id']:x for x in rt.load(s4/'resource_bindings.json')};agg=None
 if a.aggregate:
  agg=no_links(a.aggregate);rt.check_snapshot(agg);bx=rt.load(agg/'bundle.json');rt.validate(bx)
  for key,idkey in [('problems','problem_id'),('cases','case_id'),('assets','asset_id')]:
   records={x[idkey]:x for x in bx.get(key,[])};records.update({x[idkey]:x for x in b4.get(key,[])});b4[key]=list(records.values())
  sources.update({x['asset_id']:x for x in rt.load(agg/'resource_bindings.json')});sources.update({x['asset_id']:x for x in rt.load(s4/'resource_bindings.json')})
 registry=rt.load(rt.CONTENT/'config/competition_registry.json');oldp={r['business_id']:r for r in rt.load(rt.CONTENT/'out/pack/problems.json')};oldc={r['business_id']:r for r in rt.load(rt.CONTENT/'out/pack/cases.json')};catalog=[]
 for comp in registry['competitions']:
  cid=comp['competition_id'];cr=relative(root,'competitions/'+cid)
  bundle={k:[r for r in b5.get(k,[]) if r.get('competition_id')==cid] for k in ['families','rulesets','outlines','figure_styles','table_styles','writing_profiles','assets']}
  for k in ['problems','cases']:bundle[k]=[r for r in b4.get(k,[]) if r.get('competition_id')==cid]
  bundle['assets']+=[r for r in b4.get('assets',[]) if r.get('competition_id')==cid];family=bundle['families'][0];assets=sorted(bundle['assets'],key=lambda x:x['asset_id']);bindings={}
  for asset in assets:
   aid=asset['asset_id']
   if asset['role'] in FAMILY_ROLES:src=relative(s5,'competitions/'+cid+'/template_family/'+asset['oss_key'])
   else:
    b=sources[aid];base={'stage03':rt.CONTENT/'normalized/stage03/cumcm','stage04':s4,'data':no_links(a.data_root),'aggregate':agg}[b.get('root_kind','stage03')];src=relative(base,b['local_rel'])
   rt.checked(src,asset['sha256'],asset['size_bytes']);rel='files/'+aid+'/v'+str(asset['version'])+'/'+Path(asset['oss_key']).name
   result=copy_verified(src,relative(cr,rel))
   if result['conflict']:raise ValueError('COPY_CONFLICT: '+str(result))
   bindings[aid]=rel
  manifest={'release_id':a.release_id+'-'+cid,'version':a.version,'competition_id':cid,'schema_version':1,'published_status':'staging','items':[dict(asset_id=x['asset_id'],version=x['version'],sha256=x['sha256'],role=x['role'],change='add') for x in assets],'dependencies':[],'removed':[]}
  bundle.update(assets=assets,releases=[manifest],is_fixture=False);rt.validate(bundle);amap={x['asset_id']:x for x in assets}
  start={k:bindings[family[k+'_ref']['asset_id']] for k in ['ruleset','base_outline','writing_policy','figure_style','table_style']};start['layouts']={x['format']:bindings[x['asset_ref']['asset_id']] for x in family['layout_refs']}
  fi=dict(competition_id=cid,family_id=family['family_id'],family_version=family['family_version'],language=family['language'],ruleset_id=family['ruleset_id'],ruleset_version=family['ruleset_version'],ruleset_status=family['ruleset_status'],formats=list(start['layouts']),rules_compliance='unverified',consumable_status='draft',start_resources=start)
  refs=[];pmap={x['problem_id']:x for x in bundle['problems']}
  for c in bundle['cases']:
   parent=pmap.get(c['problem_id']);profile=next((x for x in assets if x['role']=='case_profile' and x['owner_id']==c['case_id']),None)
   if parent is None or profile is None:continue
   tags=list(parent['problem_types'])
   for sub in parent['subproblems']:
    for k in ['problem_types','method_tags','data_tags','domain_tags']:tags+=sub.get(k,[])
   refs.append(dict(case_id=c['case_id'],problem_id=c['problem_id'],profile_version=c['profile_version'],source_sha256=c['source_sha256'],profile_asset_ref={k:profile[k] for k in ['asset_id','version','sha256']},competition_id=cid,tags=sorted(set(tags)),resolution_status=c['resolution_status'],review_status=c['review_status'],quality='reviewed' if c['review_status']=='approved' else 'pending_review',license=c['license']))
  templates=[]
  for layout in family['layout_refs']:
   asset=amap[layout['asset_ref']['asset_id']];templates.append(dict(business_id=cid+'-'+layout['format']+'-family',competition=cid,format=layout['format'],oss_key=bindings[asset['asset_id']],sha256=asset['sha256'],version=asset['version'],changelog='Local staging draft; rules unverified',tier='free'))
  problems=[];cases=[];seedproof=[]
  for entity,idkey,legacy,target in [(bundle['problems'],'problem_id',oldp,problems),(bundle['cases'],'case_id',oldc,cases)]:
   for row in entity:
    bid=row[idkey]
    if bid not in legacy:continue
    if idkey=='case_id' and (row['resolution_status']!='resolved' or row['problem_id'] not in oldp):
     seedproof.append(dict(business_id=bid,source_sha256=row['source_sha256'],legacy_seed_status='omitted_unresolved_or_parent_not_in_legacy_seed'));continue
    rec=rt.parse(rt.json_bytes(legacy[bid]).decode('utf8'))
    if idkey=='problem_id':
     for att in rec['attachments']:
      matching=next((x for x in assets if x['role']=='attachment' and x['owner_id']==bid and x['oss_key']==att['oss_key']),None)
      if matching is None:raise ValueError('SEED_ATTACHMENT_BINDING: '+bid)
      att['oss_key']=bindings[matching['asset_id']]
    else:
     asset=next(x for x in assets if x['role']=='case_paper' and x['owner_id']==bid);rec['oss_key']=bindings[asset['asset_id']]
    target.append(rec);seedproof.append(dict(business_id=bid,source_sha256=row['source_sha256']))
  docs={'manifest.json':manifest,'assets.json':assets,'local_bindings.json':bindings,'bundle.json':bundle,'index/family_index.json':fi,'index/reference_index.json':dict(competition_id=cid,references=sorted(refs,key=lambda x:x['case_id'])),'seed/templates.json':templates,'seed/problems.json':problems,'seed/cases.json':cases,'seed/provenance.json':seedproof}
  for rel,doc in docs.items():rt.write(relative(cr,rel),doc)
  catalog.append(dict(competition_id=cid,display_name=comp['display_name'],family_id=family['family_id'],family_version=family['family_version'],language=family['language'],ruleset_status=family['ruleset_status'],formats=fi['formats'],consumable_status='draft',problem_count=len(bundle['problems']),case_count=len(bundle['cases']),reference_count=sum(x['license']['internal_analysis'] and x['review_status']=='approved' for x in refs),pending_reference_count=len(refs)))
 rt.write(root/'catalog.json',dict(release_id=a.release_id,version=a.version,published_status='staging',schema_version=1,competitions=catalog))
 rt.seal(root,{str(x):rt.checked(x/'integrity.json')['sha256'] for x in [s4,s5]+([agg] if agg else [])},{'scope':'self-contained staging; not cloud published'});rt.check_snapshot(root)
 print('Built verified immutable candidate: '+str(root));return 0
if __name__=='__main__':
 try:sys.exit(main())
 except (OSError,ValueError,KeyError) as exc:print(str(exc),file=sys.stderr);sys.exit(1)
