"""Real calculation on explicitly synthetic QA data demonstrates DE consumption contracts."""
import json,sys,uuid
from . import core,contracts
import numpy as np

def main():
    base=core.CONTENT_DIR/'reports/trae/qa_result_flow';base.mkdir(parents=True,exist_ok=True)
    task='qa-synthetic-linear-regression'
    dataset=base/'synthetic.csv';core.write_bytes(dataset,b'x,y\n0,1\n1,3\n2,5\n3,7\n4,9\n')
    lock=base/'environment.json';core.write_json(lock,{'python':sys.version,'numpy':np.__version__,'interpreter':core.PY,'purpose':'synthetic contract integration only'})
    started=core.now_utc_iso();rows=np.genfromtxt(str(dataset),delimiter=',',skip_header=1)
    design=np.column_stack([rows[:,0],np.ones(len(rows))]);slope,intercept=np.linalg.lstsq(design,rows[:,1],rcond=None)[0]
    residual=float(np.max(np.abs(design@np.asarray([slope,intercept])-rows[:,1])))
    if residual>1e-10:raise ValueError('synthetic calculation verification failed')
    ref=lambda p:{'asset_id':'asset-'+core.sha256_of(p),'sha256':core.sha256_of(p),'version':1}
    card={'result_card_id':'qa-result-ols','task_id':task,'subproblem_id':task+':q1','status':'succeeded','input_refs':[ref(dataset)],
          'run':{'run_id':str(uuid.uuid4()),'code_sha256':core.sha256_of(__file__),'environment_lock_sha256':core.sha256_of(lock),'started_at':started,'finished_at':core.now_utc_iso()},
          'method':'ordinary least squares on synthetic QA data','outputs':[{'name':'slope','value':float(slope),'unit':'y_unit/x_unit'},{'name':'intercept','value':float(intercept),'unit':'y_unit'},{'name':'max_absolute_residual','value':residual,'unit':'y_unit'}],
          'artifact_refs':[],'validation':{'status':'passed','checks':['residual <= 1e-10','5 real input rows read from SHA-pinned synthetic CSV']},'limitations':['Synthetic integration fixture; not a solved historical or user competition problem'],'schema_version':3}
    node={'node_id':'qa-paragraph','task_id':task,'order':1,'node_type':'paragraph','subproblem_ref':task+':q1','content':{'text':'合成联调用例经真实最小二乘计算得 y = {:.6g}x + {:.6g}，最大绝对残差 {:.3g}。'.format(slope,intercept,residual)},'result_refs':[card['result_card_id']],'release_id':'cumcm-2010-2025-codex-v2','version':1}
    contracts.validate_document([node],[card])
    core.write_json(base/'result_card.json',card);core.write_json(base/'document_nodes.json',[node])
    core.write_json(base/'verification.json',{'passed':True,'mode':'real synthetic numerical computation + typed result/node validation','historical_data_used':False,'client_business_engine_implemented':False})
    print('Actual synthetic calculation and result/document contract consumption passed')

if __name__=='__main__':main()
