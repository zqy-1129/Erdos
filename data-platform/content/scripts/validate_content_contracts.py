"""内容契约校验工具（阶段02）：JSON Schema 校验 + 跨记录语义校验。

用法（工作目录 D:/Erdos/data-platform/content）：
    <pytorch>/python scripts/validate_content_contracts.py --check-schemas
    <pytorch>/python scripts/validate_content_contracts.py --check-registry config/competition_registry.json
    <pytorch>/python scripts/validate_content_contracts.py --check-taxonomy config/taxonomy_v1.json
    <pytorch>/python scripts/validate_content_contracts.py --check-sources out/inventory/source_files.jsonl
    <pytorch>/python scripts/validate_content_contracts.py --check-examples examples/contracts/v1
    <pytorch>/python scripts/validate_content_contracts.py --all

退出码：0=全部通过；1=有 schema/语义/输入错误；2=工作区校验依赖缺失。

依赖仅从工作区 .deps/schema_validation/ 加载，版本见 dependencies.lock.json。
不借用 E 盘环境中的第三方包。$ref 均在本地 store 解析，不联网。
"""

import argparse
import json
import os
import sys
import importlib.util
import importlib.metadata
from datetime import datetime
from urllib.parse import urljoin, urldefrag
from pathlib import Path

SCRIPT_DIR = Path(os.path.abspath(__file__)).parent
CONTENT_DIR = SCRIPT_DIR.parent
DEPS_DIR = CONTENT_DIR / ".deps" / "schema_validation"
SCHEMAS_DIR = CONTENT_DIR / "schemas" / "v1"
DEFAULT_REGISTRY = CONTENT_DIR / "config" / "competition_registry.json"
DEFAULT_TAXONOMY = CONTENT_DIR / "config" / "taxonomy_v1.json"
DEFAULT_SOURCES = CONTENT_DIR / "out" / "inventory" / "source_files.jsonl"
DEFAULT_EXAMPLES = CONTENT_DIR / "examples" / "contracts" / "v1"
_sem_spec = importlib.util.spec_from_file_location('content_semantics', str(SCRIPT_DIR / 'content_semantics.py'))
semantics = importlib.util.module_from_spec(_sem_spec)
_sem_spec.loader.exec_module(semantics)

COMPETITION_IDS = ["mcm", "huashu", "statistics", "mathorcup", "renzheng", "teddy", "wuyi",
                   "electrician", "apmcm_zh", "zhongqing", "shenzhen", "cumcm", "cpmcm",
                   "shuwei", "apmcm_en", "xiaomeisai", "yangtze", "dongbei", "huazhong"]


def parse_json(text):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError('duplicate JSON key: '+key)
            result[key]=value
        return result
    def invalid(value): raise ValueError('non-JSON number: '+value)
    return json.loads(text,object_pairs_hook=pairs,parse_constant=invalid)


class DependencyError(RuntimeError):
    pass


def load_jsonschema(deps_dir=None):
    deps = Path(deps_dir) if deps_dir else DEPS_DIR
    if not (deps / 'jsonschema').is_dir():
        raise DependencyError('工作区依赖缺失：{}'.format(deps))
    sys.path.insert(0, str(deps))
    os.environ['PYRSISTENT_NO_C_EXTENSION'] = '1'
    try:
        from jsonschema import Draft7Validator, RefResolver  # noqa
        import jsonschema
    except ImportError as exc:
        raise DependencyError(
            "缺少 jsonschema 依赖：无法从 {deps} 导入。请运行 "
            "`scripts/bootstrap_schema_validation.py` 恢复锁定依赖，或按 requirements-schema-validation.txt 安装到工作区。原始异常：{exc}".format(
                deps=deps, exc=exc))
    origin = os.path.abspath(jsonschema.__file__)
    if os.path.commonpath([origin, os.path.abspath(str(deps))]) != os.path.abspath(str(deps)):
        raise DependencyError('jsonschema 必须来自指定工作区依赖目录：'+origin)
    for module_name in ['attr','attrs','pyrsistent','importlib_resources','zipp']:
        try: dependency_module = __import__(module_name)
        except ImportError as exc: raise DependencyError('工作区依赖不完整：'+module_name) from exc
        module_path = os.path.abspath(dependency_module.__file__)
        if os.path.commonpath([module_path, os.path.abspath(str(deps))]) != os.path.abspath(str(deps)):
            raise DependencyError('依赖模块必须来自工作区：'+module_path)
    lock = json.loads((CONTENT_DIR / 'dependencies.lock.json').read_text(encoding='utf-8'))
    for package, version in lock['packages'].items():
        try: actual_version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise DependencyError('依赖元数据缺失：'+package) from exc
        if actual_version != version:
            raise DependencyError('依赖版本不匹配：{} requires {}'.format(package,version))
    from jsonschema import FormatChecker
    fmt = FormatChecker()
    @fmt.checks('date-time', raises=(ValueError, TypeError))
    def valid_timestamp(value):
        if not isinstance(value,str): return True
        import re
        if not re.match(r'^\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d(?:\.\d+)?(?:[Zz]|[+-]\d\d:\d\d)$',value): return False
        if value[-1].upper() != 'Z' and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59): return False
        return datetime.fromisoformat(value.upper().replace('Z','+00:00')).tzinfo is not None
    return jsonschema, Draft7Validator, RefResolver, fmt


