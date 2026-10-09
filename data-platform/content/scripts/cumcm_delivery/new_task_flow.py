"""Executed new-input reference client: ingestion -> real computation -> local writing -> PDF.

This bounded reference solves a declared calibration/validation/decision fixture, not
arbitrary competition tasks. It demonstrates the DE contracts without faking a product
client integration or treating historical numbers as new computation.
"""
import asyncio,collections,csv,io,json,math,os,re,subprocess,sys,uuid,zipfile
from pathlib import Path
from . import core,audit,services,consumer,contracts,local_closure,local_model_api

TASK='local-new-calibration-v1';OWNER='team-local-acceptance'
STAGES=['analysis','assumptions','symbols','model','algorithm','results','validation','sensitivity','discussion','abstract']
WRITER_PROMPT_VERSION='structure-only-v2'
TITLE='传感器标定、误差验证与阈值决策的本地闭环验证'
PROBLEM='''本地新题验收用例（自行编写的测试题，非国赛题或客户真实业务）。
一套传感器给出标定样本，x为仪器示值（无量纲），y为参考温度（摄氏度）。
问题一：用最小二乘拟合线性标定模型，给出参数、预测值和残差。
问题二：进行留一交叉验证与固定种子的自助重采样，评估泛化误差和参数区间，注明小样本限制。
问题三：当真实温度警戒值为25摄氏度时，计算示值阈值并分析警戒值上下变动两摄氏度的敏感性。
仅使用本次数据和本次计算输出写作；历史论文只提供行文次序参考。'''
CSV='x,y\n0,1.2\n1,2.8\n2,5.1\n3,6.9\n4,9.3\n5,10.8\n6,13.2\n7,14.9\n8,17.1\n9,18.7\n10,21.3\n11,22.9\n'

def root():return core.CONTENT_DIR/'reports/trae/local_new_task_flow'
def ref(path):
    digest=core.sha256_of(path);return {'asset_id':'asset-'+digest,'version':1,'sha256':digest}
