"""Checks real semantic embedding, complete context coverage and release-space pin rejection."""
import copy,time
import numpy as np
from . import core,services

def main():
    prefix='建立数学模型并定义变量和约束。'*50
    a=prefix+'最后一部分研究车辆路径和最优调度方案。'*20
    b=prefix+'最后一部分研究太阳辐射的物理机理和温度微分方程。'*20
    start=time.monotonic();vectors=np.asarray(services.embed([a,b,a]))
    distance=float(1-np.dot(vectors[0],vectors[1]));repeat=float(np.max(np.abs(vectors[0]-vectors[2])))
    original=services.model_manifest();wrong=copy.deepcopy(original);wrong['normalization']='none'
    try:services.verify_model_pin(wrong);rejected=False
    except ValueError:rejected=True
    report={'model_manifest':original,'provider':services.model()[0].get_providers(),'full_input_tokens':services.untruncated_tokens(a),'late_context_cosine_distance':distance,'repeat_max_difference':repeat,'norms':np.linalg.norm(vectors,axis=1).tolist(),'mismatched_pooling_pin_rejected':rejected,'elapsed_seconds':time.monotonic()-start,'passed':distance>1e-5 and repeat<1e-5 and rejected and services.untruncated_tokens(a)>128}
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_embedding_quality.json',report)
    if not report['passed']:raise ValueError('real full-context embedding checks failed')
    print('actual semantic vectors cover text beyond the first 128 tokens; model-space mismatch rejected')

if __name__=='__main__':main()
