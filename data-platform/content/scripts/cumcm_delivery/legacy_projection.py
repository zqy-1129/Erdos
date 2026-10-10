"""Typed compatibility export: stable aliases, exact port fields and real PG verification."""
import ast,asyncio,json,uuid
from pathlib import Path
from . import core,audit,services,local_closure,business_contracts as bc

PORTS=core.CODE_CONTENT_DIR.parents[1]/'server/app/domain/entitlement/ports.py'
FIELDS={'legacy_problem':'ProblemRecord','legacy_template':'TemplateRecord','legacy_case':'CaseRecord'}
NS=uuid.UUID('24cd09d0-0861-4cb3-978d-2619f3b401ab')

def alias(kind,original):
    if len(original)<=64:return original
    return 'cumcm-'+kind+'-'+uuid.uuid5(NS,kind+':'+original).hex

def validate_port_fields():
    classes={node.name:[a.target.id for a in node.body if isinstance(a,ast.AnnAssign)]
        for node in ast.parse(PORTS.read_text(encoding='utf-8')).body if isinstance(node,ast.ClassDef)}
    for kind,name in FIELDS.items():
        if set(classes[name])!=set(bc.schemas()[kind]['properties']):raise ValueError('server port field drift: '+name)
    return {'source_sha256':core.sha256_of(PORTS),'records':{k:classes[v] for k,v in FIELDS.items()},
        'validation':'parsed actual server source without importing incompatible Python 3.12 module'}

def build():
    rd=audit.run_dir(local_closure.args());output=rd/'catalog/legacy_projection'
    source_rows=audit.all_sources(local_closure.args())+core.load_jsonl(rd/'converted_sources.jsonl')
    sources={r['source_id']:r for r in source_rows};mapping=[];excluded=[];notes=[]
    problems=[]
    for p in core.load_json(rd/'catalog/problems.json'):
        statement=p['statement_sources'][0];folder=rd/'parsed'/statement['sha256']
        pages=[core.load_json(f) for f in sorted(folder.glob('page-*.json'))]
        text='\n'.join(page['text'] for page in pages)
        # Legacy prompt is a bounded preview; complete source is always an attachment.
        preview=text if len(text)<=8192 else text[:7600]+'\n【此字段为题面预览，完整题面见 statement PDF 附件；不得只据预览解题。】'
        if preview!=text:notes.append({'problem_id':p['problem_id'],'field':'prompt_zh','kind':'explicit_preview','full_chars':len(text)})
        attachments=[{'name':'statement.pdf','oss_key':services.object_key(statement['sha256']),'sha256':statement['sha256'],'media_type':'application/pdf'}]
        for a in p['attachments']:
            row=sources.get(a['source_id'])
            if row:attachments.append({'name':Path(row['relative_path']).name,'oss_key':services.object_key(a['sha256']),'sha256':a['sha256'],'media_type':row.get('media_type') or 'application/octet-stream'})
        value={'business_id':alias('problem',p['problem_id']),'competition':'cumcm','year':p['year'],'problem_code':p['problem_code'],
            'title':p['title'],'tags':p['problem_types'],'prompt_zh':preview,'prompt_en':'','attachments':attachments,
            'scoring':'','dataset_hint':'附件需按 SHA256 获取并检查题面对应关系；未核验附件不得自动视为本题数据。','visibility':'member'}
        try:bc.validate('legacy_problem',value)
        except Exception as exc:excluded.append({'kind':'problem','original_id':p['problem_id'],'reason':str(exc).splitlines()[0]});continue
        problems.append(value);mapping.append({'kind':'problem','original_id':p['problem_id'],'business_id':value['business_id']})
    known={p['business_id'] for p in problems};cases=[]
    for p in core.load_json(rd/'catalog/papers.json'):
        if p['relation_status'] not in ('verified_prior_snapshot','source_body_title_match','codex_visual_verified') or alias('problem',p['problem_id'] or '') not in known:
            excluded.append({'kind':'case','original_id':p['case_id'],'reason':'paper/problem relation not source-confirmed'});continue
        value={'business_id':alias('case',p['case_id']),'problem_id':alias('problem',p['problem_id']),
            'title':p['title'],'award':p.get('award') or '', 'method_tags':p['method_tags'],
            'oss_key':services.object_key(p['source_sha256']),'sha256':p['source_sha256'],'compliance_note':p['compliance_note']}
        try:bc.validate('legacy_case',value)
        except Exception as exc:excluded.append({'kind':'case','original_id':p['case_id'],'reason':str(exc).splitlines()[0]});continue
        cases.append(value);mapping.append({'kind':'case','original_id':p['case_id'],'business_id':value['business_id'],
            'id_policy':'unchanged if <=64, otherwise UUIDv5 alias; original ID preserved; never truncate digest'})
    templates=[]
    for package in core.load_json(rd/'templates/dependency_manifest.json')['packages']:
        path=core.safe_relpath(rd/'templates',package['file']);version=package.get('version',2)
        fmt='typst' if 'typst' in path.name else 'latex';sha=core.sha256_of(path)
        value={'business_id':'cumcm-format-'+fmt+'-v'+str(version),'competition':'cumcm','format':fmt,'oss_key':services.object_key(sha),
            'sha256':sha,'version':version,'changelog':'本地格式包；选择目标届次并附赛区规则后编译。历史内容参照另经检索接口取得。','tier':'free'}
        bc.validate('legacy_template',value);templates.append(value)
    for name,rows in [('problems',problems),('cases',cases),('templates',templates),('id_mapping',mapping),('excluded',excluded),('preview_notes',notes)]:core.write_json(output/(name+'.json'),rows)
    core.write_json(output/'port_contract_pin.json',validate_port_fields())
    return {'problems':len(problems),'cases':len(cases),'templates':len(templates),'excluded':len(excluded)}

