"""Local cross-record checks for draft content v1; no database/network/file downloads."""
import re

COLLECTIONS = {'problems': ('problem_profile', 'problem_id', 'profile_version'),
               'cases': ('case_profile', 'case_id', 'profile_version'),
               'assets': ('asset', 'asset_id', 'version'),
               'families': ('template_family', 'family_id', 'family_version'),
               'rulesets': ('ruleset', 'ruleset_id', 'version'),
               'outlines': ('base_outline', 'outline_id', 'version'),
               'writing_profiles': ('writing_profile', 'writing_profile_id', 'version'),
               'figure_styles': ('figure_style', 'style_id', 'version'),
               'table_styles': ('table_style', 'style_id', 'version'),
               'figure_specs': ('figure_spec', 'spec_id', None),
               'task_contexts': ('task_context', 'task_id', None),
               'releases': ('release_manifest', 'release_id', 'version')}


def taxonomy_issues(taxonomy, legacy_text=None):
    out = []
    def add(code, path, msg): out.append('[{}] {}: {}'.format(code, path, msg))
    codes = [t['code'] for t in taxonomy['problem_types']]
    expected = {'optimization','prediction','evaluation','mechanism','classification','statistical_analysis','unknown'}
    if set(codes) != expected or len(codes) != 7:
        add('TAXONOMY_DUPLICATE', '/problem_types', 'exactly seven unique working types required')
    entries = taxonomy['method_tags']['entries']
    methods = [e['code'] for e in entries]
    if len(set(methods)) != len(methods): add('TAXONOMY_DUPLICATE', '/method_tags/entries', 'duplicate method code')
    alias_map = {}
    for i,e in enumerate(entries):
        for a in e['aliases']+[e['code']]:
            if a in alias_map and alias_map[a] != e['code']:
                add('TAXONOMY_ALIAS', '/method_tags/entries/{}'.format(i), 'ambiguous alias '+a)
            alias_map[a]=e['code']
    listed = [m for c in taxonomy['method_tags']['categories'] for m in c['codes']]
    if set(listed) != set(methods) or len(listed) != len(set(listed)):
        add('TAXONOMY_REF', '/method_tags/categories', 'category coverage differs from method entries')
    for key in ['data_tags','domain_tags']:
        items=[x['code'] for x in taxonomy[key]]
        if len(items)!=len(set(items)): add('TAXONOMY_DUPLICATE','/'+key,'duplicate code')
    if taxonomy.get('confidence_range') != [0,1]: add('TAXONOMY_RANGE','/confidence_range','must be [0,1]')
    if legacy_text is not None:
        legacy = set(re.findall(r'^\s*- tag:\s*(.+?)\s*$', legacy_text, re.M))
        mapped = {e['legacy_tag'] for e in entries}
        if mapped != legacy:
            add('METHOD_DICTIONARY', '/method_tags/entries', 'legacy aliases differ: '+str(sorted(mapped ^ legacy)))
    return out