def _canonical_name(filename):
    if filename.endswith(".schema.json"):
        return filename[: -len(".schema.json")]
    if filename.endswith(".json"):
        return filename[: -len(".json")]
    return filename


def load_store(schemas_dir):
    """载入所有 schema 文件，返回 (store, name_to_schema)。store 以 $id 为键。"""
    store = {}
    by_name = {}
    for p in sorted(Path(schemas_dir).glob("*.json")):
        try:
            schema = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("schema 读取失败 {}：{}".format(p, exc))
        sid = schema.get("$id")
        if sid:
            if sid in store: raise ValueError('duplicate schema $id: '+sid)
            store[sid] = schema
        by_name[_canonical_name(p.name)] = schema
    if not store: raise ValueError('empty schema directory')
    def references(node,base):
        if isinstance(node,dict):
            if '$ref' in node:
                target,fragment=urldefrag(urljoin(base,node['$ref']))
                if target not in store: raise ValueError('nonlocal/missing $ref: '+node['$ref'])
                # Resolve every fragment too; valid schema syntax alone does not check references.
                from jsonschema import RefResolver
                RefResolver(base_uri=target,referrer=store[target],store=store).resolve_fragment(store[target],fragment)
            for value in node.values(): references(value,base)
        elif isinstance(node,list):
            for value in node: references(value,base)
    for sid,schema in store.items(): references(schema,sid)
    return store, by_name


def meta_check(schemas_dir, Draft7Validator):
    """自校验：每个 schema 是合法 Draft-7。返回错误列表。"""
    errors = []
    for p in sorted(Path(schemas_dir).glob("*.json")):
        try:
            schema = json.loads(p.read_text(encoding="utf-8"))
            Draft7Validator.check_schema(schema)
        except Exception as exc:
            errors.append("schema 自校验失败 {}：{}".format(p.name, exc))
    return errors


def make_validator(schema, store, Draft7Validator, RefResolver, draft7_format_checker):
    base = schema.get("$id", "https://erdos.local/content/schemas/v1/root.json")
    def blocked(uri): raise ValueError('nonlocal $ref blocked: '+uri)
    resolver = RefResolver(base_uri=base, referrer=schema, store=store, handlers={p:blocked for p in ['http','https','file','ftp']})
    return Draft7Validator(schema, resolver=resolver, format_checker=draft7_format_checker)


def schema_error_strings(validator, doc):
    """返回 [(json_path, message)]。"""
    out = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: tuple(str(p) for p in e.absolute_path)):
        path = "/".join(str(x) for x in err.absolute_path) or "(root)"
        out.append((path, err.message))
    return out