async def verify_pg():
    rd=audit.run_dir(local_closure.args());folder=rd/'catalog/legacy_projection';conn=await services.pg()
    schema='content_legacy_local_v3'
    try:
        await conn.execute('CREATE SCHEMA IF NOT EXISTS '+schema)
        # Match real business String limits instead of opaque JSON-only tables.
        definitions={
            'problems':"business_id varchar(64) PRIMARY KEY, competition varchar(16) NOT NULL, year integer NOT NULL CHECK(year BETWEEN 2010 AND 2025), problem_code varchar(8) NOT NULL, title varchar(128) NOT NULL, tags jsonb NOT NULL, prompt_zh varchar(8192) NOT NULL, prompt_en varchar(8192) NOT NULL, attachments jsonb NOT NULL, scoring varchar(2048) NOT NULL, dataset_hint varchar(2048) NOT NULL, visibility varchar(16) NOT NULL",
            'templates':"business_id varchar(64) PRIMARY KEY, competition varchar(16) NOT NULL, format varchar(16) NOT NULL, oss_key varchar(256) NOT NULL, sha256 varchar(64) NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'), version integer NOT NULL CHECK(version>0), changelog varchar(512) NOT NULL, tier varchar(16) NOT NULL",
            'cases':"business_id varchar(64) PRIMARY KEY, problem_id varchar(64) NOT NULL REFERENCES "+schema+".problems(business_id), title varchar(128) NOT NULL, award varchar(32) NOT NULL, method_tags jsonb NOT NULL, oss_key varchar(256) NOT NULL, sha256 varchar(64) NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'), compliance_note varchar(256) NOT NULL CHECK(length(compliance_note)>0)"}
        for name,definition in definitions.items():await conn.execute('CREATE TABLE IF NOT EXISTS '+schema+'.'+name+'('+definition+')')
        async with conn.transaction():
            for name in ('cases','templates','problems'):await conn.execute('DELETE FROM '+schema+'.'+name)
            for name in ('problems','templates','cases'):
                rows=core.load_json(folder/(name+'.json'));fields=list(rows[0]) if rows else []
                if not rows:continue
                values=[tuple(json.dumps(r[f],ensure_ascii=False) if isinstance(r[f],(dict,list)) else r[f] for f in fields) for r in rows]
                placeholders=','.join('$'+str(i+1)+('::jsonb' if fields[i] in ('tags','attachments','method_tags') else '') for i in range(len(fields)))
                await conn.executemany('INSERT INTO '+schema+'.'+name+'('+','.join(fields)+') VALUES('+placeholders+')',values)
        counts={};checked=0
        for name in ('problems','templates','cases'):
            expected=core.load_json(folder/(name+'.json'));actual=await conn.fetch('SELECT * FROM '+schema+'.'+name)
            mapped={r['business_id']:dict(r) for r in actual}
            for row in expected:
                retrieved=mapped[row['business_id']]
                for f in ('tags','attachments','method_tags'):
                    if f in retrieved:retrieved[f]=core.json_loads(retrieved[f])
                if row!=retrieved:raise ValueError('legacy typed record roundtrip drift')
                checked+=1
            counts[name]=len(actual)
        negative=[]
        for name,field,value in [('cases','business_id','x'*65),('cases','problem_id','missing-parent'),('templates','sha256','not-a-sha'),('templates','version',0),('cases','compliance_note','')]:
            rows=core.load_json(folder/(name+'.json'))
            if not rows:raise ValueError('negative checks require materialized records')
            original=rows[0];bad=dict(original);bad['business_id']='negative-'+uuid.uuid4().hex;bad[field]=value
            fields=list(bad);params=[json.dumps(bad[f],ensure_ascii=False) if isinstance(bad[f],(dict,list)) else bad[f] for f in fields]
            tx=conn.transaction();await tx.start();rejected=False
            try:
                await conn.execute('INSERT INTO '+schema+'.'+name+'('+','.join(fields)+') VALUES('+','.join('$'+str(i+1)+('::jsonb' if fields[i] in ('tags','attachments','method_tags') else '') for i in range(len(fields)))+')',*params)
            except Exception:rejected=True
            finally:await tx.rollback()
            if not rejected:raise ValueError('legacy database accepted invalid '+field)
            negative.append({'table':name,'field':field,'rejected':True})
        report={'status':'pass','schema':schema,'counts':counts,'exact_fields_and_values_roundtripped':checked,'negative_constraints':negative,
            'server_business_tables_modified':False,'cloud_deployed':False,'port_source_sha256':core.sha256_of(PORTS)}
        core.write_json(rd/'quality/legacy_projection_pg.json',report)
        local_closure.state('legacy_projection','pass',['quality/legacy_projection_pg.json','catalog/legacy_projection/port_contract_pin.json'],report)
        print(json.dumps(report,ensure_ascii=False))
    finally:await conn.close()

if __name__=='__main__':print(build());asyncio.run(verify_pg())
