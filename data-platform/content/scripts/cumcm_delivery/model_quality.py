"""Reject nonanswers, keep source observations separate, never invent accuracy gold."""
import collections,re
from . import core,audit,local_closure,source_review

def assess():
    rd=audit.run_dir(local_closure.args());rows=[];counts=collections.Counter();by_kind=collections.defaultdict(collections.Counter)
    for path in sorted((rd/'quality/local_model_reviews').glob('*.json')):
        r=core.load_json(path);image=core.safe_relpath(core.CONTENT_DIR,r['input_path'])
        if core.sha256_of(image)!=r['input_sha256']:raise ValueError('model review input bytes drift')
        finding=r['finding'].strip();reason=None
        if re.fullmatch(r'[\d\s(),.\[\]，（）]+',finding):reason='coordinates_or_numbers_are_not_requested_transcription'
        elif not finding or '模型未输出' in finding:reason='model_abstained'
        elif r['inference']['generated_tokens'] >= (384 if r['kind'] in ('paragraph','formula') else 160):reason='generation_reached_limit'
        elif r['kind']=='formula':reason='general_vlm_formula_not_approved; use_exact_crop_and_dedicated_formula_API'
        elif r['kind']=='structure' and finding=='无标题':reason='absence_claim_not_independently_verified'
        status='rejected' if reason and reason.startswith('coordinates') else 'abstained' if reason else 'candidate'
        rows.append({'review_id':r['review_id'],'kind':r['kind'],'source_sha256':r['source_sha256'],'page':r['page'],
            'input_sha256':r['input_sha256'],'status':status,'reason':reason,'requires_source_inspection':True,
            'approved_for_template_semantics':False,'ground_truth':None})
        counts[status]+=1;by_kind[r['kind']][status]+=1
    core.write_jsonl(rd/'quality/model_review_disposition.jsonl',rows)
    progress=core.load_json(rd/'quality/local_model_review_progress.json')
    source=core.load_jsonl(rd/'quality/source_visual_reviews.jsonl')
    report={'model_observations':len(rows),'status_counts':dict(counts),'by_kind':{k:dict(v) for k,v in by_kind.items()},
        'queue_progress':progress,'independent_source_inspections':len(source),
        'ground_truth_accuracy_claimed':False,'cer':None,'formula_accuracy':None,'chart_table_accuracy':None,
        'policy':'All model observations are nonauthoritative. Rejected outputs cannot promote extraction or recipe quality. Full source pages remain the fallback.',
        'local_usage_authorized':True,'third_party_raw_upload_authorized':False}
    core.write_json(rd/'quality/model_quality_assessment.json',report)
    local_closure.state('safe_semantic_quality_gate','pass',['quality/model_quality_assessment.json','quality/model_review_disposition.jsonl'],
        {'scope':'nonanswer rejection and independent evidence separation; not complete human gold or semantic accuracy certification'})
    print(dict(counts))

def record_seen_traffic_nonanswer():
    source_review.record('traffic-p6-vlm-coordinate-rejection','7ec9ec0c162ddb121738dbb4af62ad3de358c21806da4b223947dd6b7ba728bb',6,
        '已直接查看该正文段落的截图，含图5-3非工作日不同时段的车流量、图5-4节假日不同时段的车流量及聚类讨论。视觉模型只输出坐标，不能作为正文转写或图表语义标签；其结果拒用。',
        {'model_review_id':'paragraph:unit-d658c7d360064b1932b93975a293c055a82546367fa85942f6cfa274dfcb7e5f',
         'crop_sha256':'a832efaf851454781fa52da9cb71705f98c8b324dcb1cba82be3524087e73ba3',
         'visible_captions':['图5-3 非工作日不同时段的车流量','图5-4 节假日不同时段的车流量'],
         'scope':'reject coordinate-only answer; not transcription of every glyph or chart data reconstruction'})

if __name__=='__main__':record_seen_traffic_nonanswer();assess()