def calculate():
    audit.deps();from validate_content_contracts import load_jsonschema;load_jsonschema()
    import numpy as np
    out=root();out.mkdir(parents=True,exist_ok=True);core.write_bytes(out/'problem.txt',PROBLEM.encode('utf-8'));core.write_bytes(out/'input.csv',CSV.encode('utf-8'))
    rows=np.genfromtxt(str(out/'input.csv'),delimiter=',',skip_header=1);x,y=rows.T
    if rows.shape!=(12,2) or not np.isfinite(rows).all() or len(np.unique(x))<2:raise ValueError('invalid calibration input')
    from . import calibration_solver
    solver=Path(calibration_solver.__file__);core.write_bytes(out/'calibration_solver.py',solver.read_bytes())
    lock={'interpreter':core.PY,'python':sys.version,'numpy':np.__version__,'bootstrap_seed':20251007,'bootstrap_repeats':500,
        'solver_sha256':core.sha256_of(solver),'orchestration_sha256':core.sha256_of(__file__)}
    core.write_json(out/'environment.json',lock);started=core.now_utc_iso();solved=calibration_solver.solve(rows)
    core.write_json(out/'solver-results.json',solved)
    slope,intercept,rmse,r2,loo_rmse=[solved[k] for k in ('slope','intercept','rmse','r2','loo_rmse')]
    pred=np.asarray(solved['pred']);residual=np.asarray(solved['residual']);loo=solved['loo']
    ci=np.asarray(solved['ci']);thresholds=np.asarray(solved['thresholds'])
    checks=['finite parameters and predictions','normal-equation residual verified','bootstrap fixed seed and 500 actual samples','LOOCV refits without held-out row']
    normal_error=solved['normal_error']
    if not np.isfinite([slope,intercept,rmse,r2,loo_rmse]).all() or normal_error>1e-8:raise ValueError('computation checks failed')
    outputs=[{'name':n,'value':v,'unit':u} for n,v,u in [('slope',float(slope),'degC/x_unit'),('intercept',float(intercept),'degC'),
        ('training_rmse',rmse,'degC'),('r_squared',r2,'dimensionless'),('loo_rmse',loo_rmse,'degC'),
        ('slope_bootstrap_95_interval',ci[:,0].tolist(),'degC/x_unit'),('intercept_bootstrap_95_interval',ci[:,1].tolist(),'degC'),
        ('warning_temperature',25.,'degC'),('warning_x_threshold',float(thresholds[1]),'x_unit'),
        ('sensitivity_temperatures',[23.,25.,27.],'degC'),('sensitivity_x_thresholds',thresholds.tolist(),'x_unit')]]
    run_id='compute-'+uuid.uuid4().hex;cards=[]
    groups={1:['slope','intercept','training_rmse','r_squared'],2:['loo_rmse','slope_bootstrap_95_interval','intercept_bootstrap_95_interval'],3:['warning_temperature','warning_x_threshold','sensitivity_temperatures','sensitivity_x_thresholds']}
    for q,names in groups.items():
        card={'result_card_id':TASK+':result'+str(q),'task_id':TASK,'subproblem_id':TASK+':q'+str(q),'status':'succeeded',
            'input_refs':[ref(out/'input.csv'),ref(out/'problem.txt')],
            'run':{'run_id':run_id,'code_sha256':core.sha256_of(solver),'environment_lock_sha256':core.sha256_of(out/'environment.json'),'started_at':started,'finished_at':core.now_utc_iso()},
            'method':{1:'ordinary least squares calibration',2:'leave-one-out validation and nonparametric bootstrap',3:'inverse linear threshold and temperature perturbation'}[q],
            'outputs':[o for o in outputs if o['name'] in names],'artifact_refs':[],'validation':{'status':'passed','checks':checks},
            'limitations':['Self-authored integration fixture, not a competition solution','Small sample does not establish future instrument reliability','Inverse threshold may extrapolate beyond observed calibration range'], 'schema_version':3}
        contracts.validate('result_card',card);cards.append(card)
    core.write_json(out/'result_cards.json',cards)
    b=io.StringIO();writer=csv.writer(b);writer.writerow(['x','observed_degC','predicted_degC','residual_degC','loo_prediction_degC'])
    writer.writerows(zip(x.tolist(),y.tolist(),pred.tolist(),residual.tolist(),loo));core.write_bytes(out/'computed.csv',b.getvalue().encode())
    import matplotlib;matplotlib.use('Agg');import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family':['Times New Roman','SimSun'],'pdf.fonttype':42,'svg.fonttype':'path'})
    plots=[]
    for name in ('calibration','residual','sensitivity'):
        fig,ax=plt.subplots(figsize=(6.3,3.6))
        if name=='calibration':ax.scatter(x,y,label='Observed');ax.plot(x,pred,label='OLS calibration');ax.set_xlabel('Instrument x (dimensionless)');ax.set_ylabel('Temperature (deg C)');ax.legend()
        elif name=='residual':ax.scatter(loo,y-np.asarray(loo));ax.axhline(0,color='gray',lw=.8);ax.set_xlabel('Held-out predicted temperature (deg C)');ax.set_ylabel('LOOCV error (deg C)')
        else:ax.plot([23,25,27],thresholds,marker='o');ax.set_xlabel('Warning temperature (deg C)');ax.set_ylabel('Instrument threshold (dimensionless)')
        ax.grid(alpha=.2);fig.tight_layout()
        for suffix in ('png','pdf','svg'):fig.savefig(str(out/(name+'.'+suffix)),dpi=300)
        plt.close(fig);plots.append(out/(name+'.png'))
    for q,name in enumerate(('calibration','residual','sensitivity')):
        cards[q]['artifact_refs']=[ref(out/(name+'.'+suffix)) for suffix in ('png','pdf','svg')]+[ref(out/'computed.csv')]
        contracts.validate('result_card',cards[q])
    core.write_json(out/'result_cards.json',cards)
    core.write_json(out/'computation_verification.json',{'status':'pass','normal_equation_max_error':normal_error,'input_sha256':core.sha256_of(out/'input.csv'),
        'actual_rows':len(rows),'result_card_count':3,'plot_count':3,'historical_numbers_used':False,'purpose':'bounded reference client integration fixture'})
    print('real new-input computation complete',flush=True);return cards,plots

