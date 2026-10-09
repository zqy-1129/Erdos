"""Source inspections recorded only after the operator actually viewed the evidence."""
from . import core,audit,local_closure,source_review

def record_seen_ct_formulas():
    sha='00474738e7498bbb507905b2d88aabad4c55103aafdf88fc6635af043f5a3f88'
    rd=audit.run_dir(local_closure.args())
    page=core.load_json(rd/'parsed'/sha/'formula-page-0009.json')
    seen={'b440e266d6359c081c19d4569b1585584d12a006f94464a73948104a83bdc9ec':
        r'D=\frac{2\sqrt{m^2a^2+b^2}}{\sqrt{m^2+1}}',
        '925c239bc8aced9bf9861253dc6ebddf62715b82f4faed17895f83ffc3470198':
        r'\theta=\operatorname{arccot}\sqrt{\frac{4a^2-d^2}{d^2-4b^2}}'}
    for f in page['formulas']:
        ref=f['crop_ref']
        if not ref or ref['sha256'] not in seen:continue
        path=core.safe_relpath(core.CONTENT_DIR,f['crop_path'])
        if core.sha256_of(path)!=ref['sha256']:raise ValueError('inspected crop drift')
        source_review.record('ct-2017-p9-'+ref['sha256'][:16],sha,9,
            '已直接查看此独立公式区域的渲染图，逐项核对字母、上下标、根式与分子分母。仅确认这一个公式的可见转写；不确认推导正确性、不替代新题计算。',
            {'formula_region_id':f['formula_region_id'],'crop_ref':ref,'latex':seen[ref['sha256']],
             'scope':'visible transcription only; no mathematical derivation or current result endorsement'})

def apply_review_overlay():
    rd=audit.run_dir(local_closure.args());reviews=core.load_jsonl(rd/'quality/source_visual_reviews.jsonl')
    verified={r['ground_truth']['formula_region_id']:r for r in reviews
              if isinstance(r['ground_truth'],dict) and 'formula_region_id' in r['ground_truth'] and r['status']=='source_checked'}
    rows=core.load_jsonl(rd/'catalog/formula_regions.jsonl')
    for f in rows:
        review=verified.get(f['formula_region_id'])
        if not review:continue
        truth=review['ground_truth']
        if f['crop_ref']!=truth['crop_ref'] or f['source_sha256']!=review['source_sha256'] or f['page']!=review['page']:
            raise ValueError('formula inspection evidence mismatch')
        f.update(semantics_verified=False,transcription_verified=True,semantic_status='source_checked',transcription_status='source_checked',
                 source_review_id=review['review_id'],source_checked_latex=truth['latex'])
    core.write_jsonl(rd/'catalog/formula_regions.jsonl',rows)
    for f in rows:
        if f['formula_region_id'] not in verified:continue
        path=rd/'parsed'/f['source_sha256']/('formula-page-%04d.json'%f['page']);value=core.load_json(path)
        value['formulas']=[f if old['formula_region_id']==f['formula_region_id'] else old for old in value['formulas']]
        core.write_json(path,value)

if __name__=='__main__':record_seen_ct_formulas()
