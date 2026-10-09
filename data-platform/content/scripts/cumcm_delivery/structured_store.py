"""Real typed PG storage and manifest-bound verification for PDF components."""
import asyncio,json
from . import core,audit,services,local_closure,business_contracts as bc

async def import_structured(args=None):
    args=args or local_closure.args();rd=audit.run_dir(args);release=args.release_id;dest=audit.release_dir(args)
    # Inputs come only from immutable, pinned release metadata, not live build files.
    audit.validate_database_inputs(dest);source=dest/'metadata';conn=await services.pg();counts={}
    manifest=core.load_json(dest/'manifest.json');entries={e['path']:e for e in manifest['files']}
    def pinned(path,jsonl=False):
        relative=path.relative_to(dest).as_posix();entry=entries.get(relative)
        if entry is None:raise ValueError('structured input absent from sealed manifest')
        raw=core.safe_relpath(dest,relative).read_bytes()
        if len(raw)!=entry['size'] or core.sha256_bytes(raw)!=entry['sha256']:raise ValueError('structured input bytes drift: '+relative)
        return [core.json_loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()] if jsonl else core.json_loads(raw.decode('utf-8'))
    try:
        await conn.execute((core.CONTENT_DIR/'sql/cumcm/0004_structured_sources.sql').read_text(encoding='utf-8'))
        status=await conn.fetchval('SELECT status FROM content_de_codex.releases WHERE release_id=$1',release)
        if status!='staging':raise ValueError('structured import requires staged new release')
        if await conn.fetchval('SELECT manifest_sha256 FROM content_de_codex.releases WHERE release_id=$1',release)!=core.sha256_of(dest/'manifest.json'):
            raise ValueError('structured input manifest differs from imported PG release')
        async with conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(621032025)')
            for table in ('formula_regions','visual_components','section_blocks','sections','blocks','pages'):await conn.execute('DELETE FROM content_de_codex.'+table+' WHERE release_id=$1',release)
            page_buffer=[];block_buffer=[];visual_buffer=[];page_count=0;block_count=0;visual_count=0
            async def verify_rows(table,records,idfield,fields):
                for offset in range(0,len(records),5000):
                    batch=records[offset:offset+5000];expected={r[1]:r for r in batch}
                    stored=await conn.fetch('SELECT * FROM content_de_codex.'+table+' WHERE release_id=$1 AND '+idfield+'=ANY($2::text[])',release,list(expected))
                    if len(stored)!=len(expected):raise ValueError('typed component roundtrip count mismatch: '+table)
                    for item in stored:
                        row=expected[item[idfield]]
                        if tuple(item[f] for f in fields)!=row[2:-1] or core.json_loads(item['payload'])!=core.json_loads(row[-1]):
                            raise ValueError('typed component fields or payload mismatch: '+table)
            async def flush():
                if page_buffer:
                    await conn.copy_records_to_table('pages',schema_name='content_de_codex',records=page_buffer)
                if block_buffer:
                    await conn.copy_records_to_table('blocks',schema_name='content_de_codex',records=block_buffer)
                if visual_buffer:
                    await conn.copy_records_to_table('visual_components',schema_name='content_de_codex',records=visual_buffer)
                stored=await conn.fetch('SELECT source_sha256,page,width,height,evidence_asset_id,evidence_version,payload FROM content_de_codex.pages WHERE release_id=$1 AND (source_sha256,page) IN (SELECT * FROM unnest($2::text[],$3::integer[]))',release,[r[1] for r in page_buffer],[r[2] for r in page_buffer])
                expected={(r[1],r[2]):r for r in page_buffer}
                if len(stored)!=len(expected):raise ValueError('page roundtrip count mismatch')
                for item in stored:
                    r=expected[(item['source_sha256'],item['page'])]
                    if (item['width'],item['height'],item['evidence_asset_id'],item['evidence_version'])!=r[3:7] or core.json_loads(item['payload'])!=core.json_loads(r[7]):raise ValueError('page roundtrip mismatch')
                for table,buffer,idfield,fieldnames in [('blocks',block_buffer,'block_id',('source_sha256','page','reading_order','text_content','bbox','extraction_method')),('visual_components',visual_buffer,'component_id',('kind','source_sha256','page','anchor_block_id','evidence_asset_id','evidence_version','semantic_status'))]:
                    if not buffer:continue
                    stored=await conn.fetch('SELECT * FROM content_de_codex.'+table+' WHERE release_id=$1 AND '+idfield+'=ANY($2::text[])',release,[r[1] for r in buffer]);expected={r[1]:r for r in buffer}
                    if len(stored)!=len(expected):raise ValueError('source roundtrip count mismatch: '+table)
                    for item in stored:
                        r=expected[item[idfield]]
                        if tuple(item[f] for f in fieldnames)!=r[2:-1] or core.json_loads(item['payload'])!=core.json_loads(r[-1]):raise ValueError('source roundtrip mismatch: '+table)
                page_buffer.clear();block_buffer.clear();visual_buffer.clear()
            for path in sorted((source/'parsed').glob('*/page-*.json')):
                p=pinned(path);ref=p['full_page_evidence']['asset_ref'];sha=p['input_sha256'];page=p['page']
                page_buffer.append((release,sha,page,p['width'],p['height'],ref['asset_id'],ref['version'],json.dumps(p,ensure_ascii=False)))
                page_count+=1
                for i,b in enumerate(p['blocks'],1):
                    block={'block_id':b['block_id'],'source_sha256':sha,'page':page,'order':i,'text':b['text'],'bbox':b['bbox'],
                        'coordinates':'pdf_points_bottom_left','extraction_method':b['method'],'confidence':b['confidence'],'semantic_status':'candidate'}
                    box=block['bbox']
                    if box[2]<=0 or box[3]<=0 or box[0]>=p['width'] or box[1]>=p['height']:block['semantic_status']='abstained'
                    bc.validate('block',block)
                    block_buffer.append((release,b['block_id'],sha,page,i,b['text'],block['bbox'],b['method'],json.dumps(block,ensure_ascii=False)));block_count+=1
                for v in p['visual_components']:
                    bc.validate('visual',v);visual_buffer.append((release,v['component_id'],v['kind'],sha,page,v['anchor_block_id'],v['asset_ref']['asset_id'],v['asset_ref']['version'],v['semantic_status'],json.dumps(v,ensure_ascii=False)));visual_count+=1
                if len(page_buffer)>=100:await flush();print('structured pages {}'.format(page_count),flush=True)
            await flush()
            expected_pages=sum(d['pages'] for d in pinned(dest/'metadata.json')['parsing']['documents'])
            if page_count!=expected_pages:raise ValueError('source page manifest coverage incomplete')
            sections=pinned(source/'catalog/business_entities/section.jsonl',jsonl=True);records=[];membership=[]
            for s in sections:
                bc.validate('section',s);records.append((release,s['section_id'],s['case_id'],s['parent_id'],s['level'],s['title'],s['start_block'],s['end_block'],s['recognized_chars'],s['stage'],json.dumps(s,ensure_ascii=False)))
                membership.extend((release,b,s['section_id']) for b in s['block_ids'])
            await conn.copy_records_to_table('sections',schema_name='content_de_codex',records=records)
            await verify_rows('sections',records,'section_id',('case_id','parent_id','level','title','start_block','end_block','recognized_chars','stage'))
            for offset in range(0,len(membership),20000):await conn.copy_records_to_table('section_blocks',schema_name='content_de_codex',records=membership[offset:offset+20000])
            actual_members=await conn.fetch('SELECT block_id,section_id FROM content_de_codex.section_blocks WHERE release_id=$1',release)
            if {(r['block_id'],r['section_id']) for r in actual_members}!={(r[1],r[2]) for r in membership}:
                raise ValueError('exclusive section membership roundtrip mismatch')
            counts={'pages':page_count,'blocks':block_count,'visual_components':visual_count,'sections':len(records),'section_blocks':len(membership)}
            formulas=pinned(source/'catalog/formula_regions.jsonl',jsonl=True);formula_records=[]
            for f in formulas:
                bc.validate('formula_region',f);ref=f['crop_ref'] or {}
                formula_records.append((release,f['formula_region_id'],f['source_sha256'],f['page'],f['region_type'],f['bbox'],ref.get('asset_id'),ref.get('version'),f['latex_candidate'],f['semantic_status'],json.dumps(f,ensure_ascii=False)))
            for offset in range(0,len(formula_records),10000):
                await conn.copy_records_to_table('formula_regions',schema_name='content_de_codex',records=formula_records[offset:offset+10000])
            await verify_rows('formula_regions',formula_records,'formula_region_id',('source_sha256','page','region_type','bbox','crop_asset_id','crop_version','latex_candidate','semantic_status'))
            counts['formula_regions']=len(formula_records)
            for table,total in counts.items():
                actual=await conn.fetchval('SELECT count(*) FROM content_de_codex.'+table+' WHERE release_id=$1',release)
                if actual!=total:raise ValueError('typed source count mismatch: '+table)
            # Register clean business entities and visual observations with payload hashes.
            business_counts={}
            for path in sorted((source/'catalog/business_entities').glob('*.jsonl')):
                kind=path.stem;idfield={'source':'source_id','problem':'problem_id','subproblem':'subproblem_id','paper':'case_id','section':'section_id'}[kind]
                for row in pinned(path,jsonl=True):
                    bc.validate(kind,row);payload=json.dumps(row,ensure_ascii=False,sort_keys=True,allow_nan=False)
                    await conn.execute('INSERT INTO content_de_codex.entities VALUES($1,$2,$3,$4::jsonb,$5) ON CONFLICT(release_id,kind,entity_id) DO UPDATE SET payload=excluded.payload,payload_sha256=excluded.payload_sha256',release,'v4_'+kind,row[idfield],payload,core.sha256_bytes(payload))
                    business_counts[kind]=business_counts.get(kind,0)+1
            # Compare all clean business payloads to their immutable source, including hashes.
            for path in sorted((source/'catalog/business_entities').glob('*.jsonl')):
                kind=path.stem;idfield={'source':'source_id','problem':'problem_id','subproblem':'subproblem_id','paper':'case_id','section':'section_id'}[kind]
                expected={r[idfield]:r for r in pinned(path,jsonl=True)}
                stored=await conn.fetch('SELECT entity_id,payload,payload_sha256 FROM content_de_codex.entities WHERE release_id=$1 AND kind=$2',release,'v4_'+kind)
                if len(stored)!=len(expected):raise ValueError('business payload count drift: '+kind)
                for item in stored:
                    value=core.json_loads(item['payload']);canonical=json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)
                    if value!=expected[item['entity_id']] or core.sha256_bytes(canonical)!=item['payload_sha256']:raise ValueError('business payload roundtrip drift: '+kind)
            counts['business_entities']=sum(business_counts.values())
            fingerprints=await table_fingerprints(conn,release)
            await conn.execute('INSERT INTO content_de_codex.structured_import_receipts(release_id,manifest_sha256,counts,fingerprints) VALUES($1,$2,$3::jsonb,$4::jsonb) ON CONFLICT(release_id) DO UPDATE SET manifest_sha256=excluded.manifest_sha256,counts=excluded.counts,fingerprints=excluded.fingerprints,verified_at=now()',release,core.sha256_of(dest/'manifest.json'),json.dumps(counts),json.dumps(fingerprints))
        report={'status':'pass','release_id':release,'counts':counts,'input_manifest_sha256':core.sha256_of(dest/'manifest.json'),
            'foreign_keys':'source asset, page, anchor block, evidence asset/version, case, parent section and disjoint membership enforced by real PG',
            'historical_and_user_tasks_separate':True,'original_page_json_preserved':True}
        core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_local_structured_store.json',report)
        local_closure.state('typed_pdf_store','pass',['reports/trae/cumcm_local_structured_store.json'],report);print(json.dumps(report))
    finally:await conn.close()

