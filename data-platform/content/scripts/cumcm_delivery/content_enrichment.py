"""Evidence-backed relationships, disjoint section ownership and local reference policy."""
import collections,json,re,statistics
from . import core,audit,local_closure,business_contracts as bc,visual_enrichment

STAGE={'abstract':'abstract','restatement':'analysis','analysis':'analysis','assumptions':'assumptions','symbols':'symbols',
    'solve':'algorithm','model':'model','validation':'validation','sensitivity':'sensitivity','discussion':'discussion','conclusion':'discussion',
    'references':'references','appendix':'appendix','toc':'toc'}
NUMBER={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}

def normalized(text):return re.sub(r'[^A-Za-z0-9\u3400-\u9fff]','',text).lower()
def body_title(problem):return re.sub(r'^[A-E]\s*题\s*','',problem['title']).strip()
def subrefs(title,pid,valid):
    result=[]
    for m in re.finditer(r'(?:问题|任务|第)\s*([一二三四五六七八九1-9](?:[、，,和及]\s*[一二三四五六七八九1-9])*)',title):
        for word in re.findall('[一二三四五六七八九1-9]',m[1]):
            ref=str(pid)+':q'+str(NUMBER.get(word,int(word) if word.isdigit() else 0))
            if ref in valid:result.append(ref)
    return list(dict.fromkeys(result))

def sections_for(paper,pages,valid):
    audit.deps();import profile_extraction as pe
    flat=[b for p in pages for b in p['blocks']]
    headings,toc=pe.detect_structure([p['text'] for p in pages],paper['source_sha256'],paper.get('problem_id'))
    lookup={}
    for h in headings:lookup[(h['page_range'][0],normalized(h['page_evidence']))]=h
    points=[]
    for idx,b in enumerate(flat):
        h=lookup.get((b['page'],normalized(b['text'])))
        if not h and normalized(b['text']) in ('摘要','abstract','参考文献','references','附录','appendix','目录'):
            title=re.sub(r'\s','',b['text']);h={'section_title':title,'section_type':pe.section_type(title),'level':1,'page_range':[b['page'],b['page']],'page_evidence':b['text'],'evidence':{'source_sha256':paper['source_sha256'],'locator_type':'page','start':b['page'],'end':b['page']}}
        if h:
            # Numerical fragments, formula operators and repeated body cross-references are not headings.
            if re.search('[=∑∫√≤≥∂±θ]|^sin|^cos|^tan',h['section_title'],re.I):continue
            if len(h['section_title'])>65 or h['section_title'].count('，')>=2:continue
            points.append((idx,h))
    if not flat:return [],[],{'status':'abstained','reason':'no located text blocks'}
    if not points or points[0][0]>0:points.insert(0,(0,{'section_title':'前置信息','section_type':'front_matter','level':1,'page_range':[1,1],'page_evidence':flat[0]['text'],'evidence':{'source_sha256':paper['source_sha256'],'locator_type':'page','start':1,'end':1}}))
    rows=[];stack=[];body_parts=collections.defaultdict(int);owned=set();revised=[]
    for i,(start,h) in enumerate(points):
        end=points[i+1][0] if i+1<len(points) else len(flat)
        if end<=start:continue
        while stack and stack[-1]['level']>=h['level']:stack.pop()
        parent=stack[-1] if stack else None
        sid='section-'+core.sha256_bytes(paper['case_id']+':'+flat[start]['block_id'])
        kind=h['section_type'];stage=STAGE.get(kind,parent['stage'] if parent else 'analysis')
        if '结果' in h['section_title'] or re.search('results',h['section_title'],re.I):stage='results'
        refs=subrefs(h['section_title'],paper.get('problem_id'),valid) or (parent['subproblem_refs'] if parent else [])
        blocks=flat[start:end];chars=sum(sum(not c.isspace() for c in b['text']) for b in blocks)
        row={'section_id':sid,'case_id':paper['case_id'],'parent_id':parent['section_id'] if parent else None,
            'level':h['level'],'title':h['section_title'],'start_block':blocks[0]['block_id'],'end_block':blocks[-1]['block_id'],
            'block_ids':[b['block_id'] for b in blocks],'recognized_chars':chars,'stage':stage,'subproblem_refs':refs,'semantic_status':'candidate'}
        bc.validate('section',row);rows.append(row);stack.append(row)
        for b in blocks:
            if b['block_id'] in owned:raise ValueError('section block double counted')
            owned.add(b['block_id'])
        bucket=kind if kind in ('front_matter','abstract','toc','references','appendix') else 'body'
        if blocks[0]['page'] in toc:bucket='toc'
        body_parts[bucket]+=chars
        revised.append(dict(h,section_id=sid,parent_section_id=row['parent_id'],exclusive_block_ids=row['block_ids'],
            exclusive_recognized_chars=chars,page_range=[blocks[0]['page'],blocks[-1]['page']],subproblem_refs=refs,boundary_status='source_anchored_candidate'))
    total=sum(sum(not c.isspace() for c in b['text']) for b in flat)
    if len(owned)!=len(flat) or sum(body_parts.values())!=total:raise ValueError('disjoint section accounting failed')
    length={'recognized_nonspace_chars':total,'disjoint_parts':dict(body_parts),'body_recognized_chars':body_parts['body'],
        'exclusive_section_ratios':[{'section_id':s['section_id'],'chars':s['recognized_chars'],'ratio_of_recognized_document':round(s['recognized_chars']/max(1,total),6)} for s in rows],
        'scope':'recognized located text; counts exclude neither lost OCR glyphs nor invisible math; not original author word count',
        'partition_complete':True,'semantic_boundaries':'candidate_until_source_review','is_original_length':False}
    return rows,revised,length

