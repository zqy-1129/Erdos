"""Produce a truthful final matrix from executed full-corpus and independent evidence."""
import argparse,asyncio,collections,json,re
from pathlib import Path
from . import core,audit,services

RUN_ID='cumcm-2010-2025-codex-r002'
RELEASE_ID='cumcm-2010-2025-codex-v2'
ROOT_REPORTS=Path('D:/Erdos/reports/trae')
RUN=core.CONTENT_DIR/'normalized/cumcm_delivery'/RUN_ID
LOCAL_REPORTS=core.CONTENT_DIR/'reports/trae'

async def database_counts():
    c=await services.pg()
    try:
        async with c.transaction(readonly=True):
            return {'active_release':await c.fetchval("SELECT release_id FROM content_de_codex.activation WHERE competition_id='cumcm'"),
              'unit_kinds':[dict(r) for r in await c.fetch('SELECT kind,count(*) AS count,count(*) FILTER(WHERE external_consumer_allowed) AS consumer_allowed FROM content_de_codex.units WHERE release_id=$1 GROUP BY kind ORDER BY kind',RELEASE_ID)],
              'pgvector_version':await c.fetchval("SELECT extversion FROM pg_extension WHERE extname='vector'"),
              'objects':await c.fetchval('SELECT count(*) FROM content_de_codex.assets WHERE release_id=$1',RELEASE_ID)}
    finally:await c.close()