async def activation_guard(conn,release_id):
    release=await conn.fetchrow('SELECT metadata,manifest_sha256 FROM content_de_codex.releases WHERE release_id=$1',release_id)
    meta=core.json_loads(release['metadata'])
    if not meta.get('structured_store_required'):return
    exists=await conn.fetchval("SELECT to_regclass('content_de_codex.structured_import_receipts') IS NOT NULL")
    if not exists:raise ValueError('new local release has no structured import receipt')
    receipt=await conn.fetchrow('SELECT * FROM content_de_codex.structured_import_receipts WHERE release_id=$1',release_id)
    if not receipt or receipt['manifest_sha256']!=release['manifest_sha256']:raise ValueError('structured import receipt pin mismatch')
    counts=core.json_loads(receipt['counts']);expected=meta['component_counts']
    mapping={'pages':'page','blocks':'block','sections':'section','visual_components':'visual','formula_regions':'formula_region'}
    for table,key in mapping.items():
        actual=await conn.fetchval('SELECT count(*) FROM content_de_codex.'+table+' WHERE release_id=$1',release_id)
        if actual!=counts[table] or actual!=expected[key]:raise ValueError('structured activation coverage drift: '+table)
    actual=await conn.fetchval("SELECT count(*) FROM content_de_codex.entities WHERE release_id=$1 AND kind LIKE 'v4_%'",release_id)
    if actual!=counts['business_entities']:raise ValueError('business entity activation coverage drift')
    if await table_fingerprints(conn,release_id)!=core.json_loads(receipt['fingerprints']):raise ValueError('typed source field or payload fingerprint drift')