def title_relationship(paper,pages,problems):
    candidates=[]
    for p in problems:
        if p['year']!=paper['year']:continue
        title=body_title(p);needle=normalized(title)
        if len(needle)<6:continue
        for page in pages[:min(4,len(pages))]:
            if needle in normalized(page['text']):
                quote=next((b['text'] for b in page['blocks'] if needle in normalized(b['text'])),page['text'][:6000])
                candidates.append((p['problem_id'],{'source_sha256':paper['source_sha256'],'page':page['page'],
                    'quote':quote,'statement_sha256':p['statement_sources'][0]['sha256'],'matched_official_title':title,
                    'basis':'same edition, unique full official task title in first four paper pages; filename is not confirmation'}));break
    unique={p for p,_ in candidates}
    if len(unique)==1:return candidates[0]
    return None,{'source_sha256':paper['source_sha256'],'body_candidates':sorted(unique),'basis':'abstain on absent or conflicting source-title match'}

def enrich():
    rd=audit.run_dir(local_closure.args());problems=core.load_json(rd/'catalog/problems.json');papers=core.load_json(rd/'catalog/papers.json')
    subs=core.load_json(rd/'catalog/subproblems.json');valid={s['subproblem_id'] for s in subs};links=[];sections=[];owners={};counts=collections.Counter()
    problem_rows=[];sub_rows=[];paper_rows=[]
    for p in problems:
        row={'problem_id':p['problem_id'],'year':p['year'],'code':p['problem_code'],'title':p['title'],
            'statement_refs':[{'asset_id':'asset-'+s['sha256'],'version':1,'sha256':s['sha256']} for s in p['statement_sources']],
            'attachment_refs':[{'asset_id':'asset-'+s['sha256'],'version':1,'sha256':s['sha256']} for s in p['attachments']],
            'type_tags':p['problem_types'],'classification_status':'candidate'}
        bc.validate('problem',row);problem_rows.append(row)
    for s in subs:
        # Repair complete task boundaries using all numbered task headers, including Arabic lists.
        folder=rd/'parsed'/s['evidence']['source_sha256'];pages=[core.load_json(p) for p in sorted(folder.glob('page-*.json'))]
        blocks=[b for p in pages for b in p['blocks']];start=next((i for i,b in enumerate(blocks) if b['block_id']==s['evidence']['block_id']),None)
        if start is None:raise ValueError('subproblem source anchor missing')
        end=len(blocks)
        for idx in range(start+1,len(blocks)):
            value=blocks[idx]['text']
            m=re.match(r'^\s*(?:(?:问题|任务|问)\s*([一二三四五六七八九十1-9])(?:[：:、.\s]|$)|[（(]([1-9])[）)]\s*|([1-9])[．.、]\s*)',value)
            if m:
                token=next(g for g in m.groups() if g);number=NUMBER.get(token,int(token) if token.isdigit() else 0)
                if number>s['ordinal']:end=idx;break
        goal='\n'.join(b['text'] for b in blocks[start:end]).strip()
        s['goal']=goal;s['boundary_evidence']={'start_block':blocks[start]['block_id'],'last_block':blocks[end-1]['block_id'],'next_task_block':blocks[end]['block_id'] if end<len(blocks) else None,'source_sha256':s['evidence']['source_sha256'],'rule':'stop before next explicitly numbered task; no 2200-character truncation'}
        continuous=re.sub(r'(?<=[\u3400-\u9fff])\n(?=[\u3400-\u9fff])','',goal)
        sentences=[t.strip() for t in re.split('[。；]',continuous) if t.strip()]
        s['constraints']=[t for t in sentences if re.search('要求|不超过|至少|至多|不能|必须|约束|假设|满足',t)]
        s['deliverables']=[t for t in sentences if re.search('计算|求出|给出|确定|预测|估计|评价|设计|建立',t)]
        row={'subproblem_id':s['subproblem_id'],'problem_id':s['problem_id'],'ordinal':s['ordinal'],'goal':goal,
            'constraints':s['constraints'],'deliverables':s['deliverables'],'dependencies':s['dependency_refs'],
            'evidence':{'source_sha256':s['evidence']['source_sha256'],'page':s['evidence']['page'],'quote':goal[:8000],'block_ids':[b['block_id'] for b in blocks[start:end]]},'semantic_status':'candidate'}
        bc.validate('subproblem',row);sub_rows.append(row)
    for paper in papers:
        pages=[core.load_json(p) for p in sorted((rd/'parsed'/paper['source_sha256']).glob('page-*.json'))]
        paper['title']=visual_enrichment.repair_font_runs(paper['title'])[0]
        pid,evidence=title_relationship(paper,pages,problems)
        if pid:
            paper['problem_id']=pid;paper['relation_status']='source_body_title_match';paper['relation_evidence']=evidence
        elif paper['relation_status']!='verified_prior_snapshot':
            paper['relation_evidence']=evidence
        rows,structure,length=sections_for(paper,pages,valid);sections.extend(rows);paper['structure']=structure;paper['disjoint_recognized_length']=length
        paper['profile_version']=5
        for section in rows:
            for bid in section['block_ids']:owners[bid]=section
        counts[paper['relation_status']]+=1
        links.append({'case_id':paper['case_id'],'problem_id':paper['problem_id'],'status':paper['relation_status'],
            'evidence':paper.get('relation_evidence',{'source_sha256':paper['source_sha256'],'basis':'prior verified snapshot'}),'not_automatic_confirmation':paper['relation_status'] not in ('source_body_title_match','verified_prior_snapshot')})
        row={'case_id':paper['case_id'],'source_sha256':paper['source_sha256'],'year':paper['year'],'title':paper['title'],'page_count':paper['page_count'],
            'problem_id':paper['problem_id'],'relation_status':'source_checked' if paper['relation_status'] in ('source_body_title_match','verified_prior_snapshot') else 'candidate',
            'award':paper['award'],'award_status':'abstained','method_mentions':paper['method_tags'],'method_status':'candidate','compliance_note':paper['compliance_note']}
        bc.validate('paper',row);paper_rows.append(row)
    bysha={p['source_sha256']:p for p in papers};units=core.load_jsonl(rd/'catalog/retrieval_units.jsonl')
    for unit in units:
        paper=bysha.get(unit['source_sha256'])
        if paper:
            unit['problem_id']=paper['problem_id'];owned=[owners[b] for b in unit['block_ids'] if b in owners]
            if owned:
                section=owned[-1];unit['stage']=section['stage'];unit['section_id']=section['section_id'];unit['subproblem_refs']=section['subproblem_refs']
                unit['parent_section']={k:section[k] for k in ('section_id','title','level','stage','subproblem_refs','start_block','end_block','semantic_status')}
            page=core.load_json(rd/'parsed'/unit['source_sha256']/('page-%04d.json'%unit['page']))
            blocks={b['block_id']:b for b in page['blocks']};unit['text']='\n'.join(blocks[b]['text'] for b in unit['block_ids'])
            ordered=page['blocks'];positions=[i for i,b in enumerate(ordered) if b['block_id'] in set(unit['block_ids'])]
            unit['context_before']='\n'.join(b['text'] for b in ordered[max(0,min(positions)-2):min(positions)])
            unit['context_after']='\n'.join(b['text'] for b in ordered[max(positions)+1:max(positions)+3])
            unit['problem_types']=audit.feature_types(unit['text'])
            unit['full_page_asset_ref']=page.get('full_page_evidence',{}).get('asset_ref')
        unit['local_internal_allowed']=True;unit['local_scope']='team_internal';unit['quality_notice']='source-linked extraction candidate; inspect original page; no historical numeric result may be presented as current computation'
        unit['external_consumer_allowed']=False
    for name,rows in [('problems',problems),('subproblems',subs),('papers',papers),('paper_problem_links',links)]:core.write_json(rd/'catalog'/(name+'.json'),rows)
    core.write_jsonl(rd/'catalog/retrieval_units.jsonl',units)
    for name,rows in [('problem',problem_rows),('subproblem',sub_rows),('paper',paper_rows),('section',sections)]:core.write_jsonl(rd/'catalog/business_entities'/(name+'.jsonl'),rows)
    policy={'scope':'team_internal','authorized_by':'explicit user confirmation','historical_year_range':[2010,2025],
        'local_internal_retrieval_allowed':True,'distribute_original':False,'send_to_third_party_model':False,
        'allowed_runtime':'loopback authenticated API or isolated offline package','quality_rule':'source page is final evidence; candidate status must travel with content; abstain on uncertain formulas, titles, methods, awards and data'}
    core.write_json(rd/'quality/local_usage_policy.json',policy)
    report={'status':'pass','source_body_title_matches':counts['source_body_title_match'],'relations':dict(counts),
        'papers_with_disjoint_partition':len(papers),'exclusive_sections':len(sections),'subproblem_boundaries_rebuilt':len(subs),
        'historical_units':len(units),'original_length_claimed':False,'semantic_review_is_separate':True}
    core.write_json(rd/'quality/content_enrichment.json',report);local_closure.state('source_structure_and_boundaries','pass',['quality/content_enrichment.json'],report)
    refresh_catalog_report()
    print(json.dumps(report,ensure_ascii=False),flush=True)