def escaped_text(text):return '#text('+json.dumps(text,ensure_ascii=False)+')'

def current_numeric_summary(cards):
    v={o['name']:o['value'] for c in cards for o in c['outputs']}
    return '本次标定斜率为 {:.6g} 摄氏度/示值单位，截距为 {:.6g} 摄氏度；训练均方根误差为 {:.6g} 摄氏度，留一验证误差为 {:.6g} 摄氏度；25 摄氏度的示值阈值为 {:.6g}，超出已观测标定区间，需另行验证。'.format(v['slope'],v['intercept'],v['training_rmse'],v['loo_rmse'],v['warning_x_threshold'])

def writing_prompt(stage,q,capsule):
    # Historical facts and prose remain in the retrieval ledger, not the current-result writer.
    outlines={
        'analysis':'只分析任务依赖：先标定，再验证误差，最后用已拟合模型反求阈值。',
        'assumptions':'只写待检验的建模假设：样本范围内线性近似，观测对可作重采样单位，参考温度视为标定目标。明确这些是假设而非已证明事实。',
        'symbols':'只解释符号：x 是无量纲仪器示值，y 是摄氏温度，a 是斜率，b 是截距，预测值与残差分别表示拟合温度和观测减预测。',
        'sensitivity':'只解释温度警戒值变化时，通过线性模型反函数得到示值阈值变化；正斜率使阈值随警戒温度增加而增加。扰动计算尚未评估传感器故障或参数漂移。',
        'discussion':'只讨论限制：小样本、自编单案例、缺少独立实测样本、阈值外推，不能推出真实仪器安全性或任意竞赛求解能力。',
        'abstract':'概述已做的线性标定、逐样本留一验证、自助采样区间及逆向阈值分析，明确当前仅是小样本本地接口验收。'}
    questions={
        1:{'model':'最小二乘线性标定，以观测温度减预测温度的残差平方和为目标，斜率与截距为待估参数。',
           'algorithm':'设计矩阵包含仪器示值列与常数列，检查满秩，使用线性最小二乘数值函数求解，再逐行计算预测值、残差和均方根误差。',
           'results':'斜率为正，预测随示值增加。结果表显示斜率、截距、训练均方根误差和决定系数；拟合误差小不能替代外部预测验证。',
           'validation':'实际检查参数有限、设计矩阵满秩和设计矩阵转置乘残差接近零。正交性证明数值解满足最小二乘的一阶条件，不证明现实中没有系统误差。'},
        2:{'model':'用留一交叉验证评估样本内逐点预测误差；对观测对有放回重采样估计参数分布，采用分位数区间。',
           'algorithm':'每次移除一个样本，重新拟合其余样本并预测被移除样本。固定随机种子重采样，剔除秩不足样本，完成既定重复次数后计算参数分位数。',
           'results':'本次留一均方根误差高于训练误差，提示训练误差较乐观。区间表描述斜率与截距的自助采样变动，不是未来观测温度的预测区间。',
           'validation':'逐样本留一拟合不含被验证的样本；重采样检查满秩和有限参数。固定种子用于复现，不保证小样本区间具有真实覆盖率。'},
        3:{'model':'利用正斜率线性标定反函数，把警戒温度减截距后除以斜率，得到仪器示值阈值。',
           'algorithm':'从已完成的标定结果读取斜率和截距，先检查斜率非零；计算基准警戒温度及上下扰动温度对应的示值阈值，比较观测示值范围。',
           'results':'基准阈值超出观测标定区间，必须标记外推。温度上下扰动对应的阈值变化由结果表给出，不能直接当成已经验证的报警策略。',
           'validation':'通过反函数关系核对阈值，明确斜率非零条件和外推限制。当前没有独立阈值附近的实测数据，尚不能验证真实告警可靠性。'}}
    scope=questions[q][stage] if q else outlines[stage]
    structure=[{'stage':h['stage'],'recognized_characters':len(h['text']),
                'sentence_count':len(re.findall('[。！？]',h['text']))} for h in capsule['historical_references']]
    return '请只写一段约一百五十字的中文论文正文，不要标题、Markdown、公式或数值。当前是自编传感器标定测试，只有无量纲仪器示值和摄氏温度两列。当前这一节的唯一内容范围：'+scope+'\n历史资料仅提供章节结构和篇幅参照：'+json.dumps(structure,ensure_ascii=False)+'\n各数值由本次结果表单独插入。不要引入其他领域、其他数据、未执行的方法或历史结论。现在只输出当前这一节正文。'

