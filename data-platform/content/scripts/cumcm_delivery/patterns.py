"""Recognized corpus distributions stay separate from approved writing recommendations."""
import collections,statistics
from . import core
RUN=core.CONTENT_DIR/'normalized/cumcm_delivery/cumcm-2010-2025-codex-r002'

def main(run=None):
    global RUN
    if run is not None: RUN=run
    papers=core.load_json(RUN/'catalog/papers.json');problems={p['problem_id']:p for p in core.load_json(RUN/'catalog/problems.json')}
    units=core.load_jsonl(RUN/'catalog/retrieval_units.jsonl');by_case=collections.defaultdict(list)
    for unit in units:
        if unit['case_id']:by_case[unit['case_id']].append(unit)
    observations=[];groups=collections.defaultdict(list)
    for paper in papers:
        chars=collections.Counter()
        for unit in by_case[paper['case_id']]:chars[unit['stage']]+=sum('\u3400'<=c<='\u9fff' for c in unit['text'])
        row={'case_id':paper['case_id'],'source_sha256':paper['source_sha256'],'year':paper['year'],'page_count':paper['page_count'],
             'recognized_cjk_chars_by_candidate_stage':dict(chars),'detected_heading_count':len(paper['structure']),
             'figure_caption_candidates':len(paper['figure_captions']),'table_caption_candidates':len(paper['table_captions']),'formula_line_candidates':len(paper['formula_candidates']),
             'basis':'recognized whole-source lines, stage heuristics; includes appendices/references/misrecognized text; not true disjoint body length','review_status':'pending_review'}
        observations.append(row)
        types=problems.get(paper['problem_id'],{}).get('problem_types',['unknown'])
        for kind in types:groups[kind].append(row)
    distributions=[]
    for kind,rows in groups.items():
        stages={stage for r in rows for stage in r['recognized_cjk_chars_by_candidate_stage']}
        distributions.append({'problem_type_candidate':kind,'sample_count':len(rows),'evidence_case_ids':[r['case_id'] for r in rows],
          'stage_recognized_cjk_medians':{stage:statistics.median(r['recognized_cjk_chars_by_candidate_stage'].get(stage,0) for r in rows) for stage in stages},
          'page_count_median':statistics.median(r['page_count'] for r in rows),'not_a_recommended_body_length':True})
    core.write_json(RUN/'catalog/writing_patterns.json',{'observations':observations,'distributions':distributions,'external_consumer_allowed':False,'source_text_is_instruction':False,'use_after':'review stage boundaries and actual body lengths before making content templates'})
    print('actual recognized writing-pattern distributions: {} papers; quality still pending'.format(len(papers)))

if __name__=='__main__':main()