def refresh_catalog_report():
    rd=audit.run_dir(local_closure.args());value=core.load_json(rd/'quality/catalog_report.json')
    links=core.load_json(rd/'catalog/paper_problem_links.json');counts=collections.Counter(r['status'] for r in links)
    value.update(confirmed_prior_links=counts['verified_prior_snapshot'],source_body_title_matches=counts['source_body_title_match'],
                 source_checked_links=counts['verified_prior_snapshot']+counts['source_body_title_match'],
                 pending_links=sum(v for k,v in counts.items() if k not in ('verified_prior_snapshot','source_body_title_match')),
                 relation_status_counts=dict(counts),relation_quality_notice='Source title matching establishes identity only, not full paper semantic verification.')
    core.write_json(rd/'quality/catalog_report.json',value)

def source_patterns():
    rd=audit.run_dir(local_closure.args());papers=core.load_json(rd/'catalog/papers.json');sections=core.load_jsonl(rd/'catalog/business_entities/section.jsonl')
    bycase={p['case_id']:p for p in papers};recipes=core.load_json(rd/'recipes/writing_recipes.json')
    for recipe in recipes:
        candidates=[]
        for section in sections:
            p=bycase[section['case_id']]
            if section['stage']!=recipe['stage'] or section['recognized_chars']<30:continue
            if recipe['problem_types'] and not any(t in recipe['problem_types'] for t in audit.feature_types(section['title'])):continue
            candidates.append((p,section))
        candidates=sorted(candidates,key=lambda x:(x[0]['year'],x[0]['case_id'],x[1]['section_id']))
        counts=[s['recognized_chars'] for _,s in candidates];n=len(counts)
        if not n:continue
        lengths=sorted(counts)
        recipe['source_backed_observations']={'sample_sections':n,'sample_papers':len({p['case_id'] for p,_ in candidates}),
            'year_coverage':sorted({p['year'] for p,_ in candidates}),
            'recognized_chars':{'p25':lengths[(n-1)//4],'p50':statistics.median(counts),'p75':lengths[3*(n-1)//4]},
            'counting_basis':'exclusive deepest-section ownership; recognized text only; source-boundary candidates, not human-calibrated author word count',
            'evidence':[{'case_id':p['case_id'],'source_sha256':p['source_sha256'],'section_id':s['section_id'],'title':s['title'],
                'source_ref':{'asset_id':'asset-'+p['source_sha256'],'version':1,'sha256':p['source_sha256']},'start_block':s['start_block'],'end_block':s['end_block'],'recognized_chars':s['recognized_chars']} for p,s in candidates[:12]],
            'scope':'local_team_internal','status':'source_pinned_extraction_observations'}
        recipe['length_budget']['historical_distribution']=recipe['source_backed_observations']['recognized_chars']
        recipe['length_budget']['not_official_requirement']=True
        recipe['local_internal_allowed']=True
    core.write_json(rd/'recipes/writing_recipes.json',recipes)
    core.write_json(rd/'quality/source_pattern_report.json',{'recipes_with_source_observations':sum('source_backed_observations' in r for r in recipes),
        'total_recipes':len(recipes),'recognized_length_only':True,'status':'source-backed extraction completed; semantic endorsement requires source review'})
    local_closure.state('source_backed_patterns','pass',['quality/source_pattern_report.json'])

if __name__=='__main__':enrich();source_patterns()