def write_sections():
    out=root();cards=core.load_json(out/'result_cards.json');pinned=consumer.bootstrap(local_closure.config()['release_id'],'team_internal')
    if not pinned['team_local_retrieval_available']:raise PermissionError('local historical scope is not active')
    texts={};capsules=[];nodes=[];saved=out/'sections';saved.mkdir(parents=True,exist_ok=True)
    steps=[(stage,None) for stage in ('analysis','assumptions','symbols')]+[(stage,q) for q in (1,2,3) for stage in ('model','algorithm','results','validation')]+[(stage,None) for stage in ('sensitivity','discussion','abstract')]
    goals={1:'传感器标定，线性最小二乘拟合参数及残差',2:'留一交叉验证与自助重采样参数区间，小样本误差验证',3:'线性标定模型反函数阈值决策，警戒温度敏感性分析'}
    for idx,(stage,q) in enumerate(steps,1):
        key='q'+str(q)+'-'+stage if q else stage
        path=saved/(key+'.json')
        request={'request_id':TASK+'-'+key,'competition_id':'cumcm','release_id':pinned['release_id'],'stage':stage,
            'usage_purpose':'team_internal','subproblem':{'subproblem_id':TASK+':q'+str(q) if q else TASK,'goal':goals[q] if q else '传感器标定线性拟合，误差交叉验证和阈值敏感性分析','problem_types':['statistical_analysis']},'top_k':2,'token_budget':3000}
        capsule=consumer.retrieve(request,'team_internal');capsules.append(capsule)
        row=core.load_json(path) if path.exists() else None
        if row and row.get('writer_prompt_version')!=WRITER_PROMPT_VERSION:
            core.write_bytes(out/'draft_archive/raw-context-v1'/path.name,path.read_bytes(),immutable=True)
            row=None
        if row is None:
            sources=[h['text'] for h in capsule['historical_references']]
            prompt=writing_prompt(stage,q,capsule)
            response=local_model_api.infer(prompt,max_new_tokens=480);text=response['text'].strip()
            issues=[]
            if re.search(r'\d',text):issues.append('numeric_literals_require_result_card_review')
            if response.get('generated_tokens',0)>=480:issues.append('generation_budget_exhausted_requires_completeness_review')
            for source in sources:
                if any(text[n:n+40] in source for n in range(max(0,len(text)-39))):issues.append('historical_passage_overlap_requires_rewrite')
            row={'stage':stage,'text':text,'model_inference':response,'capsule_manifest_sha256':capsule['manifest_sha256'],
                'historical_unit_ids':[h['unit_id'] for h in capsule['historical_references']],'source_is_instruction':False,'status':'machine_draft_requires_editorial_review',
                'draft_quality_issues':issues,'writer_prompt_version':WRITER_PROMPT_VERSION,
                'historical_raw_prose_in_writer_prompt':False}
            core.write_json(path,row)
        if row['capsule_manifest_sha256']!=pinned['manifest_sha256']:raise ValueError('cached writing uses another release')
        final_text=row['text']+'\n'+current_numeric_summary(cards) if stage=='abstract' else row['text']
        texts[key]=final_text;nodes.append({'node_id':TASK+':'+key,'task_id':TASK,'order':idx,'node_type':'paragraph',
            'subproblem_ref':TASK+':q'+str(q) if q else None,'content':{'text':final_text},
            'result_refs':[cards[q-1]['result_card_id']] if q else [c['result_card_id'] for c in cards] if stage in ('sensitivity','discussion','abstract') else [],'release_id':pinned['release_id'],'version':1})
        print('local model writing '+key+' complete',flush=True)
    for idx,name in enumerate(('calibration','residual','sensitivity'),1):
        nodes.append({'node_id':TASK+':figure:'+name,'task_id':TASK,'order':idx,'node_type':'figure','content':{'asset_ref':ref(out/(name+'.png')),'caption':name+'，数据仅来自本次已完成计算。'},'result_refs':[cards[idx-1]['result_card_id']],'release_id':pinned['release_id'],'version':1})
    nodes.append({'node_id':TASK+':equation','task_id':TASK,'order':14,'node_type':'equation',
        'content':{'latex':r'y=ax+b,\quad \widehat\beta=(X^TX)^{-1}X^Ty'},'result_refs':[cards[0]['result_card_id']],
        'release_id':pinned['release_id'],'version':1})
    for q,card in enumerate(cards,1):
        table=out/('q'+str(q)+'-results.json');core.write_json(table,card['outputs'])
        nodes.append({'node_id':TASK+':table'+str(q),'task_id':TASK,'order':14+q,'node_type':'table',
            'content':{'asset_ref':ref(table),'caption':'问题'+str(q)+'的真实计算结果表'},'result_refs':[card['result_card_id']],
            'release_id':pinned['release_id'],'version':1})
    for name,text in [('restatement',PROBLEM),('references','历史结构参照来源和固定版本见 context_capsules.json；计算输入仅为本次 input.csv。'),('appendix','完整复现代码、数据和环境见随附项目及 SHA256 清单。')]:
        content={'text':text}
        if name=='appendix':content['asset_ref']=ref(out/'calibration_solver.py')
        nodes.append({'node_id':TASK+':'+name,'task_id':TASK,'order':1,'node_type':{'references':'reference','appendix':'appendix'}.get(name,'paragraph'),'content':content,'result_refs':[],
            'release_id':pinned['release_id'],'version':1})
    final_order=['abstract','restatement','analysis','assumptions','symbols','q1-model','equation','q1-algorithm','q1-results','table1','figure:calibration','q1-validation',
        'q2-model','q2-algorithm','q2-results','table2','figure:residual','q2-validation','q3-model','q3-algorithm','q3-results','table3','q3-validation',
        'sensitivity','figure:sensitivity','discussion','references','appendix']
    for node in nodes:node['order']=final_order.index(node['node_id'][len(TASK)+1:])+1
    nodes.sort(key=lambda n:n['order'])
    contracts.validate_document(nodes,cards);core.write_json(out/'document_nodes.json',nodes);core.write_json(out/'context_capsules.json',capsules)
    return texts,cards,nodes,pinned