def check_registry(path, store, by_name):
    validator = make_validator(by_name["competition_registry"], store, *_v)
    doc = parse_json(Path(path).read_text(encoding="utf-8"))
    issues = ["{}: {}".format(p, m) for p, m in schema_error_strings(validator, doc)]
    if issues: return issues, 0
    # 语义：19 个且 id/目录名唯一
    comps = doc.get("competitions", [])
    ids = [c.get("competition_id") for c in comps]
    dirs = [c.get("source_dir_name") for c in comps]
    if len(comps) != 19:
        issues.append("(semantic) 赛事数量非 19：{}".format(len(comps)))
    if len(set(ids)) != len(ids):
        issues.append("(semantic) competition_id 重复")
    if len(set(dirs)) != len(dirs):
        issues.append("(semantic) source_dir_name 重复")
    unknown = [i for i in ids if i not in COMPETITION_IDS]
    if unknown:
        issues.append("(semantic) 出现未注册 competition_id：{}".format(unknown))
    return issues, len(comps)


def check_taxonomy(path, store, by_name):
    validator = make_validator(by_name["taxonomy"], store, *_v)
    doc = parse_json(Path(path).read_text(encoding="utf-8"))
    issues = ["[SCHEMA] {}: {}".format(p, m) for p, m in schema_error_strings(validator, doc)]
    if not issues:
        issues.extend(semantics.taxonomy_issues(doc, (CONTENT_DIR/'config/method_tags_v0.yaml').read_text(encoding='utf-8')))
    return issues


def check_sources(path, store, by_name):
    validator = make_validator(by_name["source_file"], store, *_v)
    issues = []
    total = 0
    ok = 0
    seen = set()
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                doc = parse_json(line)
            except ValueError as exc:
                issues.append("第{}行 JSON 解析失败：{}".format(lineno, exc))
                continue
            errs = schema_error_strings(validator, doc)
            if isinstance(doc,dict) and isinstance(doc.get('relative_path'),str):
                if doc['relative_path'] in seen: errs.append(('relative_path','[DUPLICATE_PATH] duplicate source record'))
                seen.add(doc['relative_path'])
            if errs:
                issues.append("第{}行 {}: {}".format(
                    lineno, doc.get("relative_path", "?") if isinstance(doc,dict) else '?', "; ".join("{} {}".format(p, m) for p, m in errs[:3])))
            else:
                ok += 1
    if total == 0: issues.append('[EMPTY_INPUT] no source records')
    return issues, total, ok


def validate_bundle(bundle, store, by_name, taxonomy):
    validator = make_validator(by_name['bundle'], store, *_v)
    issues = ['[SCHEMA] /{}: {}'.format(p,m) for p,m in schema_error_strings(validator,bundle)]
    if issues: return issues
    return semantics.validate_graph(bundle,taxonomy)


