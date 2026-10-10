"""Source occurrence changes and content identity; changed bytes never inherit review approval."""
from . import core,audit

def diff(previous,current):
    before={r['relative_path']:r for r in previous if r['in_scope']}
    after={r['relative_path']:r for r in current if r['in_scope']}
    added=[after[p] for p in sorted(after.keys()-before.keys())]
    removed=[before[p] for p in sorted(before.keys()-after.keys())]
    changed=[{'path':p,'before_sha256':before[p]['sha256'],'after_sha256':after[p]['sha256'],'review_status':'pending_review'} for p in sorted(before.keys()&after.keys()) if before[p]['sha256']!=after[p]['sha256']]
    renames=[]
    for old in removed:
        candidates=[r for r in added if r['sha256']==old['sha256'] and r['root_kind']==old['root_kind'] and r['year']==old['year']]
        origins=[r for r in removed if r['sha256']==old['sha256'] and r['root_kind']==old['root_kind'] and r['year']==old['year']]
        if len(candidates)==len(origins)==1:
            renames.append({'before_path':old['relative_path'],'after_path':candidates[0]['relative_path'],'content_id':'sha256:'+old['sha256'],'content_identity_unchanged':True})
    return {'added_paths':[r['relative_path'] for r in added],'removed_paths':[r['relative_path'] for r in removed],'changed':changed,'renames':renames,
            'review_invalidated_content_sha256':sorted({r['before_sha256'] for r in changed}|{r['sha256'] for r in removed if not any(v['before_path']==r['relative_path'] for v in renames)}),
            'unchanged_count':sum(before[p]['sha256']==after[p]['sha256'] for p in before.keys()&after.keys()),
            'entity_policy':'problem identity is competition/year/code; paper and asset identity are content SHA; changed paper bytes create a new candidate instead of inheriting approval'}

def record(args):
    previous=core.safe_relpath(core.CONTENT_DIR,'out/cumcm_delivery/releases/'+args.previous)
    current=audit.run_dir(args)
    values=diff(core.load_jsonl(previous/'metadata/sources/source_files.jsonl'),core.load_jsonl(current/'sources/source_files.jsonl'))
    core.write_json(current/'quality/source_changes.json',dict(values,previous_release_id=args.previous,new_release_id=args.release_id))
