"""Restore exact offline workspace dependencies from the known local conda cache.

No installation in the user's Python environment. Existing mismatched files are never overwritten.
"""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

ROOT=Path(os.path.abspath(__file__)).parents[1]
CACHE_PACKAGES={'jsonschema':'jsonschema-4.16.0-py39haa95532_0',
                'pyrsistent':'pyrsistent-0.18.0-py39h196d8e1_0',
                'attrs':'attrs-21.4.0-pyhd3eb1b0_0'}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--conda-cache',type=Path,default=Path('E:/Anaconda/pkgs'))
    parser.add_argument('--target',type=Path,default=ROOT/'.deps/schema_validation')
    parser.add_argument('--verify-only',action='store_true')
    args=parser.parse_args()
    base=os.path.abspath(str(ROOT/'.deps'));target=os.path.abspath(str(args.target))
    if os.path.commonpath([base,target])!=base: parser.error('target must remain in content/.deps')
    lock=json.loads((ROOT/'dependencies.lock.json').read_text(encoding='utf-8'))
    plan=[]
    try:
        for rel,digest in lock['files'].items():
            dest=Path(target)/rel
            if dest.exists():
                if hashlib.sha256(dest.read_bytes()).hexdigest()!=digest:
                    raise ValueError('existing dependency differs: '+str(dest))
                continue
            if args.verify_only: raise ValueError('missing dependency: '+str(dest))
            top=rel.split('/')[0]
            if top.startswith(('attr','attrs')): package='attrs'
            elif top.startswith('jsonschema'): package='jsonschema'
            elif top.startswith('importlib_resources'): package='importlib-resources'
            elif top.startswith('zipp'): package='zipp'
            else: package='pyrsistent'
            if package in CACHE_PACKAGES:
                site = 'site-packages' if package=='attrs' else 'Lib/site-packages'
                source=args.conda_cache/CACHE_PACKAGES[package]/site/rel
            else:
                source=Path(lock['source_sites'][package])/rel
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=digest:
                raise ValueError('offline source missing/hash mismatch: '+str(source))
            plan.append((source,dest))
        # Verify the entire plan before writing any file.
        for source,dest in plan:
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(str(source),str(dest))
    except (OSError,ValueError) as exc:
        print('[DEPENDENCY-FAIL] '+str(exc));return 2
    print('Verified {} locked files; restored {} files.'.format(len(lock['files']),len(plan)))
    return 0

if __name__=='__main__': raise SystemExit(main())