def validate_graph(bundle, taxonomy):
    issues=[]
    def add(code,path,msg): issues.append('[{}] {}: {}'.format(code,path,msg))
    groups={k:bundle.get(k,[]) for k in COLLECTIONS}
    if bundle.get('release') is not None: groups['releases']=groups['releases']+[bundle['release']]
    maps={}
    for k,(_,key,_) in COLLECTIONS.items():
        maps[k]={}
        for i,record in enumerate(groups[k]):
            rid=record[key]
            if rid in maps[k]: add('DUPLICATE_ID','/{}/{}/{}'.format(k,i,key),'重复ID '+rid)
            maps[k][rid]=record
    assets=maps['assets']
    pt={t['code'] for t in taxonomy['problem_types']}
    methods={e['code'] for e in taxonomy['method_tags']['entries']}
    data={t['code'] for t in taxonomy['data_tags']}
    domains={t['code'] for t in taxonomy['domain_tags']}

    def competition(record,target,path):
        if record.get('competition_id') != target.get('competition_id'):
            add('COMPETITION_MISMATCH',path,'赛事不匹配')

    def lock(reference,path,record=None,role=None,owner=None):
        if reference is None: return None
        a=assets.get(reference['asset_id'])
        if a is None:
            add('ASSET_REF',path,'资源依赖缺失 '+reference['asset_id']);return None
        if reference['version']!=a['version'] or reference['sha256']!=a['sha256']:
            add('ASSET_LOCK',path,'资源依赖缺失：sha256/version 与 asset 不一致')
        if record is not None: competition(record,a,path)
        if role and a['role']!=role: add('ASSET_ROLE',path,'asset role mismatch: expected '+role)
        if owner and a['owner_id']!=owner: add('ASSET_OWNER',path,'owner mismatch: expected '+owner)
        return a

    def labels(record,path):
        for key,allowed in [('problem_types',pt),('method_tags',methods),('data_tags',data),('domain_tags',domains)]:
            for i,t in enumerate(record.get(key,[])):
                if t not in allowed: add('TAXONOMY_REF',path+'/'+key+'/'+str(i),'unregistered code '+t)
        if 'unknown' in record.get('problem_types',[]) and len(record['problem_types'])!=1:
            add('LABEL_UNKNOWN',path+'/problem_types','unknown cannot coexist with determined types')
        status=record.get('label_status')
        if status=='unknown' and record.get('confidence') is not None:
            add('LABEL_STATUS',path+'/confidence','unknown confidence must be null')
        if status=='approved' and (record.get('confidence') is None or record.get('provenance',{}).get('kind')=='unknown'):
            add('LABEL_PROVENANCE',path+'/provenance','approved label requires confidence and provenance')

    def evidence(ev,path,source_hash=None):
        if ev is None: return
        if source_hash and ev['source_sha256']!=source_hash: add('EVIDENCE_HASH',path+'/source_sha256','evidence source hash differs')
        if ev['end']<ev['start'] or (ev['locator_type']=='page' and ev['start']<1):
            add('EVIDENCE_RANGE',path,'invalid locator range')

    for i,p in enumerate(groups['problems']):
        path='/problems/'+str(i);labels(p,path)
        seen=set()
        for j,s in enumerate(p['subproblems']):
            sp=path+'/subproblems/'+str(j);labels(s,sp)
            if s['subproblem_id'] in seen: add('DUPLICATE_ID',sp+'/subproblem_id','重复ID')
            seen.add(s['subproblem_id']);evidence(s['provenance']['evidence'],sp+'/provenance/evidence',p['source_sha256'])
            if not set(s['problem_types'])-{'unknown'} <= set(p['problem_types']):
                add('LABEL_AGGREGATE',sp+'/problem_types','subproblem types missing from problem aggregate')
        evidence(p['provenance']['evidence'],path+'/provenance/evidence',p['source_sha256'])
        a=lock(p['source_asset_ref'],path+'/source_asset_ref',p,'problem',p['problem_id'])
        if a and a['sha256']!=p['source_sha256']: add('SOURCE_HASH',path+'/source_sha256','源哈希不一致')
        if bool(p['source_asset_ref']) != bool(p['source_sha256']): add('SOURCE_LOCK',path+'/source_asset_ref','hash and source asset must be known together')

    for i,c in enumerate(groups['cases']):
        path='/cases/'+str(i);parent=maps['problems'].get(c['problem_id'])
        if c['resolution_status']=='unresolved':
            if c['problem_id'] is not None or c['publication_status']!='staging':
                add('UNRESOLVED_PARENT',path+'/problem_id','unresolved parent allowed only as null in staging')
        elif parent is None: add('PARENT_MISSING',path+'/problem_id','父题不存在 '+str(c['problem_id']))
        else: competition(c,parent,path+'/competition_id')
        a=lock(c['source_asset_ref'],path+'/source_asset_ref',c,'case_paper',c['case_id'])
        if a and a['sha256']!=c['source_sha256']: add('SOURCE_HASH',path+'/source_sha256','源哈希不一致')
        if bool(c['source_asset_ref']) != bool(c['source_sha256']): add('SOURCE_HASH',path+'/source_asset_ref','源哈希不一致：missing source lock')
        if any(c['license'].values()) and c['license_evidence'] is None: add('LICENSE_EVIDENCE',path+'/license_evidence','positive permission requires decision evidence')
        if c['publication_status']=='published':
            if not c['license']['distribute_profile']: add('LICENSE_SCOPE',path+'/license/distribute_profile','未授权发布')
            if c['review_status']!='approved' or not c['source_sha256'] or not c['extractor_version']:
                add('PROFILE_NOT_READY',path,'published profile requires review and extraction provenance')
        for m in c.get('method_tags',[]):
            if m not in methods: add('TAXONOMY_REF',path+'/method_tags','unregistered method '+m)
        subids={s['subproblem_id'] for s in parent['subproblems']} if parent else set()
        for field in ['structure','writing','figures']:
            for j,s in enumerate(c[field]):
                sp=path+'/'+field+'/'+str(j);evidence(s.get('evidence'),sp+'/evidence',c['source_sha256'])
                if s.get('subproblem_ref') is not None and s['subproblem_ref'] not in subids: add('SUBPROBLEM_REF',sp+'/subproblem_ref','missing parent subproblem')
                if s.get('kind')=='fact' and s.get('evidence') is None: add('EVIDENCE_REQUIRED',sp+'/evidence','extracted fact needs evidence')
                if s.get('evidence') is not None and not c['source_sha256']: add('EVIDENCE_HASH',sp+'/evidence','evidence requires a known source hash')
                pr=s.get('page_range')
                if pr and pr[1]<pr[0]: add('EVIDENCE_RANGE',sp+'/page_range','reversed page range')
        length=c['length']
        for unit,key in [('zh_chars','zh_parts'),('en_words','en_parts')]:
            vals=list(length[key].values())
            if length[unit] is not None and all(v is not None for v in vals) and sum(vals)!=length[unit]:
                add('LENGTH_TOTAL',path+'/length/'+unit,'component counts do not sum to total')

    role_owner={'problem':'problems','attachment':'problems','case_paper':'cases','case_profile':'cases','template_layout':'families','template_outline':'outlines','writing_policy':'families','figure_style':'figure_styles','table_style':'table_styles','writing_profile':'writing_profiles','ruleset':'rulesets'}
    for i,a in enumerate(groups['assets']):
        path='/assets/'+str(i);group=role_owner.get(a['role']);owner=maps[group].get(a['owner_id']) if group else None
        if group and owner is None: add('ASSET_OWNER',path+'/owner_id','missing owner '+a['owner_id'])
        elif owner:
            competition(a,owner,path+'/competition_id')
            if a['role']=='case_profile' and a['version']!=owner['profile_version']: add('PROFILE_LOCK',path+'/version','profile asset version mismatch')
            if a['role'] in ['figure_style','table_style','writing_profile','ruleset','template_outline'] and a['version']!=owner['version']:
                add('PROFILE_LOCK',path+'/version','resource/entity version mismatch')
            if a['role']=='case_paper' and a['license']!=owner['license']:
                add('LICENSE_SCOPE',path+'/license','source paper permissions differ from case')
        if a['license_status']=='unknown' and any(a['license'].values()): add('LICENSE_SCOPE',path+'/license','unknown authorization defaults to all false')
        if any(a['license'].values()) and a['license_evidence'] is None: add('LICENSE_EVIDENCE',path+'/license_evidence','permission evidence missing')

    for i,r in enumerate(groups['rulesets']):
        path='/rulesets/'+str(i)
        for j,s in enumerate(r['sources']): lock(s['asset_ref'],path+'/sources/'+str(j),r)
        for j,c in enumerate(r['constraints']):
            idx=c['source_index']
            if c['origin']=='official' and (idx is None or idx>=len(r['sources'])): add('RULES_SOURCE',path+'/constraints/'+str(j),'official constraint missing source')
        if r['verification_status']=='verified' and (not r['sources'] or r['verified_at'] is None or r['edition'] is None):
            add('RULES_UNVERIFIED',path,'verified rules require edition, sources and timestamp')

    def family_resources(f,path):
        formats=set()
        for j,l in enumerate(f['layout_refs']):
            if l['format'] in formats: add('DUPLICATE_FORMAT',path+'/layout_refs/'+str(j),'duplicate output format')
            formats.add(l['format']);a=lock(l['asset_ref'],path+'/layout_refs/'+str(j),f,'template_layout',f['family_id'])
            if a and a.get('format')!=l['format']: add('ASSET_FORMAT',path+'/layout_refs/'+str(j),'layout format differs from asset')
        for key,role in [('base_outline_ref','template_outline'),('writing_policy_ref','writing_policy'),('figure_style_ref','figure_style'),('table_style_ref','table_style'),('ruleset_ref','ruleset')]:
            lock(f[key],path+'/'+key,f,role)
        r=maps['rulesets'].get(f['ruleset_id'])
        if r is None: add('RULESET_REF',path+'/ruleset_id','missing ruleset')
        else:
            competition(f,r,path+'/ruleset_id')
            if f['ruleset_version']!=r['version'] or f['ruleset_status']!=r['verification_status']:
                add('RULESET_LOCK',path+'/ruleset_id','ruleset version/status mismatch')
            if f['ruleset_ref']: lock(f['ruleset_ref'],path+'/ruleset_ref',f,'ruleset',r['ruleset_id'])
        if f['publication_status']=='published' and (f['review_status']!='approved' or any(f[k] is None for k in ['base_outline_ref','writing_policy_ref','figure_style_ref','table_style_ref','ruleset_ref']) or any(l['asset_ref'] is None for l in f['layout_refs'])):
            add('FAMILY_NOT_READY',path,'published template requires all resources and approval')
    for i,f in enumerate(groups['families']): family_resources(f,'/families/'+str(i))

    def profile_lock(s,path,record):
        c=maps['cases'].get(s['case_id'])
        if c is None: add('PROFILE_REF',path,'missing case '+s['case_id']);return None
        competition(record,c,path)
        if s['profile_version']!=c['profile_version'] or s['source_sha256']!=c['source_sha256']: add('PROFILE_LOCK',path,'profile version/source hash mismatch')
        return c
    for i,w in enumerate(groups['writing_profiles']):
        path='/writing_profiles/'+str(i)
        ids=[s['case_id'] for s in w['sources']]
        if w['sample_count']!=len(set(ids)) or len(ids)!=len(set(ids)): add('SAMPLE_COUNT',path+'/sample_count','must equal number of distinct cited cases')
        for j,s in enumerate(w['sources']):
            c=profile_lock(s,path+'/sources/'+str(j),w)
            if c and not c['license']['internal_analysis']: add('LICENSE_SCOPE',path+'/sources/'+str(j),'case not licensed for analysis')
        lock(w['missing_fallback']['fallback_ref'],path+'/missing_fallback/fallback_ref',w,'writing_policy')
        if w['sample_count']==0 and w['missing_fallback']['fallback_ref'] is None: add('MISSING_FALLBACK',path,'zero samples require fallback')
        for j,stats in enumerate(w['statistics']['length']):
            q=stats['quantiles']
            if q is not None and q!=sorted(q): add('QUANTILE_ORDER',path+'/statistics/length/'+str(j),'quantiles must be ordered')

    for i,s in enumerate(groups['figure_specs']):
        path='/figure_specs/'+str(i);a=lock(s['style_ref'],path+'/style_ref',role='figure_style')
        ctx=maps['task_contexts'].get(s['task_id'])
        if ctx is None: add('TASK_REF',path+'/task_id','missing task context')
        elif a: competition(ctx,a,path+'/style_ref')
    for i,t in enumerate(groups['task_contexts']):
        path='/task_contexts/'+str(i);f=maps['families'].get(t['family_id'])
        if f is None: add('FAMILY_REF',path+'/family_id','missing family');continue
        competition(t,f,path+'/family_id')
        if f['family_version']!=t['family_version']: add('FAMILY_LOCK',path+'/family_version','family version mismatch')
        if (t['ruleset_id'],t['ruleset_version'],t['ruleset_status'])!=(f['ruleset_id'],f['ruleset_version'],f['ruleset_status']): add('RULESET_LOCK',path+'/ruleset_id','family rules mismatch')
        if t['rules_compliance']=='verified' and t['ruleset_status']!='verified': add('RULES_UNVERIFIED',path+'/rules_compliance','unverified rules cannot claim compliance')
        local=set()
        for j,l in enumerate(t['local_resources']):
            a=lock(l['asset_ref'],path+'/local_resources/'+str(j),t);local.add(l['asset_ref']['asset_id'])
            if a and t['model_execution']=='third_party' and not a['license']['send_to_third_party_model']:
                add('LICENSE_SCOPE',path+'/local_resources/'+str(j),'third-party model permission denied')
        selected=next((l['asset_ref'] for l in f['layout_refs'] if l['format']==t['format']),None)
        needed=[selected]+[f[k] for k in ['base_outline_ref','writing_policy_ref','figure_style_ref','table_style_ref','ruleset_ref']]
        if any(r is None or r['asset_id'] not in local for r in needed): add('LOCAL_RESOURCE_MISSING',path+'/local_resources','selected format and content resources must be cached')
        lock(t['style_ref'],path+'/style_ref',t,'figure_style')
        if t['style_ref']!=f['figure_style_ref']: add('ASSET_LOCK',path+'/style_ref','family style lock mismatch')
        for j,s in enumerate(t['candidate_profiles']):
            c=profile_lock(s,path+'/candidate_profiles/'+str(j),t)
            if c and (not c['license']['internal_analysis'] or c['resolution_status']!='resolved'): add('REFERENCE_NOT_READY',path+'/candidate_profiles/'+str(j),'reference requires resolved parent and analysis permission')
            a=lock(s['profile_asset_ref'],path+'/candidate_profiles/'+str(j)+'/profile_asset_ref',t,'case_profile',s['case_id'])
            if a and a['asset_id'] not in local: add('LOCAL_RESOURCE_MISSING',path+'/candidate_profiles/'+str(j),'profile asset not cached')
            if c and t['model_execution']=='third_party' and not c['license']['send_to_third_party_model']:
                add('LICENSE_SCOPE',path+'/candidate_profiles/'+str(j),'case cannot be sent to third-party model')

    release_edges={}
    for i,r in enumerate(groups['releases']):
        path='/releases/'+str(i);release_edges[r['release_id']]=[];seen=set();removed=set(r['removed'])
        for j,item in enumerate(r['items']):
            ip=path+'/items/'+str(j);aid=item['asset_id']
            if aid in seen: add('DUPLICATE_ID',ip+'/asset_id','重复ID in release')
            seen.add(aid)
            if item['change']=='remove':
                if aid not in removed: add('REMOVAL_STATE',ip,'remove item must be in removed')
                continue
            if aid in removed: add('REMOVAL_STATE',ip,'active item also removed')
            a=lock(item,ip,r)
            if not a: continue
            if item['role']!=a['role']: add('ASSET_ROLE',ip+'/role','release role mismatch')
            if r['published_status']=='published':
                scope='distribute_profile' if a['role'] in ['case_profile','writing_profile'] else 'distribute_original'
                if a['license_status'] not in ['audited','distributable'] or not a['license'][scope]: add('LICENSE_SCOPE',ip,'未授权发布 '+aid+' needs '+scope)
                if a['role']=='case_profile':
                    c=maps['cases'].get(a['owner_id'])
                    if c and (c['publication_status']!='published' or not c['license']['distribute_profile']): add('PROFILE_NOT_READY',ip,'case profile not ready for distribution')
                if a['role']=='writing_profile':
                    w=maps['writing_profiles'].get(a['owner_id'])
                    if w:
                        for source in w['sources']:
                            c=maps['cases'].get(source['case_id'])
                            if c and not c['license']['distribute_profile']: add('LICENSE_SCOPE',ip,'aggregate distribution needs source profile permission')
                if a['role']=='template_layout':
                    f=maps['families'].get(a['owner_id'])
                    if f and f['publication_status']!='published': add('FAMILY_NOT_READY',ip,'published release requires published template family')
        if removed!={x['asset_id'] for x in r['items'] if x['change']=='remove'}: add('REMOVAL_STATE',path+'/removed','removed and remove items must agree')
        for j,dep in enumerate(r['dependencies']):
            target=maps['releases'].get(dep['release_id']);dp=path+'/dependencies/'+str(j)
            if not target or target['version']!=dep['version']: add('RELEASE_DEPENDENCY',dp,'资源依赖缺失: release version')
            else:
                release_edges[r['release_id']].append(dep['release_id'])
                if r['published_status']=='published' and target['published_status']!='published': add('RELEASE_DEPENDENCY',dp,'published release depends on unpublished release')
    visited=set();active=set()
    def visit(rid):
        if rid in active: add('RELEASE_CYCLE','/releases','release dependency cycle '+rid);return
        if rid in visited: return
        active.add(rid)
        for dep in release_edges.get(rid,[]): visit(dep)
        active.remove(rid);visited.add(rid)
    for rid in release_edges: visit(rid)
    # Delta releases inherit resources from locked dependencies; removals must name actual base items.
    if not any('[RELEASE_CYCLE]' in e for e in issues):
        effective_cache={}
        def effective(rid):
            if rid in effective_cache: return effective_cache[rid]
            r=maps['releases'][rid];items={}
            for dep in release_edges.get(rid,[]):
                for aid,item in effective(dep).items():
                    if aid in items and (items[aid]['version'],items[aid]['sha256'])!=(item['version'],item['sha256']):
                        add('RELEASE_CONFLICT','/releases/'+rid+'/dependencies','base versions conflict for '+aid)
                    items[aid]=item
            for item in r['items']:
                aid=item['asset_id']
                if item['change']=='remove':
                    old=items.get(aid)
                    if old is None or (old['version'],old['sha256'])!=(item['version'],item['sha256']):
                        add('REMOVAL_STATE','/releases/'+rid+'/items','removed item has no matching base version: '+aid)
                    items.pop(aid,None)
                else: items[aid]=item
            effective_cache[rid]=items;return items
        for r in groups['releases']:
            items=effective(r['release_id'])
            for item in items.values():
                a=assets.get(item['asset_id'])
                if not a or a['role']!='template_layout': continue
                f=maps['families'].get(a['owner_id'])
                if not f: continue
                required=[l['asset_ref'] for l in f['layout_refs']]+[f[k] for k in ['base_outline_ref','writing_policy_ref','figure_style_ref','table_style_ref','ruleset_ref']]
                if any(x is None or x['asset_id'] not in items or (x['version'],x['sha256'])!=(items[x['asset_id']]['version'],items[x['asset_id']]['sha256']) for x in required):
                    add('RELEASE_CLOSURE','/releases/'+r['release_id'],'template release omits dependent resources')
    return issues
