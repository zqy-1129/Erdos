"""Build delivery evidence only from completed local checks, preserving quality gaps."""
import asyncio,json,re,zipfile
from pathlib import Path
from . import core,audit,services,local_closure,structured_store

async def database():
    conn=await services.pg();release=local_closure.config()['release_id']
    try:
        async with conn.transaction(readonly=True):
            active=await conn.fetchval("SELECT release_id FROM content_de_codex.activation WHERE competition_id='cumcm'")
            if active!=release:raise ValueError('v3 is not active')
            await structured_store.activation_guard(conn,release)
            return {'active_release':active,'pgvector_version':await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname='vector'"),
                'units':await conn.fetchval('SELECT count(*) FROM content_de_codex.units WHERE release_id=$1',release),
                'assets':await conn.fetchval('SELECT count(*) FROM content_de_codex.assets WHERE release_id=$1',release),
                'historical_external_allowed':await conn.fetchval("SELECT count(*) FROM content_de_codex.units WHERE release_id=$1 AND kind LIKE 'historical_%' AND external_consumer_allowed",release),
                'structured_receipt':dict(await conn.fetchrow('SELECT manifest_sha256,counts,fingerprints FROM content_de_codex.structured_import_receipts WHERE release_id=$1',release))}
    finally:await conn.close()

