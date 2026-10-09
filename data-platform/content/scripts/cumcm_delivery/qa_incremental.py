"""Real synthetic source changes exercise two sealed versions without touching user originals."""
import argparse,asyncio,shutil
from . import core,audit,inventory,attachments,incremental,qa_publish

OLD='cumcm-2010-2025-qa-inc-v1'
NEW='cumcm-2010-2025-qa-inc-v2'
BASE=audit.RUNTIME/'local/qa-incremental-sources'

def args(version):
    return argparse.Namespace(run_id=version,release_id=version,data_root=str(BASE),scope_config='config/cumcm_delivery/scope.json',target='local-audit',ocr=True,max_documents=0,activate=False,qa_fixture_only=True,previous=OLD)

def make_pdf(path,changed=False,paper=False):
    source=path.with_suffix('.typ')
    text='#set text(font: "SimSun", size: 11pt)\n#set page(paper: "a4")\n'
    text+='= 合成增量联调论文\n\n' if paper else '= A题 合成增量联调题面\n\n'
    text+='本材料仅为数据流水线合成测试，不是历史竞赛资料。\n\n'
    text+='问题一：依据当前输入数据建立线性模型，给出变量单位及验证步骤。\n\n'
    text+='问题二：对当前结果进行敏感性分析，解释适用条件。\n\n'
    if changed:text+='新增约束：最大输入取值不超过十，输出需要同时报告误差。\n\n'
    if paper:
        for page in range(3):
            if page:text+='#pagebreak()\n'
            text+='= 模型建立与验证\n\n'
            text+='本节是合成测试文字，用于检验来源身份、章节保存和重新审核状态。先记录输入摘要和单位，再给出模型参数、计算过程、验证依据。历史论文数值不能作为当前计算输出。\n\n'*12
    core.write_bytes(source,text.encode('utf-8'))
    audit.command([audit.TYPST,'compile',source,path])
    core.output_path(source).unlink()

def build(version):
    a=args(version);rd=audit.run_dir(a)
    inventory.main(a)
    if version==NEW:incremental.record(a)
    audit.expand(a);audit.parse(a);audit.normalize_sources(a);audit.catalog(a);attachments.main(a)
    template_source=core.CONTENT_DIR/'normalized/cumcm_delivery/cumcm-2010-2025-codex-r002/templates'
    for path in template_source.rglob('*'):
        if path.is_file():audit.copy_verified(path,rd/'templates'/path.relative_to(template_source),core.sha256_of(path))
    audit.prepare_embeddings(a);audit.pack(a);audit.import_release(a)
    return audit.read_only_check(a)

def main():
    receipt=core.CONTENT_DIR/'reports/trae/cumcm_codex_incremental.json'
    if receipt.exists():
        evidence=core.load_json(receipt)
        if all(c['passed'] for c in evidence['checks']):
            print('Existing completed incremental evidence retained without rewriting fixtures',flush=True)
            return
        raise ValueError('previous incremental fixture failed; inspect it before reusing a version')
    if (audit.release_dir(args(OLD))/'SEALED.json').exists() or (audit.release_dir(args(NEW))/'SEALED.json').exists():
        raise ValueError('incremental fixture is immutable and already used; do not overwrite it')
    starting_pointer=asyncio.run(qa_publish.pointer())
    if starting_pointer not in (qa_publish.FULL,qa_publish.QA):raise ValueError('unexpected active release before inactive incremental test')
    problem=BASE/'problems/12_CUMCM国赛/CUMCM2020Problems'
    paper=BASE/'papers_pending_auth/12_CUMCM国赛/2020年合成联调'
    (BASE/'templates/12_CUMCM国赛').mkdir(parents=True,exist_ok=True)
    problem.mkdir(parents=True,exist_ok=True);paper.mkdir(parents=True,exist_ok=True)
    make_pdf(problem/'A题.pdf');make_pdf(paper/'A题合成论文.pdf',paper=True)
    for name,value in [('changed.csv',2),('renamed.csv',3),('removed.csv',4)]:
        core.write_bytes(problem/name,('x,y\n1,%d\n2,%d\n'%(value,value*2)).encode())
    old_check=build(OLD)
    old_root=audit.release_dir(args(OLD));old_manifest=core.sha256_of(old_root/'manifest.json')
    old_problem=core.load_json(old_root/'metadata/catalog/problems.json')[0]['problem_id']
    old_case=core.load_json(old_root/'metadata/catalog/papers.json')[0]['case_id']
    # These targets are explicitly named and contained in the synthetic workspace root.
    core.write_bytes(problem/'changed.csv',b'x,y\n1,3\n2,6\n')
    source=core.output_path(problem/'renamed.csv');destination=core.output_path(problem/'renamed-new.csv')
    source.rename(destination)
    core.output_path(problem/'removed.csv').unlink()
    core.write_bytes(problem/'added.csv',b'x,y\n3,9\n')
    make_pdf(paper/'A题合成论文.pdf',changed=True,paper=True)
    new_check=build(NEW)
    changes=core.load_json(audit.run_dir(args(NEW))/'quality/source_changes.json')
    new_root=audit.release_dir(args(NEW))
    new_problem=core.load_json(new_root/'metadata/catalog/problems.json')[0]['problem_id']
    new_case=core.load_json(new_root/'metadata/catalog/papers.json')[0]
    checks=[{'name':'real_added_changed_removed_renamed_source_bytes','passed':len(changes['renames'])==1 and len(changes['changed'])==2 and any(p.endswith('added.csv') for p in changes['added_paths']) and any(p.endswith('removed.csv') for p in changes['removed_paths'])},
      {'name':'stable_logical_problem_identity','passed':old_problem==new_problem},
      {'name':'changed_paper_has_new_content_identity_and_pending_review','passed':old_case!=new_case['case_id'] and new_case['review_status']=='machine_candidate' and not new_case['license']['distribute_profile']},
      {'name':'changed_and_removed_evidence_explicitly_invalidated','passed':len(changes['review_invalidated_content_sha256'])>=2},
      {'name':'two_actual_sealed_database_versions_preserved','passed':old_check['database_units']>25 and new_check['database_units']>25 and core.sha256_of(old_root/'manifest.json')==old_manifest},
      {'name':'synthetic_updates_remain_inactive','passed':asyncio.run(qa_publish.pointer()) not in (OLD,NEW)}]
    core.write_json(receipt,{'checks':checks,'source_changes':changes,'old_release':OLD,'new_release':NEW,'scope':'real synthetic PDFs/CSVs and real parser/embedding/sealed package/PostgreSQL/S3 importer; not modified historical corpus','original_user_data_modified':False,'starting_active_release':starting_pointer,'ending_active_release':asyncio.run(qa_publish.pointer()),'concurrent_publication_rollback_may_restore_full_release':True,'old_check':old_check,'new_check':new_check})
    if not all(c['passed'] for c in checks):raise ValueError('incremental end-to-end fixture failed')
    print('6 real incremental source/version/storage checks passed',flush=True)

if __name__=='__main__':main()