def require_editorial_reviews(out):
    expected={'analysis','assumptions','symbols','sensitivity','discussion','abstract'}
    expected.update('q'+str(q)+'-'+stage for q in (1,2,3) for stage in ('model','algorithm','results','validation'))
    paths=list((out/'sections').glob('*.json'))
    if {p.stem for p in paths}!=expected:raise ValueError('All eighteen writing stages must be present')
    for path in paths:
        row=core.load_json(path)
        if row.get('editorial_review',{}).get('status')!='reviewed_against_current_solver':
            raise ValueError('Model drafts need actual editorial review before paper assembly: '+path.name)
        if row['editorial_review']['input_sha256']!=core.sha256_of(out/'input.csv') or row['editorial_review']['solver_sha256']!=core.sha256_of(out/'calibration_solver.py'):
            raise ValueError('Editorial review is for another computation: '+path.name)
        if row['editorial_review'].get('reviewed_text_sha256')!=core.sha256_bytes(row['text'].encode('utf-8')):
            raise ValueError('Editorial text changed after review: '+path.name)

def assemble(texts,cards,nodes,pinned):
    out=root();require_editorial_reviews(out)
    folder=out/'downloaded-format';from . import complex_formats
    asset=next(a for a in pinned['format_templates'] if a['format']=='typst')
    downloaded=asyncio.run(consumer.fetch_async(pinned['release_id'],asset['asset_id'],asset['version'],trusted_role='team_internal'))
    archive=out/'downloaded-format.zip';core.write_bytes(archive,downloaded['bytes']);complex_formats.unpack(archive,folder)
    for name in ('calibration','residual','sensitivity'):core.write_bytes(folder/(name+'.png'),(out/(name+'.png')).read_bytes())
    entry=folder/'main.typ';main=entry.read_text(encoding='utf-8').replace('#paper-title[[论文标题]]','#paper-title['+escaped_text(TITLE)+']')
    main=main.replace('#set document(title: "[论文标题]", author: ())','#set document(title: '+json.dumps(TITLE,ensure_ascii=False)+', author: ())')
    main=main.replace('#show raw: set text(size: 10pt,','#show raw: set text(size: 9pt,')
    main=main.replace('#pagebreak()\n#references-cn()', '#references-cn()')
    main=main.replace('[中文摘要内容：问题概述 + 每个子问题的方法和数值结果 + 结论]',escaped_text(texts['abstract']))
    main=main.replace('[关键词1] #h(1em) [关键词2] #h(1em) [关键词3]','标定 #h(1em) 验证 #h(1em) 敏感性分析')
    core.write_bytes(entry,main.encode('utf-8'))
    for file,stage,title in [('1_restatement','restatement','问题重述'),('2_analysis','analysis','问题分析'),('3_assumptions','assumptions','模型假设'),('4_symbols','symbols','符号与单位'),('8_sensitivity','sensitivity','敏感性分析'),('9_evaluation','discussion','讨论与边界')]:
        text=PROBLEM if stage=='restatement' else texts[stage]
        value='#import "../helpers.typ": *\n= '+title+'\n'+escaped_text(text)+'\n'
        if stage=='sensitivity':value+='\n#figure(image("../sensitivity.png", width: 95%), caption: [本次警戒温度变化对应的示值阈值])\n'
        core.write_bytes(folder/'sections'/(file+'.typ'),value.encode('utf-8'))
    content='#import "../helpers.typ": *\n'
    titles={1:'线性标定',2:'交叉验证与区间估计',3:'阈值决策'}
    for q,card in enumerate(cards,1):
        content+='\n= 问题'+str(q)+'：'+titles[q]+'\n'
        for stage,label in [('model','模型建立'),('algorithm','计算方法'),('results','计算结果'),('validation','结果验证')]:
            content+='\n== '+label+'\n'+escaped_text(texts['q'+str(q)+'-'+stage])+'\n'
            if q==1 and stage=='model':content+='\n$ y = a x + b quad hat(beta) = (X^T X)^(-1) X^T y $\n'
            if stage=='results':
                content+='\n#three-line-table([表'+str(q)+' 问题'+str(q)+'的本次计算结果], (2fr, 2fr, 1fr), ([输出], [本次真实值], [单位]), (\n'
                labels={'slope':'标定斜率','intercept':'温度截距','training_rmse':'训练均方根误差','r_squared':'决定系数','loo_rmse':'留一均方根误差','slope_bootstrap_95_interval':'斜率自助采样95%区间','intercept_bootstrap_95_interval':'截距自助采样95%区间','warning_temperature':'警戒温度','warning_x_threshold':'示值阈值','sensitivity_temperatures':'扰动警戒温度','sensitivity_x_thresholds':'扰动示值阈值'}
                units={'degC/x_unit':'摄氏度/示值单位','degC':'摄氏度','dimensionless':'无量纲','x_unit':'示值单位（无量纲）'}
                for output in card['outputs']:
                    v=output['value'];value=format(v,'.8g') if isinstance(v,(float,int)) else '['+', '.join(format(n,'.8g') for n in v)+']' if isinstance(v,list) else json.dumps(v)
                    content+=','.join('['+escaped_text(t)+']' for t in (labels.get(output['name'],output['name']),value,units.get(output['unit'],output['unit'])))+',\n'
                content+='))\n'
                if q in (1,2):
                    name='calibration' if q==1 else 'residual';content+='\n#figure(image("../'+name+'.png", width: 95%), caption: ['+escaped_text('本次计算：'+titles[q])+'])\n'
    core.write_bytes(folder/'sections/question_sections.typ',content.encode('utf-8'))
    appendix=escaped_text('本题和数据是自行编写的本地联调用例。原件只作结构参照，未采用历史计算数值。以下完整脚本及 input.csv 与本报告一起交付，运行环境与模型调用日志见附件。')+'\n'
    appendix+=escaped_text('数据 SHA256：'+core.sha256_of(out/'input.csv'))+'\n'+escaped_text('计算代码 SHA256：'+core.sha256_of(out/'calibration_solver.py'))+'\n'+escaped_text('历史发布版本：'+pinned['release_id'])+'\n'
    # Embed the actual complete reproducible reference implementation as an appendix.
    appendix+='\n'+ '#raw('+json.dumps((out/'calibration_solver.py').read_text(encoding='utf-8'),ensure_ascii=False)+', block: true, lang: "python")\n'
    core.write_bytes(folder/'sections/A_code.typ',appendix.encode('utf-8'))
    core.write_bytes(folder/'references.typ',escaped_text('本地新题及数据为自编测试用例；历史模板参照的源资产、页码和发布哈希见 context_capsules.json，不能作为当前数值来源。').encode('utf-8'))
    audit.command([audit.TYPST,'compile','--root',folder,entry,out/'paper.pdf'])
    core.write_json(out/'format_consumption.json',{'status':'pass','downloaded_from':'real local S3 through content SDK','asset_ref':asset,
        'archive_sha256':core.sha256_of(archive),'filled_dynamic_question_count':3,'shared_helpers_used':True,'full_reproducible_code_appendix':True})
    audit.deps();import profile_extraction as pe
    doc=pe.load_pdfium().PdfDocument((out/'paper.pdf').read_bytes());texts_pdf=[];pages=[]
    try:
        for page in doc:
            tp=page.get_textpage()
            try:texts_pdf.append(tp.get_text_range());pages.append(list(page.get_size()))
            finally:tp.close();page.close()
    finally:doc.close()
    visible='\n'.join(texts_pdf)
    checks={'a4':all(abs(w-595.28)<2 and abs(h-841.89)<2 for w,h in pages),'under_20mb':(out/'paper.pdf').stat().st_size<20*1024**2,
        'three_question_results_present':all('问题'+str(q) in re.sub(r'\s','',visible) for q in (1,2,3)),'abstract_first_page':'摘要' in texts_pdf[0],
        'no_toc':'目录' not in visible,'current_data_sha_in_pdf':core.sha256_of(out/'input.csv') in re.sub(r'\s','',visible)}
    if not all(checks.values()):raise ValueError('assembled reference paper verification failed: '+str(checks))
    core.write_json(out/'paper_verification.json',{'status':'pass','checks':checks,'pages':len(pages),'pdf_sha256':core.sha256_of(out/'paper.pdf'),
        'template_fonts':'actual PDF inspection required; defaults SimSun/SimHei/Times New Roman','scope':'bounded local reference workflow, not a universal solver or integrated product client'})
    return out/'paper.pdf'

