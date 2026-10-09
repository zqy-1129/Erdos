"""Read-only checks of all competition bundles, resources and consumer startup paths."""
import argparse,sys
import content_runtime as rt
from consumer_v2 import ReleaseStore,startup,DEFAULT_RELEASE
def check(release):
 proof=rt.check_snapshot(release);store=ReleaseStore(release);details=[]
 legacy_p=rt.load(rt.CONTENT/'out/pack/problems.json');legacy_c=rt.load(rt.CONTENT/'out/pack/cases.json')
 pkeys=set(legacy_p[0]);ckeys=set(legacy_c[0]);tkeys={'business_id','competition','format','oss_key','sha256','version','changelog','tier'}
 for comp in store.catalog['competitions']:
  cid=comp['competition_id'];store._load(cid);bundle=store._bundles[cid]
  for aid in store._assets[cid]:store.read(cid,aid)
  for fmt in store.family_index(cid)['formats']:startup(store,cid,fmt,'local_only')
  prefix='competitions/'+cid+'/'
  seedparents={r['business_id'] for r in store.doc(prefix+'seed/problems.json')}
  for name,keys in [('problems',pkeys),('cases',ckeys),('templates',tkeys)]:
   for row in store.doc(prefix+'seed/'+name+'.json'):
    if set(row)!=keys:raise ValueError('SEED_KEY_CONTRACT: '+cid+'/'+name)
    if name=='cases' and row['problem_id'] not in seedparents:raise ValueError('SEED_PARENT_MISSING')
    paths=[row['oss_key']] if name!='problems' else [x['oss_key'] for x in row['attachments']]
    for path in paths:
     if path not in store._bindings[cid].values():raise ValueError('SEED_LOCAL_BINDING')
  details.append(dict(competition_id=cid,formats=store.family_index(cid)['formats'],assets=len(bundle['assets']),problems=len(bundle['problems']),cases=len(bundle['cases']),ruleset_status=comp['ruleset_status'],reference_count=comp['reference_count']))
 return dict(passed=True,read_only=True,release_id=store.catalog['release_id'],integrity_sha256=rt.checked(store.root/'integrity.json')['sha256'],tracked_files=len(proof['outputs']),competitions=details)
def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release',default=DEFAULT_RELEASE);a=p.parse_args(argv);print(rt.json_bytes(check(a.release)).decode('utf8'));return 0
if __name__=='__main__':
 try:sys.exit(main())
 except (OSError,ValueError,KeyError) as e:print(str(e),file=sys.stderr);sys.exit(1)
