"""阶段03：国赛真实小样本标准化。

把选定的 3 道国赛题 + 7 篇关联论文，从 D:/Erdos_data 复制到工作区样本目录，
重新流式计算源 hash（读前读后核对 size/mtime），与旧 seed/阶段一 hash 对比，
生成旧十二键 record、problem_profile/case_profile、assets、bundle、溯源与报告。

原则：
- 原件只读；仅复制本批选中的少量文件到样本目录，不解压压缩附件。
- 幂等：相同输入重复运行得到相同业务 ID 与内容 hash；已有不一致副本登记冲突，不静默覆盖。
- 不成像/不分类：题型标 unknown，画像结构/写法/篇幅/图表全部留空，不编造 approved/置信度/统计。
- 授权缺省全 false，不主动授予任何许可。

用法（工作目录 D:/Erdos/data-platform/content）：
    <pytorch>/python scripts/normalize_cumcm_sample.py --data-root D:/Erdos_data \
        --out normalized/stage03/cumcm
    <pytorch>/python scripts/normalize_cumcm_sample.py --data-root D:/Erdos_data \
        --out normalized/stage03/cumcm --check   # 只读：文件/文本/来源/父题/契约 QC
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

CHUNK_SIZE = 1024 * 1024
CONTENT_DIR = Path(os.path.abspath(__file__)).parent.parent  # scripts/.. = content
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# --------------------------------------------------------------------------- #
# 明确选择清单（proben_id/case_id、源路径、旧 seed hash、选择依据）               #
# --------------------------------------------------------------------------- #
SAMPLE_PROBLEMS = [
    {
        "problem_id": "cumcm-2018-B", "competition_id": "cumcm", "year": 2018, "problem_code": "B",
        "title": "智能RGV的动态调度策略",
        "selection_basis": "旧 seed cumcm-2018-B；题目文件为 .doc，题面文本沿用既有 WPS 抽取 UTF-8 结果",
        "source": {
            "rel": "problems/12_CUMCM国赛/CUMCM2018Problems/2018-B-Chinese/CUMCM-2018-Problem-B-Chinese.doc",
            "seed_sha256": "8945f462be3fad94bf7750a86cd2fe3462b78ae0606bf606b8da143c997d50fe",
            "media_type": "application/msword",
            "text_repo_rel": "out/texts/cumcm-2018-B.txt",
            "text_seed_sha256": "a22ce30bee062fbb296bfe06e8303393f39602baf7246a0cba9710a6e8d25f7e",
            "extract_unsupported": "source 为 .doc，本环境不启动 WPS 重抽，题面复用既有 UTF-8 文本",
        },
        "attachments": [
            {"rel": "problems/12_CUMCM国赛/CUMCM2018Problems/2018-B-Chinese/CUMCM-2018-Problem-B-Chinese-Appendix1.doc",
             "seed_sha256": "84926364ac401fdf7aec325cf5c0bf7a92772f33a20135cb64bb15f09836d21f", "media_type": "application/msword"},
            {"rel": "problems/12_CUMCM国赛/CUMCM2018Problems/2018-B-Chinese/CUMCM-2018-Problem-B-Chinese-Appendix-2.rar",
             "seed_sha256": "20b3e2fb11f014eec56b9045f45f94f676685a2f85f62dffda0d4a2ffc6218cc", "media_type": "application/vnd.rar"},
        ],
        "legacy_tags": ["优化", "动态调度"],
    },
    {
        "problem_id": "cumcm-2023-A", "competition_id": "cumcm", "year": 2023, "problem_code": "A",
        "title": "定日镜场的优化设计",
        "selection_basis": "旧 seed cumcm-2023-A；有 3 篇关联论文（A092/A165/A175）",
        "source": {
            "rel": "problems/12_CUMCM国赛/CUMCM2023Problems/A题/A题.pdf",
            "seed_sha256": "3e2507025f0a776e3ba8e340e646d2105896482b055cdd6a22862dedbf89311b",
            "media_type": "application/pdf",
            "text_repo_rel": "out/texts/cumcm-2023-A.txt",
            "text_seed_sha256": "2bf2e84f0e51007e85c10dc0a7592161ccd55920f8c10e24a21cc2d202755b35",
            "extract_unsupported": None,
        },
        "attachments": [
            {"rel": "problems/12_CUMCM国赛/CUMCM2023Problems/A题/result2.xlsx",
             "seed_sha256": "4f9fbd0cc4e817063616e8202aab25f02cd68a9b4409fe77b13a9f8f0306caa1", "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            {"rel": "problems/12_CUMCM国赛/CUMCM2023Problems/A题/result3.xlsx",
             "seed_sha256": "4f9fbd0cc4e817063616e8202aab25f02cd68a9b4409fe77b13a9f8f0306caa1", "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            {"rel": "problems/12_CUMCM国赛/CUMCM2023Problems/A题/附件.xlsx",
             "seed_sha256": "621b0cb77367d3ac79488b0a553081ee854efcaea16c4d06c91074617e9d1633", "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        ],
        "legacy_tags": ["优化", "光学建模"],
    },
    {
        "problem_id": "cumcm-2023-B", "competition_id": "cumcm", "year": 2023, "problem_code": "B",
        "title": "多波束测线问题",
        "selection_basis": "旧 seed cumcm-2023-B；有 3 篇关联论文（B226/B311/B477）",
        "source": {
            "rel": "problems/12_CUMCM国赛/CUMCM2023Problems/B题/B题.pdf",
            "seed_sha256": "e709e066139ea0a4bb64aa7e56ea097725a1f2b4e5161fc8c7e50c48be079396",
            "media_type": "application/pdf",
            "text_repo_rel": "out/texts/cumcm-2023-B.txt",
            "text_seed_sha256": "abff3be12eeb166d74fbfaa1d5ba819a2f777294d8c9962f7eccc6a3ba6b3e77",
            "extract_unsupported": None,
        },
        "attachments": [
            {"rel": "problems/12_CUMCM国赛/CUMCM2023Problems/B题/result1.xlsx",
             "seed_sha256": "9a86f9f4b9755447bda0ab9c3d8f4123561a0bfa735ede7d0c08b1c457ae83bd", "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            {"rel": "problems/12_CUMCM国赛/CUMCM2023Problems/B题/result2.xlsx",
             "seed_sha256": "426941a7da69da8694d73be68ba82b976af675f57618154f4b4fc11d19f6dbd8", "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            {"rel": "problems/12_CUMCM国赛/CUMCM2023Problems/B题/附件.xlsx",
             "seed_sha256": "5f92dee1af5906869bd7dca45d73b90090bb971d882beb7d8461c6b563eee40a", "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        ],
        "legacy_tags": ["几何建模", "优化"],
    },
]

SAMPLE_CASES = [
    {"case_id": "cumcm-2018-B-03", "problem_id": "cumcm-2018-B", "competition_id": "cumcm",
     "title": "基于 0-1 规划的单 RGV 动态调度模型",
     "legacy_award": "优秀论文",
     "legacy_method_tags": ["0-1规划", "枚举与搜索", "目标规划"],
     "parent_evidence": {"year_code": "2018", "problem_code": "B", "basis": "文件名前缀 2018B + 标题『单 RGV 动态调度』与题名『智能RGV的动态调度策略』匹配"},
     "source": {"rel": "papers_pending_auth/12_CUMCM国赛/高教社杯全国大学生数学建模竞赛优秀论文/2018年高教社杯全国大学生数学建模竞赛优秀论文/2018B：基于 0-1 规划的单 RGV 动态调度模型.pdf",
                "seed_sha256": "8ef81da6379bbbda662f665c47019d7aa84ce521aec29284d5d12b96c43f851a"}},
    {"case_id": "cumcm-2023-A-092", "problem_id": "cumcm-2023-A", "competition_id": "cumcm",
     "title": "定日镜场的优化设计", "legacy_award": "优秀论文",
     "legacy_method_tags": ["单目标优化", "蒙特卡洛", "遗传算法", "枚举与搜索"],
     "parent_evidence": {"year_code": "2023", "problem_code": "A", "basis": "文件名 A092（2023 A 题）+ 标题『定日镜场』与题名匹配"},
     "source": {"rel": "papers_pending_auth/12_CUMCM国赛/高教社杯全国大学生数学建模竞赛优秀论文/2023年高教社杯全国大学生数学建模竞赛优秀论文/A092.pdf",
                "seed_sha256": "6f19a1e21443043cc30e3ca70ecf41dd41152205ffa143adbbd2e99fd307bc8e"}},
    {"case_id": "cumcm-2023-A-165", "problem_id": "cumcm-2023-A", "competition_id": "cumcm",
     "title": "基于机理分析法的定日镜场优化设计模型", "legacy_award": "优秀论文",
     "legacy_method_tags": ["单目标优化", "枚举与搜索", "蒙特卡洛"],
     "parent_evidence": {"year_code": "2023", "problem_code": "A", "basis": "文件名 A165（2023 A 题）+ 标题『定日镜场』与题名匹配"},
     "source": {"rel": "papers_pending_auth/12_CUMCM国赛/高教社杯全国大学生数学建模竞赛优秀论文/2023年高教社杯全国大学生数学建模竞赛优秀论文/A165.pdf",
                "seed_sha256": "63e129d60639f2a84beb094818290ab1035ddc0aa1d4b94c8189689d0523b401"}},
    {"case_id": "cumcm-2023-A-175", "problem_id": "cumcm-2023-A", "competition_id": "cumcm",
     "title": "定日镜场的优化设计模型", "legacy_award": "优秀论文",
     "legacy_method_tags": ["坐标变换", "枚举与搜索", "蒙特卡洛", "敏感性分析"],
     "parent_evidence": {"year_code": "2023", "problem_code": "A", "basis": "文件名 A175（2023 A 题）+ 标题『定日镜场』与题名匹配"},
     "source": {"rel": "papers_pending_auth/12_CUMCM国赛/高教社杯全国大学生数学建模竞赛优秀论文/2023年高教社杯全国大学生数学建模竞赛优秀论文/A175.pdf",
                "seed_sha256": "904914537e950e7faabbad018ebd1ecb410d5ff94d19731a79e2f3b971a28ac1"}},
    {"case_id": "cumcm-2023-B-226", "problem_id": "cumcm-2023-B", "competition_id": "cumcm",
     "title": "多波束测线布设", "legacy_award": "优秀论文",
     "legacy_method_tags": ["最小二乘法", "模拟退火", "贪心算法", "几何建模", "单目标优化", "枚举与搜索", "粒子群算法"],
     "parent_evidence": {"year_code": "2023", "problem_code": "B", "basis": "文件名 B226（2023 B 题）+ 标题『多波束测线』与题名匹配"},
     "source": {"rel": "papers_pending_auth/12_CUMCM国赛/高教社杯全国大学生数学建模竞赛优秀论文/2023年高教社杯全国大学生数学建模竞赛优秀论文/B226.pdf",
                "seed_sha256": "6575167cc2cace9fc4f7a2d9e654128803b4270096fcb2e63b27ef05faf7a33d"}},
    {"case_id": "cumcm-2023-B-311", "problem_id": "cumcm-2023-B", "competition_id": "cumcm",
     "title": "基于主要目标法的测线设计问题", "legacy_award": "优秀论文",
     "legacy_method_tags": ["几何建模", "多目标优化", "差分方程模型"],
     "parent_evidence": {"year_code": "2023", "problem_code": "B", "basis": "文件名 B311（2023 B 题）+ 标题『测线设计』与题名匹配"},
     "source": {"rel": "papers_pending_auth/12_CUMCM国赛/高教社杯全国大学生数学建模竞赛优秀论文/2023年高教社杯全国大学生数学建模竞赛优秀论文/B311.pdf",
                "seed_sha256": "b939b88771b288865fef612c613eb1d6a5890049fade34578ba35a66a4e95095"}},
    {"case_id": "cumcm-2023-B-477", "problem_id": "cumcm-2023-B", "competition_id": "cumcm",
     "title": "多波束测深合理探测方案的设计及效果分析", "legacy_award": "优秀论文",
     "legacy_method_tags": ["贪心算法", "随机森林", "插值法", "最短路"],
     "parent_evidence": {"year_code": "2023", "problem_code": "B", "basis": "文件名 B477（2023 B 题）+ 标题『多波束测深』与题名匹配"},
     "source": {"rel": "papers_pending_auth/12_CUMCM国赛/高教社杯全国大学生数学建模竞赛优秀论文/2023年高教社杯全国大学生数学建模竞赛优秀论文/B477.pdf",
                "seed_sha256": "07329ac31d14177997c01035dcac29e1c03243ad71cb7ccaa381cf016595e1c6"}},
]


# --------------------------------------------------------------------------- #
# 基础工具                                                                    #
# --------------------------------------------------------------------------- #
import importlib.util
import subprocess
import re
SCRIPT_DIR = Path(os.path.abspath(__file__)).parent
IO_SPEC=importlib.util.spec_from_file_location('stage03_fileio',str(SCRIPT_DIR/'stage03_fileio.py'))
fileio=importlib.util.module_from_spec(IO_SPEC);IO_SPEC.loader.exec_module(fileio)
sha256_of=fileio.sha256_of
stat_dict=fileio.stat_file
hash_verified=fileio.hash_verified
copy_verified=fileio.copy_verified
TOOL_VERSION='normalize_cumcm_sample/2.0'


def load_seed_records():
    problems = {}
    cases = {}
    pp = json.loads((CONTENT_DIR / "out" / "pack" / "problems.json").read_text(encoding="utf-8"))
    for rec in pp:
        problems[rec["business_id"]] = rec
    cc = json.loads((CONTENT_DIR / "out" / "pack" / "cases.json").read_text(encoding="utf-8"))
    for rec in cc:
        cases[rec["business_id"]] = rec
    return problems, cases


# --------------------------------------------------------------------------- #
# 结构生成                                                                    #
# --------------------------------------------------------------------------- #
def empty_license():
    return {"internal_analysis": False, "distribute_original": False,
            "distribute_profile": False, "send_to_third_party_model": False}


def empty_length():
    return {"zh_chars": None, "en_words": None,
            "zh_parts": {"body": None, "abstract": None, "toc": None, "appendix": None},
            "en_parts": {"body": None, "abstract": None, "toc": None, "appendix": None},
            "section_ratios": [], "statistical_basis": None, "missing": ["正文未抽取"]}


def build_problem_profile(p, source_sha256, source_asset_id):
    return {
        "problem_id": p["problem_id"],
        "competition_id": p["competition_id"],
        "year": p["year"],
        "problem_code": p["problem_code"],
        "round": None,
        "track": None,
        "language": "zh",
        "problem_types": ["unknown"],
        "method_tags": [],
        "data_tags": [],
        "domain_tags": [],
        "confidence": None,
        "label_status": "unknown",
        "provenance": {"kind": "import", "evidence": None,
                       "note": "继承自旧 seed 十二键记录；题型/子问题未核验，留待阶段四"},
        "subproblems": [],
        "source_refs": [p["source"]["rel"]],
        "source_sha256": source_sha256,
        "source_asset_ref": {"asset_id": source_asset_id, "version": 1, "sha256": source_sha256} if source_sha256 else None,
        "attachments": [a["rel"] for a in p["attachments"]],
        "schema_version": 1,
        "profile_version": 1,
        "review_status": "pending_review",
    }


def build_case_profile(c, paper_sha256, paper_asset_id):
    return {
        "case_id": c["case_id"],
        "problem_id": c["problem_id"],
        "resolution_status": "resolved",
        "publication_status": "staging",
        "competition_id": c["competition_id"],
        "source_sha256": paper_sha256,
        "source_asset_ref": {"asset_id": paper_asset_id, "version": 1, "sha256": paper_sha256} if paper_sha256 else None,
        "extractor_version": None,
        "schema_version": 1,
        "profile_version": 1,
        "review_status": "pending_review",
        "license": empty_license(),
        "license_evidence": None,
        "method_tags": [],
        "structure": [],
        "writing": [],
        "length": empty_length(),
        "figures": [],
    }


def build_asset(asset_id, role, owner_id, competition_id, oss_key, sha256, size_bytes, media_type):
    return {
        "asset_id": asset_id,
        "role": role,
        "competition_id": competition_id,
        "owner_id": owner_id,
        "version": 1,
        "oss_key": oss_key,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "media_type": media_type,
        "license_status": "unknown",
        "license": empty_license(),
        "license_evidence": None,
        "schema_version": 1,
    }


def asset_id_for(role, owner_id, rel):
    # 稳定且唯一：由 role + owner + 源相对路径的短 hash 决定。身份不依赖内容 hash，
    # 内容变化只改 sha256 不改 asset_id；字节相同但路径不同的附件也得到不同 asset_id。
    digest = hashlib.sha256(str(rel).encode("utf-8")).hexdigest()[:12]
    return "{}-{}-{}".format(owner_id, role, digest)


# --------------------------------------------------------------------------- #
# 主体                                                                        #
# --------------------------------------------------------------------------- #
def json_bytes(value):
    return (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8')


def json_read(path):
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise ValueError('duplicate JSON key: '+key)
            result[key]=value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'),object_pairs_hook=pairs)


def select_samples(problem_ids=None,case_ids=None):
    known_p={p['problem_id']:p for p in SAMPLE_PROBLEMS};known_c={c['case_id']:c for c in SAMPLE_CASES}
    ids=problem_ids if problem_ids is not None else list(known_p)
    if not ids or len(ids)!=len(set(ids)) or set(ids)-set(known_p):raise ValueError('invalid/duplicate problem selection')
    chosen=[p for p in SAMPLE_PROBLEMS if p['problem_id'] in ids]
    cids=case_ids if case_ids is not None else [c['case_id'] for c in SAMPLE_CASES if c['problem_id'] in ids]
    if len(cids)!=len(set(cids)) or set(cids)-set(known_c):raise ValueError('invalid/duplicate case selection')
    if any(known_c[c]['problem_id'] not in ids for c in cids):raise ValueError('selected case parent outside selected problems')
    return chosen,[c for c in SAMPLE_CASES if c['case_id'] in cids]


def disjoint_roots(data_root,out_root):
    data_root=fileio.no_links(data_root);out_root=fileio.no_links(out_root)
    try:common=os.path.commonpath([str(data_root),str(out_root)])
    except ValueError:common=None  # Distinct Windows drives are disjoint.
    if common in [str(data_root),str(out_root)]:raise ValueError('source and output roots must be disjoint')
    return data_root,out_root


def inventory_hashes(paths):
    found={}
    with open(CONTENT_DIR/'out/inventory/source_files.jsonl','r',encoding='utf-8') as stream:
        for line in stream:
            row=json.loads(line)
            if row['relative_path'] in paths:
                if row['status']!='ok':raise ValueError('selected inventory record is not usable')
                if row['relative_path'] in found:raise ValueError('duplicate inventory path')
                found[row['relative_path']]=row['sha256']
    return found


def validate_contract(bundle):
    spec=importlib.util.spec_from_file_location('stage03_contracts',str(SCRIPT_DIR/'validate_content_contracts.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    _,*vv=module.load_jsonschema();module._v=tuple(vv)
    store,schemas=module.load_store(module.SCHEMAS_DIR)
    taxonomy=json_read(module.DEFAULT_TAXONOMY)
    return module.validate_bundle(bundle,store,schemas,taxonomy)


def read_parent_pages(requests):
    if not requests:return {'tool':'none','version':None,'results':{}}
    run=subprocess.run([sys.executable,str(SCRIPT_DIR/'read_pdf_identity.py')],input=json.dumps(requests),
                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf-8',
                       env=dict(os.environ,PYTHONIOENCODING='utf-8'),timeout=90)
    if run.returncode:raise ValueError('local PDF identity reader unavailable: '+run.stderr[-1200:])
    return json.loads(run.stdout)


def parent_decision(case,paper_hash,parent_hash,pages,ledger):
    entry=ledger.get(case['case_id'])
    if entry is None:return None,'no reviewed parent evidence'
    if (entry['source_rel'],entry['source_sha256'],entry['problem_id'],entry['problem_source_sha256'])!=(case['source']['rel'],paper_hash,case['problem_id'],parent_hash):
        return None,'parent evidence does not match source/version'
    page=pages['results'].get(case['case_id'])
    if page is None or page['page']!=entry['page']:return None,'identity page unavailable'
    if page['source_sha256']!=paper_hash:return None,'identity reader bytes differ from source lock'
    text=''.join(page['text'].split())
    if ''.join(entry['title_excerpt'].split()) not in text or not all(''.join(term.split()) in text for term in entry['body_terms']):
        return None,'title/abstract not supported by source page'
    expected=case['problem_id'].split('-')
    if entry['year']!=int(expected[1]) or entry['problem_code']!=expected[2]:return None,'year/code conflict'
    return {**entry,'tool':pages['tool'],'tool_version':pages['version'],
            'page_text_sha256':hashlib.sha256(page['text'].encode('utf-8')).hexdigest()},None


def run_normalize(data_root,out_root,problem_ids=None,case_ids=None):
    data_root,out_root=disjoint_roots(data_root,out_root)
    problems,cases=select_samples(problem_ids,case_ids)
    problems_seed,cases_seed=load_seed_records()
    ledger={e['case_id']:e for e in json_read(CONTENT_DIR/'config/stage03_parent_evidence.json')['records']}
    paths={p['source']['rel'] for p in problems}|{a['rel'] for p in problems for a in p['attachments']}|{c['source']['rel'] for c in cases}
    inventory=inventory_hashes(paths)
    out_root.mkdir(parents=True,exist_ok=True)
    report={'tool_version':TOOL_VERSION,'data_root':str(data_root),'out_root':str(out_root),'problem_count':len(problems),'case_count':len(cases),
            'files':[],'conflicts':[],'unresolved':[],'extractions':[],'errors':[]}
    assets=[];provenance=[];proposals={};bundle_problems=[];bundle_cases=[];problem_sources={};case_sources={}

    def error(code,message):report['errors'].append({'code':code,'message':message})
    def propose(rel,data):
        if rel in proposals and proposals[rel]!=data:raise ValueError('duplicate output path: '+rel)
        fileio.relative(out_root,rel);proposals[rel]=data
    def copied(source,local,kind,role,owner,competition,media):
        source_path=fileio.relative(data_root,source['rel']);destination=fileio.relative(out_root,local)
        result=copy_verified(source_path,destination)
        result.update(kind=kind,source_rel=source['rel'],local_rel=local,seed_sha256=source['seed_sha256'],inventory_sha256=inventory.get(source['rel']))
        result['seed_match']=result.get('src_sha256')==source['seed_sha256']
        result['inventory_match']=result.get('src_sha256')==inventory.get(source['rel']) and source['rel'] in inventory
        report['files'].append(result)
        if result['conflict'] or not result['seed_match'] or not result['inventory_match']:
            report['conflicts'].append(result);error('SOURCE_OR_COPY',source['rel']);return None,None
        aid=asset_id_for(role,owner,source['rel']);digest=result['src_sha256']
        assets.append(build_asset(aid,role,owner,competition,source['rel'],digest,result['size'],media))
        provenance.append({'source_rel':source['rel'],'local_rel':local,'asset_id':aid,'sha256':digest,'size_bytes':result['size'],'kind':kind,'owner_id':owner,
                           'seed_sha256':source['seed_sha256'],'inventory_sha256':inventory[source['rel']]})
        return digest,aid

    for p in problems:
        pid=p['problem_id'];base='problems/'+pid
        digest,aid=copied(p['source'],base+'/source/'+Path(p['source']['rel']).name,'problem_source','problem',pid,p['competition_id'],p['source']['media_type'])
        problem_sources[pid]=digest
        for a in p['attachments']:copied(a,base+'/attachments/'+Path(a['rel']).name,'attachment','attachment',pid,p['competition_id'],a['media_type'])
        profile=build_problem_profile(p,digest,aid)
        bundle_problems.append(profile);propose(base+'/profile.json',json_bytes(profile))
        record=problems_seed.get(pid)
        if record is None:error('SEED_RECORD','missing problem '+pid)
        else:propose(base+'/record.json',json_bytes(record))
        text_path=fileio.relative(CONTENT_DIR,p['source']['text_repo_rel']);info=hash_verified(text_path)
        extraction={'problem_id':pid,'tool':'verified_seed_text_import','tool_version':TOOL_VERSION,'source_sha256':digest,
                    'cache_sha256':info['sha256'],'output_sha256':None,'text_seed_match':False,'status':'failed',
                    'upstream_tool_version':None,'unsupported':p['source']['extract_unsupported']}
        try:
            if info['status']!='ok' or info['sha256']!=p['source']['text_seed_sha256']:raise ValueError('cached text missing/empty/hash differs')
            data=text_path.read_bytes();text=data.decode('utf-8');after=hash_verified(text_path)
            if hashlib.sha256(data).hexdigest()!=info['sha256'] or after.get('signature')!=info.get('signature'):raise ValueError('cache changed while reading')
            flat=''.join(text.split())
            if len(flat)<80 or '\ufffd' in text or '\x00' in text or ''.join(p['title'].split()) not in flat:raise ValueError('cached text empty/garbled/title mismatch')
            propose(base+'/extracted/prompt.zh.txt',data)
            extraction.update(output_sha256=info['sha256'],text_seed_match=True,status='verified_reuse')
        except (OSError,ValueError,UnicodeError) as exc:error('TEXT_INPUT',pid+': '+str(exc))
        report['extractions'].append(extraction);propose(base+'/extracted/diagnostics.json',json_bytes(extraction))

    for c in cases:
        cid=c['case_id'];digest,aid=copied(c['source'],'cases/'+cid+'/source/'+Path(c['source']['rel']).name,'case_paper','case_paper',cid,c['competition_id'],'application/pdf')
        case_sources[cid]=(digest,aid)
    requests=[{'case_id':c['case_id'],'path':str(fileio.relative(out_root,'cases/'+c['case_id']+'/source/'+Path(c['source']['rel']).name)),
               'page':ledger[c['case_id']]['page']} for c in cases if case_sources[c['case_id']][0] and c['case_id'] in ledger]
    try:pages=read_parent_pages(requests)
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        pages={'tool':'unavailable','version':None,'results':{}};error('PDF_IDENTITY',str(exc))
    for c in cases:
        cid=c['case_id'];digest,aid=case_sources[cid]
        if cid not in cases_seed:error('SEED_RECORD','missing case '+cid)
        evidence,reason=parent_decision(c,digest,problem_sources.get(c['problem_id']),pages,ledger)
        profile=build_case_profile(c,digest,aid)
        if reason:
            profile.update(problem_id=None,resolution_status='unresolved');report['unresolved'].append({'case_id':cid,'reason':reason});error('PARENT_UNRESOLVED',cid+': '+reason)
        metadata={'case_id':cid,'problem_id':profile['problem_id'],'competition_id':c['competition_id'],'resolution_status':profile['resolution_status'],
                  'title':evidence['title_excerpt'] if evidence else None,'parent_evidence':evidence,
                  'source':{'rel':c['source']['rel'],'sha256':digest,'local_rel':'cases/'+cid+'/source/'+Path(c['source']['rel']).name},
                  'legacy':cases_seed.get(cid),'legacy_method_tags_chinese':cases_seed.get(cid,{}).get('method_tags',[]),
                  'note':'已核对题名与摘要主题；年份/题号由来源目录与题面交叉核对；未核定奖项/授权/完整画像'}
        propose('cases/'+cid+'/metadata.json',json_bytes(metadata));propose('cases/'+cid+'/profile.json',json_bytes(profile));bundle_cases.append(profile)
    bundle={'problems':bundle_problems,'cases':bundle_cases,'assets':assets,'is_fixture':False}
    try:contract_errors=validate_contract(bundle)
    except (OSError,ValueError,RuntimeError,ImportError) as exc:contract_errors=[str(exc)]
    for item in contract_errors:error('CONTRACT',item)
    propose('assets.json',json_bytes(assets));propose('bundle.json',json_bytes(bundle));propose('provenance.json',json_bytes(provenance))
    # Compare all proposed business bytes before writing any derived file.
    for rel,data in proposals.items():
        target=fileio.relative(out_root,rel)
        if target.exists() and target.read_bytes()!=data:error('OUTPUT_CONFLICT',rel+' preserved; use a fresh snapshot')
    report['qc']={'total_files':len(report['files']),'conflicts':len(report['conflicts']),'seed_mismatches':[r['source_rel'] for r in report['files'] if not r['seed_match']],
                  'unresolved':report['unresolved'],'problems_resolved':len(problems),'cases_resolved':sum(c['resolution_status']=='resolved' for c in bundle_cases),
                  'verified_texts':sum(e['status']=='verified_reuse' for e in report['extractions']),'contract_errors':contract_errors,'passed':not report['errors']}
    if report['qc']['passed']:
        # Bundle is committed last; no downstream index is published for a failed attempt.
        for rel,data in sorted(proposals.items(),key=lambda item:item[0]=='bundle.json'):fileio.write_exact(fileio.relative(out_root,rel),data)
        manifest={'schema_version':1,'tool_version':TOOL_VERSION,'scope':'cumcm stage03 selected-version snapshot','is_fixture':False,
                  'problem_ids':[p['problem_id'] for p in problems],'case_ids':[c['case_id'] for c in cases],
                  'outputs':{rel:hashlib.sha256(data).hexdigest() for rel,data in sorted(proposals.items())},'asset_local_bindings':provenance,
                  'parent_evidence_config_sha256':sha256_of(CONTENT_DIR/'config/stage03_parent_evidence.json'),
                  'note':'licenses false; profiles not yet extracted; planned oss_key is separate from verified local_rel'}
        fileio.write_exact(out_root/'sample_manifest.json',json_bytes(manifest))
        report['read_only_verification']=check_normalized(data_root,out_root)
        if not report['read_only_verification']['passed']:
            report['qc']['passed']=False;error('POST_WRITE_QC','read-only verification failed')
    # Diagnostics are deliberately separate from immutable business records.
    diagnostic=out_root/('normalization_report.json' if report['qc']['passed'] else 'last_failed_attempt.json')
    fileio.no_links(diagnostic);diagnostic.write_bytes(json_bytes(report))
    return report,bundle


def check_normalized(data_root,out_root):
    data_root,out_root=disjoint_roots(data_root,out_root);errors=[]
    def error(code,message):errors.append({'code':code,'message':message})
    try:
        manifest=json_read(fileio.relative(out_root,'sample_manifest.json'))
        bundle=json_read(fileio.relative(out_root,'bundle.json'));assets=json_read(fileio.relative(out_root,'assets.json'))
        problems,cases=select_samples(manifest['problem_ids'],manifest['case_ids'])
        expected=len(problems)+sum(len(p['attachments']) for p in problems)+len(cases)
        required={'bundle.json','assets.json','provenance.json'}
        for p in problems:
            required.update('problems/'+p['problem_id']+'/'+name for name in ['profile.json','record.json','extracted/prompt.zh.txt','extracted/diagnostics.json'])
        for c in cases:required.update('cases/'+c['case_id']+'/'+name for name in ['profile.json','metadata.json'])
        if set(manifest['outputs'])!=required:error('OUTPUT_COVERAGE','manifest does not cover all required business files')
        if len(assets)!=expected or assets!=bundle['assets']:error('ASSET_COVERAGE','asset count/bundle differs')
        asset_map={a['asset_id']:a for a in assets}
        bindings=manifest['asset_local_bindings']
        if len(bindings)!=expected or len({b['local_rel'] for b in bindings})!=expected:error('LOCAL_BINDING','duplicate/missing local mapping')
        inventory=inventory_hashes({b['source_rel'] for b in bindings})
        for binding in bindings:
            source=hash_verified(fileio.relative(data_root,binding['source_rel']));local=hash_verified(fileio.relative(out_root,binding['local_rel']))
            asset=asset_map.get(binding['asset_id'])
            if source['status']!='ok' or local['status']!='ok' or source['sha256']!=binding['sha256'] or local['sha256']!=binding['sha256'] or local['size']!=binding['size_bytes']:
                error('BYTE_QC',binding['local_rel'])
            if binding['sha256']!=binding['seed_sha256'] or inventory.get(binding['source_rel'])!=binding['sha256']:error('BASELINE_HASH',binding['source_rel'])
            if asset is None or (asset['sha256'],asset['size_bytes'],asset['owner_id'],asset['oss_key'])!=(binding['sha256'],binding['size_bytes'],binding['owner_id'],binding['source_rel']):error('ASSET_LOCAL_BINDING',binding['asset_id'])
        if json_read(fileio.relative(out_root,'provenance.json'))!=bindings:error('PROVENANCE','manifest/provenance local bindings differ')
        for rel,digest in manifest['outputs'].items():
            target=fileio.relative(out_root,rel);info=hash_verified(target)
            if info['status']!='ok' or info['sha256']!=digest:error('OUTPUT_HASH',rel)
        for issue in validate_contract(bundle):error('CONTRACT',issue)
        if sha256_of(CONTENT_DIR/'config/stage03_parent_evidence.json')!=manifest['parent_evidence_config_sha256']:error('EVIDENCE_CONFIG','evidence snapshot changed')
        ledger={e['case_id']:e for e in json_read(CONTENT_DIR/'config/stage03_parent_evidence.json')['records']}
        requests=[{'case_id':c['case_id'],'path':str(fileio.relative(out_root,'cases/'+c['case_id']+'/source/'+Path(c['source']['rel']).name)),'page':ledger[c['case_id']]['page']} for c in cases if c['case_id'] in ledger]
        pages=read_parent_pages(requests);parent_hashes={p['problem_id']:p['source_sha256'] for p in bundle['problems']};case_hashes={c['case_id']:c['source_sha256'] for c in bundle['cases']}
        for c in cases:
            proof,reason=parent_decision(c,case_hashes[c['case_id']],parent_hashes.get(c['problem_id']),pages,ledger)
            metadata=json_read(fileio.relative(out_root,'cases/'+c['case_id']+'/metadata.json'))
            if reason or metadata['parent_evidence']!=proof or metadata['problem_id']!=c['problem_id']:error('PARENT_EVIDENCE',c['case_id'])
            profile=json_read(fileio.relative(out_root,'cases/'+c['case_id']+'/profile.json'))
            if profile!=next((item for item in bundle['cases'] if item['case_id']==c['case_id']),None):error('PROFILE_SIDECAR',c['case_id'])
        pp,_=load_seed_records()
        for p in problems:
            record=json_read(fileio.relative(out_root,'problems/'+p['problem_id']+'/record.json'))
            if record!=pp[p['problem_id']] or len(record)!=12:error('LEGACY_RECORD',p['problem_id'])
            profile=json_read(fileio.relative(out_root,'problems/'+p['problem_id']+'/profile.json'))
            if profile!=next((item for item in bundle['problems'] if item['problem_id']==p['problem_id']),None):error('PROFILE_SIDECAR',p['problem_id'])
            prompt=hash_verified(fileio.relative(out_root,'problems/'+p['problem_id']+'/extracted/prompt.zh.txt'))
            if prompt['sha256']!=p['source']['text_seed_sha256']:error('TEXT_HASH',p['problem_id'])
        return {'passed':not errors,'errors':errors,'source_files':expected,'verified_local_resources':len(bindings),'problems':len(problems),'cases':len(cases),'read_only':True}
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,ImportError,subprocess.SubprocessError) as exc:
        error('INPUT_OR_QC',str(exc));return {'passed':False,'errors':errors,'read_only':True}


def main():
    parser=argparse.ArgumentParser(description='Stage03 verified small sample; --check is read-only')
    parser.add_argument('--data-root',type=Path,default=Path('D:/Erdos_data'))
    parser.add_argument('--out',type=Path,default=CONTENT_DIR/'normalized/stage03/cumcm')
    parser.add_argument('--problems',nargs='+');parser.add_argument('--cases',nargs='*')
    parser.add_argument('--check',action='store_true',help='read-only source/copy/text/evidence/contract QC; no normalization')
    parser.add_argument('--json-report',type=Path)
    args=parser.parse_args()
    try:
        if args.json_report:
            report_path=fileio.no_links(args.json_report)
            for protected in [args.data_root,args.out]:
                base=fileio.no_links(protected)
                try:overlap=os.path.commonpath([str(base),str(report_path)])==str(base)
                except ValueError:overlap=False
                if overlap:raise ValueError('JSON report must be outside source and sample roots')
        if args.check:
            if args.problems or args.cases is not None:raise ValueError('--check uses selection locked in manifest')
            report=check_normalized(args.data_root,args.out);passed=report['passed']
        else:
            report,_=run_normalize(args.data_root,args.out,args.problems,args.cases);passed=report['qc']['passed']
        if args.json_report:
            fileio.no_links(args.json_report);args.json_report.parent.mkdir(parents=True,exist_ok=True);args.json_report.write_bytes(json_bytes(report))
        print(json.dumps(report if args.check else report['qc'],ensure_ascii=False,indent=2))
        if not passed and not args.check:print(json.dumps(report['errors'],ensure_ascii=False,indent=2))
        return 0 if passed else 1
    except (OSError,ValueError,KeyError,RuntimeError,ImportError,subprocess.SubprocessError) as exc:
        print('[NORMALIZATION_ERROR] '+str(exc));return 1


if __name__=='__main__':sys.exit(main())