async def persist(cards,nodes,pinned):
    out=root();conn=await services.pg();assets=[]
    try:
        await conn.execute((core.CONTENT_DIR/'sql/cumcm/0003_local_tasks.sql').read_text(encoding='utf-8'))
        async with conn.transaction():
            payload={'task_id':TASK,'owner_id':OWNER,'scope':'isolated new-input reference fixture','status':'completed_reference_flow','problem':PROBLEM}
            existing=await conn.fetchval('SELECT problem_sha256 FROM content_tasks_local.tasks WHERE owner_id=$1 AND task_id=$2',OWNER,TASK)
            if existing and existing!=core.sha256_of(out/'problem.txt'):raise ValueError('new input task identity drift')
            await conn.execute('INSERT INTO content_tasks_local.tasks VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT(owner_id,task_id) DO UPDATE SET payload=excluded.payload',OWNER,TASK,core.sha256_of(out/'problem.txt'),pinned['release_id'],json.dumps(payload,ensure_ascii=False))
            filenames=['problem.txt','input.csv','computed.csv','q1-results.json','q2-results.json','q3-results.json',
                'calibration_solver.py','solver-results.json','environment.json','result_cards.json','document_nodes.json',
                'context_capsules.json','format_consumption.json','paper_verification.json','paper.pdf']
            filenames.extend(name+'.'+suffix for name in ('calibration','residual','sensitivity') for suffix in ('png','pdf','svg'))
            filenames.extend(['editorial_decisions.json','editorial_review.json','computation_verification.json'])
            filenames.extend('sections/'+p.name for p in sorted((out/'sections').glob('*.json')))
            filenames.extend(name for name in ('paper_full_checks.json','paper_visual_review.json','reproduction_verification.json') if (out/name).is_file())
            for filename in filenames:
                path=out/filename;asset=ref(path);key=services.put_verified(path,asset['sha256']);payload=dict(asset,filename=filename,size=path.stat().st_size)
                await conn.execute('INSERT INTO content_tasks_local.assets VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT DO NOTHING',OWNER,TASK,asset['asset_id'],1,asset['sha256'],key,json.dumps(payload));assets.append(payload)
            for table,key,rows in [('results','result_card_id',cards),('nodes','node_id',nodes)]:
                for row in rows:await conn.execute('INSERT INTO content_tasks_local.'+table+' VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(owner_id,task_id,'+key+') DO UPDATE SET payload=excluded.payload',OWNER,TASK,row[key],json.dumps(row,ensure_ascii=False))
                stored=await conn.fetch('SELECT payload FROM content_tasks_local.'+table+' WHERE owner_id=$1 AND task_id=$2',OWNER,TASK)
                if {core.json_loads(r['payload'])[key]:core.json_loads(r['payload']) for r in stored}!={r[key]:r for r in rows}:
                    raise ValueError('Current result/node payload roundtrip differs: '+table)
            payload['current_asset_refs']=[{k:asset[k] for k in ('asset_id','version','sha256','filename','size')} for asset in assets]
            await conn.execute('UPDATE content_tasks_local.tasks SET payload=$3::jsonb WHERE owner_id=$1 AND task_id=$2',OWNER,TASK,json.dumps(payload,ensure_ascii=False))
        counts={table:await conn.fetchval('SELECT count(*) FROM content_tasks_local.'+table+' WHERE owner_id=$1 AND task_id=$2',OWNER,TASK) for table in ('tasks','assets','results','nodes')}
        other=await conn.fetchval('SELECT count(*) FROM content_tasks_local.assets WHERE owner_id=$1 AND task_id=$2','other-owner',TASK)
        if other or counts['results']!=3 or counts['nodes']!=len(nodes):raise ValueError('isolated task database verification failed')
        from . import local_task_store
        paper_asset=ref(out/'paper.pdf');fetched=await local_task_store.fetch_asset(OWNER,TASK,paper_asset['asset_id'],1)
        if fetched['bytes']!=(out/'paper.pdf').read_bytes():raise ValueError('current paper task download differs from assembled result')
        try:await local_task_store.fetch_asset('other-owner',TASK,paper_asset['asset_id'],1)
        except LookupError:denied=True
        else:denied=False
        if not denied:raise ValueError('cross-owner task asset access not denied')
        core.write_json(out/'database_verification.json',{'status':'pass','counts':counts,'other_owner_rows':other,
            'real_task_asset_download_verified':True,'cross_owner_fetch_denied':denied,
            'all_result_and_node_payloads_read_back':True,'current_asset_refs':len(assets),
            'scope':'owner/task scoped rows in separate local schema; trusted owner comes from gateway','assets':assets})
    finally:await conn.close()

def main():
    calculate();texts,cards,nodes,pinned=write_sections();pdf=assemble(texts,cards,nodes,pinned);asyncio.run(persist(cards,nodes,pinned))
    local_closure.state('new_input_reference_flow','pass',[str(pdf.relative_to(core.CONTENT_DIR)),'reports/trae/local_new_task_flow/database_verification.json'],
        {'new_inputs_ingested':True,'real_computation':True,'per_section_local_llm_drafts':True,'editorial_review_required':True,'model_autonomous_paper_claimed':False,'assembled_pdf':True,'universal_solver_claimed':False,'product_client_integrated':False})
    print('new input -> real computation -> local section writing -> assembled paper -> separate PG/S3 task store passed',flush=True)

if __name__=='__main__':main()