def main():
    def local(name):return core.load_json(LOCAL_REPORTS/name)
    parse=core.load_json(RUN/'quality/parse_report.json');catalog=core.load_json(RUN/'quality/catalog_report.json')
    if not parse['complete'] or parse['failures'] or parse['processed']!=385:raise ValueError('full parsing required')
    for name in ('cumcm_codex_consumer.json','cumcm_codex_publication.json','cumcm_codex_recovery.json','cumcm_codex_incremental.json'):
        if not all(c['passed'] for c in local(name)['checks']):raise ValueError('final independent integration evidence failed: '+name)
    readonly=local('cumcm_codex_readonly.json')
    if not local('cumcm_codex_rendering.json')['passed'] or not local('cumcm_codex_rendered_fonts.json')['all_passed']:raise ValueError('actual font and rendering evidence required')
    if not readonly['passed']:raise ValueError('read-only proof required')
    if not local('cumcm_codex_database_drift.json')['all_passed']:raise ValueError('typed-field drift rejection evidence required')
    evaluation=local('cumcm_codex_evaluation.json')
    if evaluation['semantic_probe_success']!=40 or not all(v['rejected'] for v in evaluation['boundaries']):raise ValueError('retrieval integration probes required')
    baseline=core.load_json(ROOT_REPORTS/'protected_baseline_final_check.json')
    if not baseline['all_passed']:raise ValueError('protected originals changed')
    regression=(LOCAL_REPORTS/'cumcm_codex_regression_final.log').read_text(encoding='utf-8-sig')
    match=re.search(r'Ran (\d+) tests',regression)
    if not match or not re.search(r'^OK\s*$',regression,re.M):raise ValueError('final regression evidence must pass')
    database=asyncio.run(database_counts())
    if database['active_release']!=RELEASE_ID:raise ValueError('active version was not restored')
    archive=core.load_json(RUN/'quality/archive_report.json');conversion=core.load_json(RUN/'quality/conversion_report.json')
    attachments=core.load_json(RUN/'quality/attachment_report.json');quality=core.load_json(RUN/'quality/quality_metrics.json')
    source=core.load_jsonl(RUN/'sources/source_files.jsonl')
    counts={'original_in_scope_files':sum(r['in_scope'] for r in source),'source_exclusions':sum(not r['in_scope'] for r in source),
      'archive_containers':len(archive['archives']),'archive_members':len(core.load_jsonl(RUN/'derived_sources.jsonl')),
      'converted_documents':len(core.load_jsonl(RUN/'converted_sources.jsonl')),'attachment_profiles':attachments['profiled'],
      'unique_pdf_documents':parse['processed'],'pages':sum(d['pages'] for d in parse['documents']),
      'ocr_pages':sum(d['ocr_pages'] for d in parse['documents']),'pages_needing_review':sum(d['needs_review_pages'] for d in parse['documents']),
      'logical_problems':catalog['problem_entities'],'subproblem_candidates':catalog['subproblem_candidates'],'paper_candidates':catalog['paper_candidates'],
      'historical_retrieval_units':catalog['retrieval_units'],'confirmed_prior_paper_links':catalog['confirmed_prior_links'],
      'writing_recipes':len(core.load_json(RUN/'recipes/writing_recipes.json')),'figure_recipes':len(core.load_json(RUN/'recipes/figure_recipes.json')),
      'format_bundles':2,'database_objects':database['objects'],'normal_consumer_historical_units':sum(r['consumer_allowed'] for r in database['unit_kinds'] if r['kind'].startswith('historical')),
      'regression_tests':int(match.group(1))}
    # A pass is scoped to actual local integration; quality partials remain visible.
    specifications={
      'C00':('pass',['quality/preflight.json','quality/parser_lock.json'],'解释器、独立 schema、保护基线已核验。'),
      'C01':('pass',['sources/source_files.jsonl','sources/coverage_by_year.json'],'所有范围内原件纳管；原文件数不等于逻辑题目数。'),
      'C02':('pass',['quality/archive_report.json','quality/archive_negative.json'],'真实 7-Zip 解包与源链；四项安全负例拒绝。'),
      'C03':('pass',['quality/conversion_report.json','quality/attachment_report.json','catalog/attachment_profiles.jsonl'],'真实 Word 转换及附件只读画像；视频只纳管原件，未伪造采样参数。'),
      'C04':('partial',[],'新增严格 v3 消费/封包/结果契约；v1 原字节不变。完整业务旧模型投影尚未验收。'),
      'C05':('pass',['quality/postgres_constraints.json'],'真实 PostgreSQL 迁移、外键及六项负例；SQLite 只作推理缓存。'),
      'C06':('partial',['quality/parser_lock.json','quality/parser_selection.md'],'实际 OCR 模型权重/配置可追溯；独立校准误差指标未测。'),
      'C07':('pass',['quality/parse_report.json','quality/link_integrity_report.json'],'全量 385 PDF 均实际解析且无失败；稀疏/不可读页显式待复核，不能视作语义正确率。'),
      'C08':('partial',['quality/link_integrity_report.json'],'页、块、bbox、来源校验通过；复杂阅读顺序和章节边界待复核。'),
      'C09':('partial',['quality/quality_review_queue.jsonl'],'公式行 crop 与来源已保存；符号、前提及 LaTeX 语义尚未逐条核验。'),
      'C10':('partial',['quality/quality_review_queue.jsonl'],'当前为图题/表题裁剪，完整矢量/组合图、跨页表及单位关系重建未完成。'),
      'C11':('partial',['catalog/subproblems.json','quality/link_integrity_report.json'],'原题原文小问候选与闭合无环依赖已保存；分支及交付物需逐题确认。'),
      'C12':('partial',['catalog/paper_problem_links.json','quality/catalog_report.json'],'既有确认链保留，其余候选或未关联均显式记录；不能用文件名自动确认。'),
      'C13':('partial',['catalog/block_subproblem_links.jsonl','quality/link_integrity_report.json'],'多对多候选闭合；映射仍需正文语义对照。'),
      'C14':('partial',['catalog/writing_patterns.json'],'识别篇幅/章节/图表统计有来源，明确不等于真实正文篇幅或写作建议。'),
      'C15':('partial',['catalog/retrieval_units.jsonl'],'整行、句界、相邻上下文与父章节保存；完整长推导边界待复核。'),
      'C16':('partial',['recipes/writing_recipes.json','catalog/writing_patterns.json'],'17 条自编写作方案可用；优秀论文形成的经审核内容模板尚未完成。'),
      'C17':('pass',['recipes/figure_recipes.json'],'8 类图表明确当前计算、列角色、单位与缺数据回退；新增负例通过。'),
      'C18':('pass',[],'真实合成最小二乘计算产生 result_card/document_node；业务客户端求解器未在此实现。'),
      'C19':('pass',['templates/format_readiness.json','quality/dynamic_section_tests.json'],'两种完整 ZIP 带规则/字体/动态章节契约；两问、四问图表公式实际编译。'),
      'C20':('partial',['templates/ruleset.json','templates/font_manifest.json','templates/format_readiness.json'],'Windows 字体与渲染已检查。字体是产品默认；2025 官网归档、赛区补充要求和填充论文终审待补。'),
      'C21':('partial',[],'不可变封包的 manifest schema、全字节、引用、向量及 DB roundtrip 已验证，dry-run 不写入；全部画像逐实体业务 schema/语义验收仍缺。'),
      'C22':('pass',[],'所有发布对象经过真实私有 S3 PUT/GET SHA256；非本地文件后端或 ETag 替代。'),
      'C23':('pass',[],'真实 importer 的 25 方案隔离版本测试上传失败、进程中断事务、重试、幂等、冲突、激活回滚；未中断全库正式导入。'),
      'C24':('pass',[],'独立 HTTP bootstrap/retrieve/assets 实际通过，信封对齐既有业务契约；生产 JWT/网关挂载由 BE 接入。'),
      'C25':('pass',['vectors/profile.json'],'固定模型真实 384 维全文分窗 embedding + PG 向量/术语 RRF；模型空间拒绝混用。'),
      'C26':('pass',[],'赛事、年份、版本、stage、题型、用途强过滤，consumer 无法自授 audit 权限。'),
      'C27':('pass',[],'实际完整 capsule token 预算、下载 SHA、坏缓存、路径越界、资产版本锁通过；EN 仍须按所选 LLM 重算预算。'),
      'C28':('partial',['quality/quality_metrics.json','quality/quality_review_queue.jsonl'],'独立 SHA 留出抽样已备齐，未人工/独立转录标注，CER/公式/图表/F1 均为 null。'),
      'C29':('partial',[],'40 同源文本集成探针及 11 边界通过；它们不替代独立标注的 recall@5/nDCG。'),
      'C30':('partial',[],'独立消费者真实 HTTP→PG/S3 下载无源盘读取；独立离线整包 SDK 与生产云路径等价性未验收。'),
      'C31':('pass',[],'全量新快照、旧回归和新增测试通过；只读检查前后 DB/封包不变，14 处保护根均通过 SHA 核验。'),
      'C32':('pass',[],'四方对接、真实本地地址/命令、矩阵与剩余范围均已交付。'),
      'C33':('pass',[],'真实合成 PDF/CSV 经解析、向量、封包、PG/S3 完成两版本新增/改名/改动/移除演练，变更审核失效；未改动历史全库。'),
      'C34':('partial',[],'正常历史内容可用数为 0；25 条自编方案及 2 个格式包可用，不能宣称历史内容消费验收通过。')}
    external={
      'C00':[ROOT_REPORTS/'cumcm_codex_before/protected_baseline.json',LOCAL_REPORTS/'cumcm_codex_runtime.json'],
      'C07':[LOCAL_REPORTS/'cumcm_codex_sparse_pages.jsonl',LOCAL_REPORTS/'cumcm_codex_sparse_summary.json'],
      'C04':[core.CONTENT_DIR/'schemas/cumcm/v3',ROOT_REPORTS/'protected_baseline_final_check.json'],
      'C17':[LOCAL_REPORTS/'cumcm_codex_added_checks.log',LOCAL_REPORTS/'cumcm_codex_rendering.json',core.CONTENT_DIR/'config/cumcm_delivery/rendering/cumcm-figure-style-v1/SEALED.json'],
      'C18':[LOCAL_REPORTS/'qa_result_flow/verification.json',core.CONTENT_DIR/'schemas/cumcm/v3'],
      'C20':[LOCAL_REPORTS/'cumcm_codex_rendered_fonts.json'],
      'C21':[LOCAL_REPORTS/'cumcm_codex_readonly.json',LOCAL_REPORTS/'cumcm_codex_dry_run.json'],
      'C22':[LOCAL_REPORTS/'cumcm_codex_import_full_verified.json',LOCAL_REPORTS/'cumcm_codex_object_negative.json'],
      'C23':[LOCAL_REPORTS/'cumcm_codex_publication.json',LOCAL_REPORTS/'cumcm_codex_recovery.json'],
      'C24':[LOCAL_REPORTS/'cumcm_codex_consumer.json',core.CONTENT_DIR/'handoff/cumcm/content_api_v3.openapi.json'],
      'C25':[LOCAL_REPORTS/'cumcm_codex_embedding_quality.json'],
      'C26':[LOCAL_REPORTS/'cumcm_codex_evaluation.json',LOCAL_REPORTS/'cumcm_codex_consumer.json'],
      'C27':[LOCAL_REPORTS/'cumcm_codex_consumer.json'],
      'C29':[LOCAL_REPORTS/'cumcm_codex_evaluation.json'],
      'C30':[LOCAL_REPORTS/'cumcm_codex_consumer.json'],
      'C31':[LOCAL_REPORTS/'cumcm_codex_regression_final.log',LOCAL_REPORTS/'cumcm_codex_readonly.json',LOCAL_REPORTS/'cumcm_codex_database_drift.json',LOCAL_REPORTS/'cumcm_codex_final_security_checks.log',ROOT_REPORTS/'protected_baseline_final_check.json'],
      'C32':[core.CONTENT_DIR/'handoff/cumcm/CODEX_LOCAL_HANDOFF.md',LOCAL_REPORTS/'cumcm_requirements_pdf_evidence.json'],
      'C33':[LOCAL_REPORTS/'cumcm_codex_incremental.json',LOCAL_REPORTS/'cumcm_codex_regression_final.log'],
      'C34':[ROOT_REPORTS/'cumcm_codex_normal_readiness.json']}
    readiness={'release_id':RELEASE_ID,'normal_consumer_historical_units':counts['normal_consumer_historical_units'],'historical_model_send_allowed':0,
      'self_authored_writing_recipes':counts['writing_recipes'],'self_authored_figure_recipes':counts['figure_recipes'],'format_bundles':2,
      'historical_readiness':'pending independent content QA and explicit distribution/model-use authorization','cloud_deployed':False}
    ROOT_REPORTS.mkdir(parents=True,exist_ok=True)
    def write(name,value):(ROOT_REPORTS/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    write('cumcm_codex_normal_readiness.json',readiness)
    planned=core.load_json(ROOT_REPORTS/'国赛2010至2025_验收清单.json');matrix=[]
    for criterion in planned['required_checks']:
        key=criterion['id'];status,refs,note=specifications[key]
        evidence=[str(RUN/p) for p in refs]+[str(p) for p in external.get(key,[])]
        for path in evidence:
            if not Path(path).exists():raise ValueError('missing evidence '+path)
        matrix.append({'check_id':key,'phase':criterion['phase'],'requirement':criterion['requirement'],'status':status,
          'executed_at':core.now_utc_iso(),'command_or_method':'actual source-bound audit and linked executable QA; detailed command recipes in handoff',
          'environment_alias':'local-audit','exit_code_or_result':note,'input_refs':[RUN_ID,RELEASE_ID],'output_refs':evidence,'evidence_paths':evidence,
          'limitations':note if status!='pass' else 'Pass applies to declared local scope; production cloud/business integration not claimed.'})
    summary=dict(collections.Counter(r['status'] for r in matrix))
    write('cumcm_codex_acceptance_evidence.json',{'generated_at':core.now_utc_iso(),'release_id':RELEASE_ID,'counts':counts,'database':database,
      'summary':summary,'overall':'local structural integration completed; historical content quality/normal consumer and cloud acceptance incomplete','checks':matrix})
    pending=[{'id':'quality','owner':'DE + 独立复核人员','work':'完成 32 篇全结构、100 段文字、100 公式、50 图、30 表的独立源页复核，补公式语义、完整图表和单位关系，并实测指标','queue':str(RUN/'quality/quality_review_queue.jsonl')},
      {'id':'content_templates','owner':'DE','work':'核实论文父题、小问映射、正文边界和长推导上下文；将经审核优秀论文结构/写法/篇幅转为题型内容模板'},
      {'id':'retrieval_gold','owner':'DE + 独立标注人员','work':'建立未用于调优的独立新题查询 gold，分别测术语 baseline 与 hybrid 的 recall@5/nDCG'},
      {'id':'rights','owner':'资料授权负责人','work':'逐资料确认原件分发、画像/片段分发和第三方模型使用权限；没有证据不得改为 allowed'},
      {'id':'cloud','owner':'BE/运维','work':'选定云 PostgreSQL+pgvector 和私有对象存储，建立独立环境适配器、TLS/备份/JWT/网关并实际迁移联调；当前只启用本地'},
      {'id':'client_engine','owner':'FE/EN','work':'按 IPC/主进程内容 API/BYOK 直连架构接入，执行真实新题计算、分节写作、动态拼装和填充论文校验；模型/新题留在客户端'},
      {'id':'format','owner':'DE/FE','work':'补 2025 原始官网归档、目标年份/赛区规则和实际填充论文终审；Word/DOCX 输出等价性另验收'},
      {'id':'incremental_scale','owner':'DE','work':'真实合成资料两版本全链演练已通过；下次历史资料实际变化时，按相同规则做全库新版本和质量回归，不修改旧 SEALED 版本'}]
    write('cumcm_codex_pending.json',pending)
    write('cumcm_delivery_latest.json',{'run_id':RUN_ID,'release_id':RELEASE_ID,'environment':'local-audit','report':str(ROOT_REPORTS/'cumcm_codex_acceptance.md'),'evidence':str(ROOT_REPORTS/'cumcm_codex_acceptance_evidence.json'),'pending':str(ROOT_REPORTS/'cumcm_codex_pending.json')})
    phase_status={str(phase):('pass' if all(r['status']=='pass' for r in matrix if r['phase']==phase) else 'partial') for phase in range(11)}
    write('cumcm_codex_run_state.json',{'generated_at':core.now_utc_iso(),'run_id':RUN_ID,'release_id':RELEASE_ID,'environment':'local-audit','stages':phase_status,'full_processing_and_import_complete':True,'normal_historical_content_accepted':False,'cloud_deployed':False,'evidence':str(ROOT_REPORTS/'cumcm_codex_acceptance_evidence.json')})
    lines=['# 国赛 2010—2025 最终审核与修复交付','',
      '**结论：本地全量处理、不可变封包、真实 PostgreSQL/S3/embedding 和独立消费者 API 联调已完成。Trae 的“全部完成”不能等同于十阶段全部验收通过。历史内容模板的语义质量、正常客户放行、既有业务客户端接入和云上线仍未达标。**','',
      '本次不购买云资源，不修改源资料、旧快照或既有服务端/客户端/引擎代码。项目专用本地服务已配置；资料原件仍在 D:/Erdos_data。','',
      '## 实际数量与存放','',
      '| 内容 | 数量 |','|---|---:|']
    labels={'original_in_scope_files':'范围内原始文件','source_exclusions':'明确排除的来源文件','archive_containers':'含嵌套的真实解包容器','archive_members':'解包来源成员','converted_documents':'真实转换的 DOC/DOCX','attachment_profiles':'附件画像','unique_pdf_documents':'实际解析的唯一 PDF','pages':'实际处理页数','ocr_pages':'实际 OCR 页数','pages_needing_review':'稀疏/不可读待复核页','logical_problems':'逻辑赛题','subproblem_candidates':'原题小问候选','paper_candidates':'论文候选','historical_retrieval_units':'历史检索单元','confirmed_prior_paper_links':'保留的既有确认关联','writing_recipes':'自编写作方案','figure_recipes':'自编图表方案','format_bundles':'完整格式包','database_objects':'发布的唯一对象','normal_consumer_historical_units':'普通客户可用历史单元','regression_tests':'通过的回归测试'}
    lines.extend('| {} | {} |'.format(labels[k],v) for k,v in counts.items())
    lines+=['','年度计数不人为凑满 80 题：2010—2018 的 A—D 与 2019—2025 的 A—E 共 71 个逻辑题。论文来源含压缩包，原始文件数与去重后论文候选数口径不同。','',
      '- 原件：`D:/Erdos_data`（只读）。',
      '- 处理检查点：`'+str(RUN)+'`。',
      '- 不可变封包：`'+str(core.CONTENT_DIR/'out/cumcm_delivery/releases'/RELEASE_ID)+'`，包括全部对象、源链、页数据和真实向量二进制。',
      '- PostgreSQL：`127.0.0.1:55433 / cumcm_codex / content_de_codex`，保存关系、JSONB、权限、版本和 pgvector。',
      '- S3：`127.0.0.1:18333 / cumcm-private`，保存内容寻址的原件、转换件、裁剪证据和格式 ZIP。',
      '- 内容 API：`127.0.0.1:18789`，只绑定本机；客户端按固定版本按需下载，不把全库打进 exe。',
      '- 图表样式：`config/cumcm_delivery/rendering/cumcm-figure-style-v1` 独立封存，bootstrap/capsule 返回版本与 canonical SHA。它是产品建议，独立于历史资料 release。',
      '- 密钥只在 `.runtime/local/`，本报告不包含密钥；生产云尚未配置。','',
      '## 已执行的验证与修复','',
      '已补齐真实向量、完整长文本处理、源链和附件入库，并修复隐含 tokenizer 截断、版本空间校验、Word 转换身份、过期裁剪混入封包、固定三问/虚构示例数值、Windows 字体、规则/动态章节随包交付、下载越界与坏缓存、信封/审计鉴权、激活条件与增量构建缺附件/质量检查。','',
      '{} 项回归通过；385 PDF 无解析失败；全部发布对象实际 S3 取回核对 SHA；所有入库实体/向量与封包逐项 roundtrip 核验。40 个同源文本检索集成探针与 11 个边界通过，另完成真实 HTTP 模板/原件下载、用途拒绝、缓存校验。'.format(counts['regression_tests']),'',
      '隔离的 25 方案版本使用同一真实 importer，测试上传阶段异常、终止已执行未提交 INSERT 的进程、数据库回滚与重试；另实际测试重复导入、同 ID 冲突、激活和回滚。最后活跃版本恢复为完整 v2。没有中断全库导入来宣称更大测试范围。','',
      '另使用真实合成 PDF/CSV 完成两版资料的新增、改变、移除、改名，实际执行解析、embedding、封包、S3 和 PostgreSQL；旧封包保留、内容身份与审核失效符合预期。原始历史资料未被修改。图表样式实际导出三种格式，并从经验证的合成计算输入/运行 ID 生成图文节点示例。','',
      '只读 QC 前后封包和数据库摘要不变。14 个保护根的文件 SHA 已核验，包括全部原资料和旧快照。','',
      '本次续审还修复了指定 Python 3.8 环境对空查询串的处理：首次只选比赛即可发现当前版本。{} 项实际 HTTP 消费检查通过，包括未发布/测试版本的普通访问拒绝；另在隔离合成版本中真实改动数据库题目标题、检索种类和版本元数据，三项漂移均被拒绝并精确恢复。发布指针检查核验封存的关键数据库输入及所有入库字段；全量 QC 另核验全部封包文件，避免每次切换指针重复扫描所有对象。'.format(len(local('cumcm_codex_consumer.json')['checks'])),'',
      '## 字体、内容模板与质量界限','',
      'Typst/LaTeX 格式包均实际编译；已检查空白框架及两问/四问的图、表、公式用例。中文宋体、标题黑体等属于产品默认排版，**不是全国比赛强制字体**。2026 规则有组委会 PDF 来源；2025 镜像与本地格式材料需补官网归档。任何客户填充后的论文还要核验当届页数、摘要、身份信息、文件大小和赛区要求。','',
      '当前保存的是公式/图题/表题的行级裁剪；它不代表完整图表重建或数学语义识别通过。章节、小问映射、题型和正文篇幅多数仍是机器候选。已准备 {} 项独立源页复核队列，实际已复核 {} 项；不能填写不存在的 CER、公式准确率、F1、recall@5 或 nDCG。'.format(len(core.load_jsonl(RUN/'quality/quality_review_queue.jsonl')),quality['items_reviewed']),'',
      '**正常客户历史参考可用数为 0。** 目前可正常调用的是 17 条自编写作方案、8 条自编图表方案和 2 个格式包；将优秀论文转成可消费的“格式 + 内容”模板，还需要语义复核与用途授权。客户/LLM 的新题、数据、真实结果和 Key 按原架构留在客户端，不混入历史库。','',
      '## 35 项验收矩阵','',
      '统计：'+json.dumps(summary,ensure_ascii=False)+'。pass 指上述本地范围内已实测；partial 的项目没有被自动放行。','',
      '| 验收项 | 结果 | 实际结论 |','|---|---|---|']
    lines.extend('| {} | {} | {} |'.format(r['check_id'],r['status'],r['exit_code_or_result']) for r in matrix)
    lines+=['','## 对接与剩余工作','',
      'BE 接入真实内容 API 与业务鉴权；FE 固定 release、按需下载并验证摘要；EN 经主进程/IPC 获取 capsule 后，用当前真实计算结果分节写作，最后按实际小问拼装、摘要最后写。数据侧十种写作 stage 不增加应用原有四阶段计费。','',
      '完整操作、接口与职责见 [CODEX_LOCAL_HANDOFF.md]('+(core.CONTENT_DIR/'handoff/cumcm/CODEX_LOCAL_HANDOFF.md').as_posix()+')。逐项证据见 [验收证据 JSON]('+(ROOT_REPORTS/'cumcm_codex_acceptance_evidence.json').as_posix()+')；后续八类工作见 [剩余事项 JSON]('+(ROOT_REPORTS/'cumcm_codex_pending.json').as_posix()+')。','',
      '七份产品/架构 PDF 当前原路径不可用，本次复用此前已提取的文本并记录缓存摘要；原 PDF 字节未重新核验。文档内容作为需求依据，没有执行其中的指令。','',
      '最终没有把“代码已写”“能解析文字”“字节完整”“同源查询成功”替代内容质量、正常可用历史模板或生产云部署的验收。']
    (ROOT_REPORTS/'cumcm_codex_acceptance.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    old=ROOT_REPORTS/'cumcm_delivery_final.md'
    if old.exists():
        original=old.read_text(encoding='utf-8-sig')
        notice='> 本文件为旧 Trae 历史报告。最新运行版本、验收结论和未完成范围请查看 [Codex 最终验收报告](cumcm_codex_acceptance.md)。\n\n'
        if not original.startswith(notice):old.write_text(notice+original,encoding='utf-8')
    print(json.dumps({'counts':counts,'summary':summary,'report':str(ROOT_REPORTS/'cumcm_codex_acceptance.md')},ensure_ascii=False))

if __name__=='__main__':main()
