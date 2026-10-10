"""Record only observations actually inspected against the rendered source."""
from . import core,audit,local_closure,business_contracts as bc

def record(review_id,sha,page,finding,ground_truth,status='source_checked'):
    rd=audit.run_dir(local_closure.args());value=core.load_json(rd/'parsed'/sha/('page-%04d.json'%page));evidence=value['full_page_evidence']
    path=core.safe_relpath(core.CONTENT_DIR,evidence['path'])
    if core.sha256_of(path)!=evidence['asset_ref']['sha256']:raise ValueError('source inspection image drift')
    row={'review_id':review_id,'kind':'source_visual_inspection','source_sha256':sha,'page':page,
        'input_sha256':evidence['asset_ref']['sha256'],'reviewer_type':'codex_source_visual_review','status':status,
        'finding':finding,'ground_truth':ground_truth,'model_revision':None}
    bc.validate('review',row)
    out=rd/'quality/source_visual_reviews.jsonl';rows=core.load_jsonl(out) if out.exists() else []
    rows=[r for r in rows if r['review_id']!=review_id]+[row];core.write_jsonl(out,rows)
    return row

if __name__=='__main__':
    record('source-font-map-ct-2017-title','00474738e7498bbb507905b2d88aabad4c55103aafdf88fc6635af043f5a3f88',1,
        '已直接检查整页渲染图：标题为 CT系统标定与图像重建；正文清楚显示 CT、X光、Radon。原提取的畃畔、畘、畒畡畤畯畮来自错误字体映射；U+7500 回退在这些可见词上得到原图正确值。模型把总述段编号为问题1，其问题分项输出不可作为独立标注。',
        {'title':'CT系统标定与图像重建','visible_terms':['CT','X光','Radon'],'subproblem_groups':['问题一','问题二、三','问题四'],
         'scope':'this page and these visible terms only; not all formulas or all pages certified'})
