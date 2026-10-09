"""Small source-inspected topic probes; explicitly not full independent human gold."""
import math
from . import core,audit,local_closure,consumer

QUERIES=[
 ('ct-calibration','analysis','CT 设备投影采样的几何误差应先怎样标定，再开展图像重建？','cumcm-2017-A'),
 ('ct-projection','model','CT 成像中把椭圆内部介质沿射线累积成投影，用弦长解释线积分模型。','cumcm-2017-A'),
 ('ct-workflow','algorithm','从 CT 探测器的投影记录开展参数标定和重建运算，说明实际算法步骤。','cumcm-2017-A'),
 ('traffic-day-groups','analysis','比较工作日、非工作日和节假日各时段的交通车流，如何组织分类分析？','cumcm-2024-E'),
 ('traffic-clustering','model','用 K-means 聚类描述不同时段的车流量模式，区分非工作日和节假日。','cumcm-2024-E'),
 ('traffic-controls','algorithm','分析路口车流量并按时间模式聚类，为信号灯交通管控提供依据。','cumcm-2024-E')]

def prepare():
    rd=audit.run_dir(local_closure.args());rows=[{'query_id':qid,'stage':stage,'query':query,'expected_problem_id':pid,
        'label_basis':'source visually inspected CT title/task page and traffic day-group/caption paragraph; topic only',
        'independent_human_gold':False} for qid,stage,query,pid in QUERIES]
    path=rd/'quality/source_topic_queries.json';core.write_json(path,rows)

def evaluate():
    rd=audit.run_dir(local_closure.args());path=rd/'quality/source_topic_queries.json';rows=core.load_json(path);results=[]
    for row in rows:
        req={'competition_id':'cumcm','release_id':local_closure.config()['release_id'],'stage':row['stage'],
            'subproblem':{'goal':row['query'],'problem_types':[]},'usage_purpose':'team_internal','top_k':5,'token_budget':6000}
        capsule=consumer.retrieve(req,'team_internal');refs=capsule['historical_references']
        gains=[int(r.get('problem_id')==row['expected_problem_id']) for r in refs]
        results.append({'query_id':row['query_id'],'hit_at_5':any(gains),'returned_problem_ids':[r.get('problem_id') for r in refs],
            'actual_token_budget':capsule['budget']['actual'],'source_pins_present':all(r['source_ref']['sha256']==r['source_sha256'] for r in refs)})
    report={'query_file_sha256':core.sha256_of(path),'queries':results,'source_topic_hit_at_5':sum(r['hit_at_5'] for r in results)/len(results),
        'scope':'six fresh paraphrased topic fixtures over two inspected topics, full historical year range',
        'independent_human_gold':False,'full_corpus_recall_at_5':None,'full_corpus_ndcg_at_5':None,
        'index_text_used_as_query':False,'general_quality_claimed':False}
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_local_topic_retrieval.json',report);print(report)

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','evaluate']);ns=p.parse_args();prepare() if ns.command=='prepare' else evaluate()