def check_examples(examples_dir, store, by_name, taxonomy):
    ex_dir = Path(examples_dir)
    manifest = json.loads((ex_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get('$fixture') is not True: raise ValueError('example manifest must be marked as fixture')
    for group in ['positive','negative']:
        for fname in manifest.get(group,{}):
            if Path(fname).name != fname or ':' in fname or '\\' in fname: raise ValueError('invalid fixture filename: '+fname)
    pos_dir = ex_dir / "positive"
    neg_dir = ex_dir / "negative"
    pos_results = []
    neg_results = []
    for fname, meta in manifest.get("positive", {}).items():
        doc = parse_json((pos_dir / fname).read_text(encoding="utf-8"))
        schema_name = meta["schema"]
        if schema_name == "bundle":
            issues = validate_bundle(doc, store, by_name, taxonomy)
        else:
            validator = make_validator(by_name[schema_name], store, *_v)
            issues = ["{}: {}".format(p, m) for p, m in schema_error_strings(validator, doc)]
        pos_results.append((fname, issues))
    for fname, meta in manifest.get("negative", {}).items():
        doc = parse_json((neg_dir / fname).read_text(encoding="utf-8"))
        expected = meta.get("expected_error", "")
        kind = meta.get("kind", "schema")
        if kind == "bundle":
            issues = validate_bundle(doc, store, by_name, taxonomy)
        else:
            schema_name = meta["schema"]
            validator = make_validator(by_name[schema_name], store, *_v)
            issues = ["{}: {}".format(p, m) for p, m in schema_error_strings(validator, doc)]
        neg_results.append((fname, expected, issues))
    return pos_results, neg_results, manifest


def main():
    parser = argparse.ArgumentParser(description='Erdos local content contracts v1')
    parser.add_argument('--check-schemas',action='store_true')
    for name in ['registry','taxonomy','sources','examples','bundle']:
        parser.add_argument('--check-'+name,type=Path)
    parser.add_argument('--all',action='store_true')
    parser.add_argument('--deps',type=Path)
    parser.add_argument('--json-report',type=Path)
    args=parser.parse_args()
    sections=[]; dependency=None; exit_code=0
    try:
        module,D7,R,F=load_jsonschema(args.deps)
        dependency={'version':importlib.metadata.version('jsonschema'),'module':os.path.abspath(module.__file__)}
        global _v
        _v=(D7,R,F)
        store,by_name=load_store(SCHEMAS_DIR)
        flags=[args.check_schemas]+[getattr(args,'check_'+k) for k in ['registry','taxonomy','sources','examples','bundle']]
        if not any(flags): args.all=True

        def run(name,callback):
            try: issues=callback()
            except (OSError,ValueError,KeyError,TypeError) as exc: issues=['[INPUT_ERROR] '+str(exc)]
            sections.append((name,issues))

        if args.all or args.check_schemas: run('schema 自校验',lambda:meta_check(SCHEMAS_DIR,D7))
        if args.all or args.check_registry:
            run('注册表 (19 赛事)',lambda:check_registry(args.check_registry or DEFAULT_REGISTRY,store,by_name)[0])
        if args.all or args.check_taxonomy:
            run('分类规范',lambda:check_taxonomy(args.check_taxonomy or DEFAULT_TAXONOMY,store,by_name))
        if args.all or args.check_sources:
            def sources():
                issues,total,ok=check_sources(args.check_sources or DEFAULT_SOURCES,store,by_name)
                counts.update(source_total=total,source_valid=ok)
                return issues
            run('逐文件记录',sources)
        if args.all or args.check_examples:
            def examples():
                taxonomy=json.loads(DEFAULT_TAXONOMY.read_text(encoding='utf-8'))
                pos,neg,manifest=check_examples(args.check_examples or DEFAULT_EXAMPLES,store,by_name,taxonomy)
                failed=['{}: {}'.format(f,'; '.join(errors)) for f,errors in pos if errors]
                neg_failed=['{}: expected {}, got {}'.format(f,expect,errors) for f,expect,errors in neg if not expect or not errors or expect not in '\n'.join(errors)]
                counts.update(positive_total=len(pos),positive_passed=len(pos)-len(failed),negative_total=len(neg),negative_rejected=len(neg)-len(neg_failed))
                return failed+neg_failed
            run('正负样例',examples)
        if args.check_bundle:
            def bundle():
                tax_errors=check_taxonomy(DEFAULT_TAXONOMY,store,by_name)
                if tax_errors: return tax_errors
                doc=parse_json(args.check_bundle.read_text(encoding='utf-8'))
                taxonomy=json.loads(DEFAULT_TAXONOMY.read_text(encoding='utf-8'))
                return validate_bundle(doc,store,by_name,taxonomy)
            run('数据包 '+str(args.check_bundle),bundle)
    except DependencyError as exc:
        exit_code=2;sections.append(('dependency',['[DEPENDENCY-MISSING] '+str(exc)]))
    except (OSError,ValueError,KeyError,TypeError) as exc:
        exit_code=1;sections.append(('input',['[INPUT_ERROR] '+str(exc)]))
    if any(errors for _,errors in sections) and exit_code==0: exit_code=1
    for name,errors in sections:
        print('[{}] {}'.format('FAIL' if errors else 'PASS',name))
        for error in errors[:50]: print('  '+error)
    report={'generated_at':datetime.utcnow().isoformat()+'Z','exit_code':exit_code,'dependency':dependency,'counts':counts,'sections':[{'name':n,'issues':e} for n,e in sections]}
    if args.json_report:
        args.json_report.parent.mkdir(parents=True,exist_ok=True)
        args.json_report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(counts,ensure_ascii=False))
    print('结论：'+('全部通过' if exit_code==0 else '存在未通过项'))
    return exit_code


_v=None
counts={}

if __name__=='__main__':
    sys.exit(main())
