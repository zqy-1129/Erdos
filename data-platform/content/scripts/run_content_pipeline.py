"""Versioned local pipeline. check is read-only; build/update create new snapshots."""
import argparse,subprocess,sys
from pathlib import Path
import content_runtime as rt
from consumer_v2 import DEFAULT_RELEASE
PY=r'E:/Anaconda/envs/pytorch/python.exe'
def run(script,*args):
 print('Running '+script,flush=True);r=subprocess.run([PY,script]+list(args),cwd=str(rt.CONTENT))
 if r.returncode:raise ValueError('STEP_FAILED: '+script+' ('+str(r.returncode)+')')
def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='cmd',required=True)
 for cmd in ['build','update']:
  q=sub.add_parser(cmd);q.add_argument('--snapshot-id',required=True);q.add_argument('--version',type=int,required=True);q.add_argument('--data-root',default='D:/Erdos_data');q.add_argument('--previous');q.add_argument('--stage05',default='normalized/stage05_codex_v3');q.add_argument('--stage04',default='normalized/stage04/cumcm_codex_v4');q.add_argument('--resume',action='store_true');q.add_argument('--inventory')
 q=sub.add_parser('check');q.add_argument('--release',default=DEFAULT_RELEASE)
 q=sub.add_parser('demo');q.add_argument('--release',default=DEFAULT_RELEASE);q.add_argument('--run-id');q.add_argument('--out',default='out/demos')
 a=p.parse_args(argv)
 if a.cmd=='check':run('scripts/check_content_release.py','--release',a.release);return 0
 if a.cmd=='demo':
  extra=['--run-id',a.run_id] if a.run_id else [];run('scripts/run_demo.py','--release',a.release,'--out',a.out,*extra);return 0
 rt.identifier(a.snapshot_id)
 if a.version<1:raise ValueError('VERSION')
 if a.cmd=='update' and not a.previous:raise ValueError('UPDATE_REQUIRES_PREVIOUS_SNAPSHOT')
 inv=a.inventory or 'out/inventory/'+a.snapshot_id;aggregate='normalized/batches/'+a.snapshot_id;release='out/releases/'+a.snapshot_id
 for path in [aggregate,release]:
  rt.disjoint_sources(a.data_root,path)
  if (Path(path)/'integrity.json').exists():raise ValueError('SEALED_VERSION_EXISTS: choose a new snapshot-id')
 if not a.inventory:
  if (Path(inv)/'source_files.jsonl').exists() and not a.resume:raise ValueError('INVENTORY_EXISTS: use new id or explicit --resume')
  if not (a.resume and (Path(inv)/'source_files.jsonl').exists()):run('scripts/inventory_competitions.py','--data-root',a.data_root,'--out',inv,'--hash-mode','full')
 if not (Path(a.stage05)/'integrity.json').exists():run('scripts/stage05_template_families.py','--data-root',a.data_root,'--out',a.stage05)
 extra=['--previous',a.previous] if a.previous else []
 if a.resume:extra+=['--resume']
 run('scripts/batch_content_v2.py','--data-root',a.data_root,'--inventory',inv,'--out',aggregate,'--stage04',a.stage04,*extra)
 run('scripts/build_content_release.py','--release-id',a.snapshot_id,'--version',str(a.version),'--aggregate',aggregate,'--stage05',a.stage05,'--stage04',a.stage04,'--data-root',a.data_root)
 run('scripts/check_content_release.py','--release',release);return 0
if __name__=='__main__':
 try:sys.exit(main())
 except (OSError,ValueError,KeyError) as e:print(str(e),file=sys.stderr);sys.exit(1)