async def verify_components_against_source(conn,release):
    root=core.CONTENT_DIR/'out/cumcm_delivery/releases'/release
    pin=await conn.fetchval('SELECT manifest_sha256 FROM content_de_codex.releases WHERE release_id=$1',release)
    if core.sha256_of(root/'manifest.json')!=pin:raise ValueError('component roundtrip manifest differs')
    entries={e['path']:e for e in core.load_json(root/'manifest.json')['files']}
    def rows(relative):
        raw=core.safe_relpath(root,relative).read_bytes();e=entries[relative]
        if len(raw)!=e['size'] or core.sha256_bytes(raw)!=e['sha256']:raise ValueError('component roundtrip source bytes differ')
        return [core.json_loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    sections=rows('metadata/catalog/business_entities/section.jsonl');formulas=rows('metadata/catalog/formula_regions.jsonl')
    counts={}
    for table,items,identity,mapping in [
        ('sections',sections,'section_id',{k:k for k in ('case_id','parent_id','level','title','start_block','end_block','recognized_chars','stage')}),
        ('formula_regions',formulas,'formula_region_id',{k:k for k in ('source_sha256','page','region_type','bbox','latex_candidate','semantic_status')})]:
        for start in range(0,len(items),5000):
            batch=items[start:start+5000];expected={r[identity]:r for r in batch}
            stored=await conn.fetch('SELECT * FROM content_de_codex.'+table+' WHERE release_id=$1 AND '+identity+'=ANY($2::text[])',release,list(expected))
            if len(stored)!=len(expected):raise ValueError('component source count mismatch: '+table)
            for item in stored:
                value=expected[item[identity]]
                if core.json_loads(item['payload'])!=value or any(item[column]!=value[key] for column,key in mapping.items()):
                    raise ValueError('component scalar/payload differs from source: '+table)
                if table=='formula_regions':
                    ref=value['crop_ref'] or {}
                    if (item['crop_asset_id'],item['crop_version'])!=(ref.get('asset_id'),ref.get('version')):
                        raise ValueError('formula crop scalar reference differs from source')
        counts[table]=len(items)
    expected={(bid,s['section_id']) for s in sections for bid in s['block_ids']}
    stored=await conn.fetch('SELECT block_id,section_id FROM content_de_codex.section_blocks WHERE release_id=$1',release)
    if {(r['block_id'],r['section_id']) for r in stored}!=expected:raise ValueError('source section ownership differs')
    counts['exclusive_section_memberships']=len(expected)
    return counts

async def table_fingerprints(conn,release_id):
    fields={'pages':('source_sha256,page','source_sha256,page,width,height,evidence_asset_id,evidence_version,payload'),
        'blocks':('block_id','block_id,source_sha256,page,reading_order,text_content,bbox,extraction_method,payload'),
        'sections':('section_id','section_id,case_id,parent_id,level,title,start_block,end_block,recognized_chars,stage,payload'),
        'section_blocks':('block_id','block_id,section_id'),
        'visual_components':('component_id','component_id,kind,source_sha256,page,anchor_block_id,evidence_asset_id,evidence_version,semantic_status,payload'),
        'formula_regions':('formula_region_id','formula_region_id,source_sha256,page,region_type,bbox,crop_asset_id,crop_version,latex_candidate,semantic_status,payload'),
        'entities':('kind,entity_id','kind,entity_id,payload,payload_sha256')}
    result={}
    for table,(order,columns) in fields.items():
        sql="SELECT encode(sha256(convert_to(coalesce(string_agg(encode(sha256(convert_to(jsonb_build_array("+columns+")::text,'UTF8')),'hex'),'' ORDER BY "+order+"),''),'UTF8')),'hex') FROM content_de_codex."+table+' WHERE release_id=$1'
        result[table]=await conn.fetchval(sql,release_id)
    return result

if __name__=='__main__':asyncio.run(import_structured())