def main():
    a=local_closure.args();rd=audit.run_dir(a);dest=audit.release_dir(a);reports=core.CONTENT_DIR/'reports/trae'
    def q(name):return core.load_json(rd/'quality'/name)
    def r(name):return core.load_json(reports/name)
    access=r('cumcm_local_access_acceptance.json');constraints=r('cumcm_local_structured_constraints.json')
    paper=r('local_new_task_flow/paper_full_checks.json');visual=r('local_new_task_flow/paper_visual_review.json')
    task=r('local_new_task_flow/database_verification.json');reproduction=r('local_new_task_flow/reproduction_verification.json')
    editorial=r('local_new_task_flow/editorial_review.json')
    if not editorial['passed'] or editorial['actual_model_drafts']!=18 or editorial['historical_raw_prose_in_writer_prompt'] is not False or not task.get('all_result_and_node_payloads_read_back'):
        raise ValueError('Actual editorial or complete new-task readbacks missing')
    topic=r('cumcm_local_topic_retrieval.json');structure=q('full_business_contract_validation.json')
    backup=r('cumcm_local_backup_restore.json');sdk=r('cumcm_local_sdk_package.json')
    if not backup['passed'] or sdk['contains_corpus_or_credentials_or_models_or_fonts']:raise ValueError('backup or clean SDK package acceptance missing')
    for value in (access,constraints,paper):
        if not value['all_passed']:raise ValueError('required real acceptance failed')
    if not visual['passed'] or not reproduction['passed'] or task['status']!='pass':raise ValueError('new paper review/reproduction/store incomplete')
    if paper['pdf_sha256']!=visual['pdf_sha256'] or paper['pdf_sha256']!=core.sha256_of(reports/'local_new_task_flow/paper.pdf'):
        raise ValueError('visual review is for another paper revision')
    preservation=q('protected_local_preservation.json')
    if not preservation['existing_files_unchanged']:raise ValueError('baseline source bytes changed')
    if not structure['formulas_included'] or structure['status']!='pass':raise ValueError('full business contract coverage missing')
    regression=(reports/'cumcm_local_closure_regression.log').read_text(encoding='utf-8-sig');m=re.search(r'Ran (\d+) tests',regression)
    if not m or not re.search(r'^OK\s*$',regression,re.M):raise ValueError('regression not passed')
    db=asyncio.run(database());seal=core.load_json(dest/'SEALED.json');manifest=core.load_json(dest/'manifest.json')
    if db['structured_receipt']['manifest_sha256']!=seal['manifest_sha256']:raise ValueError('final release pin differs')
    for key in ('counts','fingerprints'):db['structured_receipt'][key]=core.json_loads(db['structured_receipt'][key])
    enrich=q('content_enrichment.json');formula=q('formula_enrichment_progress.json');model=q('model_quality_assessment.json')
    papers=core.load_json(rd/'catalog/papers.json');pending=[{'case_id':p['case_id'],'year':p['year'],'source_sha256':p['source_sha256'],
        'candidate_problem_id':p['problem_id'],'relation_status':p['relation_status'],'evidence':p.get('relation_evidence'),
        'next_action':'Compare visible title/abstract/restatement with same-year original tasks; save adjudication in a new derivative version, never edit this sealed release.'}
        for p in papers if p['relation_status'] not in ('source_body_title_match','verified_prior_snapshot')]
    stages=[
        (1,'原件纳管、解包和格式转换','pass','全部既定范围原件和派生源链已纳管；其他赛事新增下载不并入本 release。'),
        (2,'业务契约、PostgreSQL 与旧模型投影','pass','独立 schema；精确 12/8/8 字段投影、真实外键与字段负例；未确认关联不虚构投影。'),
        (3,'全量多模态解析与证据','partial','385 PDF/9713 页、整页与公式区域全量处理完成；复杂阅读顺序、表格结构与公式语义未获全库金标认证。'),
        (4,'题面、小问与论文关联','partial','71 题/183 小问/261 论文，140 个关联具源依据；121 个关联仍待逐篇判定，不声称全库确认。'),
        (5,'检索单元与内容/图表方案','partial','17960 历史单元、17 写作/8 图表方案与源章节篇幅统计可调用；推导前提与图表数据不是全部人工核验。'),
        (6,'格式包、字体与规则','pass','Typst/LaTeX 动态 3/5 问四用例及最终完整论文实际编译和看页；全国 2026 规则证据，赛区规则另做覆盖。'),
        (7,'真实本地导入与发布','pass','全量 SHA 封存；真实 PG/S3，结构导入回执与全部字段/JSON 指纹通过后激活；旧版保留。'),
        (8,'按阶段检索、证据与下载契约','pass','团队用途与私有证据可真实调用；普通用途及第三方原文门禁生效；绑定 release/asset/version/SHA。'),
        (9,'语义质量与检索有效性','partial','1467 本地候选识别及拒用记录、独立源查看、6 条新措辞主题查询已实测；完整 CER/公式准确率/结构 F1/40 条独立 recall/nDCG 未认证。'),
        (10,'独立消费、重建和新题参考闭环','pass','独立标准库在线/离线消费与字节一致；新输入实际计算、18 节本机模型草稿及助理逐节审核、图表、PDF、任务隔离与独立重算；非模型独立自动成文。')]
    checks=[{'stage':n,'name':name,'status':status,'scope':scope} for n,name,status,scope in stages]
    report={'executed_at':core.now_utc_iso(),'scope':'cumcm 2010-2025 local team internal DE delivery',
        'release_id':a.release_id,'manifest_sha256':seal['manifest_sha256'],'immutable_manifest_files':len(manifest['files']),
        'sealed_bytes':sum(x['size'] for x in manifest['files']),'database':db,'component_counts':structure['counts'],
        'formula_counts':formula['counts'],'model_quality':{k:model[k] for k in ('model_observations','status_counts','independent_source_inspections','ground_truth_accuracy_claimed')},
        'pending_paper_relations':len(pending),'regression_tests':int(m.group(1)), 'stages':checks,
        'local_engineering_access_checks_passed':access['all_passed'],'typed_store_checks_passed':constraints['all_passed'],
        'new_task_reference_flow_passed':True,'all_ten_original_quality_requirements_passed':False,
        'new_paper_editorial_review':{'actual_model_drafts':18,'assistant_review_required':True,'model_autonomous_paper_claimed':False},
        'raw_originals_modified':False,'third_party_raw_upload_enabled':False,'cloud_deployed':False,
        'product_exe_integrated':False,'universal_solver_claimed':False,
        'source_topic_hit_at_5':topic['source_topic_hit_at_5'],'full_corpus_semantic_accuracy_certified':False,
        'preservation':preservation,'paper_pdf_sha256':paper['pdf_sha256'],'private_backup_restored':True,'client_sdk_package':sdk}
    core.write_json(reports/'cumcm_local_closure_acceptance.json',report)
    core.write_json(reports/'cumcm_local_closure_pending.json',{'scope':'quality/identity followup, not hidden runtime bugs',
        'pending_paper_relations':pending,'metrics_not_certified':['OCR CER','formula symbol accuracy','chart/table gold','structure F1','40-query independent Recall@5/nDCG@5'],
        'candidate_policy':'Original pages always available; candidates never become automatically verified facts.',
        'out_of_scope_by_user':['cloud deployment','external original distribution','third-party original upload'],
        'team_handoff_remaining':['BE production gateway identity/entitlements','FE product IPC and exe cache integration','EN arbitrary new-task solver orchestration']})
    paths=[rd/'quality/full_business_contract_validation.json',rd/'quality/model_quality_assessment.json',rd/'quality/legacy_projection_pg.json',
           reports/'cumcm_local_access_acceptance.json',reports/'cumcm_local_structured_store.json',reports/'cumcm_local_structured_constraints.json',
           reports/'cumcm_local_topic_retrieval.json',reports/'cumcm_local_closure_regression.log',reports/'local_new_task_flow/paper_full_checks.json',
           reports/'local_new_task_flow/paper_visual_review.json',reports/'local_new_task_flow/reproduction_verification.json',reports/'local_new_task_flow/database_verification.json']
    paths.extend([reports/'cumcm_local_backup_restore.json',reports/'cumcm_local_sdk_package.json',rd/'quality/formula_identity_repair.json'])
    paths.extend([reports/'local_new_task_flow/editorial_review.json',reports/'local_new_task_flow/editorial_decisions.json'])
    core.write_json(reports/'cumcm_local_closure_evidence.json',{'release_id':a.release_id,'manifest_sha256':seal['manifest_sha256'],
        'evidence':[{'path':str(p),'sha256':core.sha256_of(p),'size':p.stat().st_size} for p in paths],'credentials_included':False})
    text='# 国赛 2010—2025 本地交付验收\n\n'
    text+='本地工程闭环已实际验收：历史源证据 → PostgreSQL/对象服务 → 团队检索与按需下载 → 新题真实计算 → 分节写作 → 完整 PDF → 独立任务存取。全部原始十阶段的语义质量目标尚未全部达标，以下保留准确状态。\n\n'
    text+='发布版本：`'+a.release_id+'`；manifest SHA256：`'+seal['manifest_sha256']+'`。\n\n'
    text+='| 阶段 | 内容 | 结果 | 实测范围与边界 |\n|---|---|---|---|\n'
    for n,name,status,scope in stages:text+='| {} | {} | {} | {} |\n'.format(n,name,status,scope)
    text+='\n数据库保存关系、384 维向量及页/块/章节/公式索引，文件在本地私有对象服务。原件仍在 D:/Erdos_data。新模型、权重和服务均在 D:/Erdos_LocalAI；exe 接入只带 SDK 和按需资源，不带资料全库。\n\n'
    text+='实际规模：385 PDF、9713 页、{} 公式区域、{} 精确显示公式截图、17960 历史检索单元、8492 独占章节；本地视觉候选 {} 项。机器候选不是数学语义认证。\n\n'.format(formula['counts']['regions'],formula['counts']['crop_evidence'],model['model_observations'])
    text+='新题样例是自编传感器测试：12 行输入、真实最小二乘/留一验证/500 次自助采样、3 个结果卡、3 幅图（PNG/PDF/SVG）、18 节本机模型草稿及助理逐节审核、9 页最终 PDF、90 个数值独立重算及完整复现脚本。原文进入写作上下文曾造成领域污染，已改为只提供章节和篇幅结构参照，失败草稿与改写记录全部保留。它证明经审核的接口闭环，不代表小模型独立可靠成文、通用国赛求解或产品 UI 已接入。\n\n'
    text+='格式经实际字体及页面检查，宋体/黑体/楷体/Times New Roman/Courier New 是产品默认值；全国 2026 原始规则有证据，2025 归档与赛区要求保留各自核验状态。\n\n'
    text+='{} 条回归通过，{} 项内容调用检查、{} 项结构约束/漂移检查通过。六条已看源主题的改写查询命中比例为 {:.3f}，不能解释成全库 Recall@5。\n\n'.format(m.group(1),len(access['checks']),len(constraints['checks']),topic['source_topic_hit_at_5'])
    text+='已实际生成私有 PostgreSQL 备份并恢复到独立测试库，全部结构指纹、活跃版本与核对表数量一致；工作库未被恢复操作覆盖。S3 仍可从完整封存包重建，本项未假称已做完整新桶恢复。\n\n'
    text+='原有 14 个保护目录的基线字节均未改动或丢失；原件目录新增 4941 个其他赛事下载，严格“无新增”检查如实为未通过，新增文件已保留。\n\n'
    text+='接收说明：[LOCAL_V3](../../handoff/cumcm/LOCAL_V3.md)；[客户端 SDK ZIP](../../handoff/cumcm/cumcm-local-client-sdk-v3.zip)。详细状态见[验收 JSON](cumcm_local_closure_acceptance.json)、[证据摘要](cumcm_local_closure_evidence.json)及[未确定关联与质量指标](cumcm_local_closure_pending.json)。\n'
    core.write_bytes(reports/'cumcm_local_closure_acceptance.md',text.encode('utf-8'))
    print({'release_id':a.release_id,'engineering_closure':True,'all_original_quality_targets':False,'stages':[(x['stage'],x['status']) for x in checks]},flush=True)

if __name__=='__main__':main()
