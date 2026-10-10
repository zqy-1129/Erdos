"""Executed independent safety/integration checks, with rollback-only SQL fixtures."""
import argparse,asyncio,json,os,re,shutil,subprocess,time,zipfile
from pathlib import Path
from . import audit,core,services
RUN=core.CONTENT_DIR/'normalized/cumcm_delivery/cumcm-2010-2025-codex-r002'

def sql():
    async def run():
        connection=await services.pg()
        checks=[]
        try:
            await connection.execute((core.CONTENT_DIR/'sql/cumcm/0002_codex_local.sql').read_text(encoding='utf-8'))
            outer=connection.transaction();await outer.start()
            try:
                await connection.execute("INSERT INTO content_de_codex.releases VALUES('qa-rollback',$1,'staging','{}',now())",'0'*64)
                negative=[('year_scope',"INSERT INTO content_de_codex.editions VALUES('qa-rollback',2026,'cumcm')"),
                          ('competition_scope',"INSERT INTO content_de_codex.editions VALUES('qa-rollback',2025,'mcm')"),
                          ('orphan_subproblem',"INSERT INTO content_de_codex.subproblems VALUES('qa-rollback','q','missing',1,'test','{}')"),
                          ('asset_version',"INSERT INTO content_de_codex.assets VALUES('qa-rollback','a',0,repeat('0',64),0,'k','{}')"),
                          ('vector_dimension',"SELECT '[1,2,3]'::vector(384)"),
                          ('duplicate_release',"INSERT INTO content_de_codex.releases VALUES('qa-rollback',repeat('0',64),'staging','{}',now())")]
                for name,statement in negative:
                    inner=connection.transaction();await inner.start()
                    try:
                        await connection.execute(statement);checks.append({'name':name,'rejected':False})
                    except Exception as exc:checks.append({'name':name,'rejected':True,'sqlstate':getattr(exc,'sqlstate',None)})
                    finally:await inner.rollback()
            finally:await outer.rollback()
            remaining=await connection.fetchval("SELECT count(*) FROM content_de_codex.releases WHERE release_id='qa-rollback'")
            if remaining or not all(c['rejected'] for c in checks):raise ValueError('database constraint QA failed')
            return {'checks':checks,'fixtures_remaining':remaining,'database':'real PostgreSQL','migration':'0002_codex_local.sql'}
        finally:await connection.close()
    core.write_json(RUN/'quality/postgres_constraints.json',asyncio.run(run()))

def archive():
    scope=core.load_json(core.CONTENT_DIR/'config/cumcm_delivery/scope.json')
    base=RUN/'quality/archive_negative';base.mkdir(parents=True,exist_ok=True)
    rows=[]
    for name,members in [('traversal',['../outside.txt']),('absolute',['C:/outside.txt']),('case_collision',['A.txt','a.txt']),('device',['CON.txt'])]:
        path=base/(name+'.zip')
        with zipfile.ZipFile(str(path),'w') as output:
            for member in members:output.writestr(member,b'qa source data')
        try:audit.seven_members(path,scope);passed=False
        except ValueError:passed=True
        rows.append({'name':name,'rejected':passed,'extract_attempted':False})
    if not all(r['rejected'] for r in rows):raise ValueError('archive safety QA failed')
    core.write_json(RUN/'quality/archive_negative.json',rows)

def templates():
    audit.deps();import profile_extraction as pe
    pdfium=pe.load_pdfium()
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plot=RUN/'quality/template_cases/qa-current-computation.png';plot.parent.mkdir(parents=True,exist_ok=True)
    fig,ax=plt.subplots(figsize=(4,2.5));ax.plot([0,1,2,3],[0,1,4,9],marker='o');ax.set_xlabel('x (unit)');ax.set_ylabel('y (unit)');fig.tight_layout();fig.savefig(str(plot),dpi=130);plt.close(fig)
    rows=[]
    for kind in ('typst','latex'):
        for count in (2,4):
            base=RUN/'quality/template_cases'/('%s-%d'%(kind,count))
            shutil.copytree(str(RUN/'templates'/kind),str(base),dirs_exist_ok=True)
            shutil.copyfile(str(plot),str(base/'qa-plot.png'))
            suffix='.typ' if kind=='typst' else '.tex'
            source=''.join(('= QAQuestion%d\n\nCurrent task result placeholder.\n\n'%i) if kind=='typst' else ('\\section{QAQuestion%d}\nCurrent task result placeholder.\n'%i) for i in range(1,count+1))
            source+=('\n$alpha+beta=gamma quad y=x^2$\n#figure(image("../qa-plot.png", width: 70%), caption: [QAPlot])\n#figure(table(columns: 2, [x], [y], [1], [1], [2], [4]), kind: table, caption: [QATable])\n' if kind=='typst' else '\n$\\alpha+\\beta=\\gamma,\\quad y=x^2$\n\\begin{figure}[ht]\\centering\\includegraphics[width=.7\\linewidth]{qa-plot.png}\\caption{QAPlot}\\end{figure}\n\\begin{table}[ht]\\centering\\begin{tabular}{rr}\\toprule x & y\\\\\\midrule 1 & 1\\\\ 2 & 4\\\\\\bottomrule\\end{tabular}\\caption{QATable}\\end{table}\n')
            core.write_bytes(base/'sections'/('question_sections'+suffix),source.encode())
            if kind=='typst':
                pdf=base/'preview.pdf';audit.command([audit.TYPST,'compile','--root',base,base/'main.typ',pdf])
            else:
                env=dict(os.environ,TECTONIC_CACHE_DIR=str(audit.RUNTIME/'tex-cache'),FONTCONFIG_FILE=str(audit.RUNTIME/'local/fonts.conf'))
                result=subprocess.run([str(audit.RUNTIME/'native/tectonic/tectonic.exe'),'--untrusted','--outdir',str(base),str(base/'main.tex')],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=300)
                core.write_bytes(base/'compile.log',result.stdout)
                if result.returncode:raise ValueError('dynamic LaTeX compilation failed')
                pdf=base/'main.pdf'
            doc=pdfium.PdfDocument(str(pdf));texts=[];dimensions=[]
            try:
                for page in doc:
                    dimensions.append(list(page.get_size()));tp=page.get_textpage()
                    try:texts.append(tp.get_text_range())
                    finally:tp.close();page.close()
            finally:doc.close()
            text='\n'.join(texts)
            matches=re.findall(r'QAQuestion\s*([1-9])',text)
            passed=sorted(map(int,matches))==list(range(1,count+1)) and 'QAPlot' in text and 'QATable' in text and all(abs(w-595.28)<2 and abs(h-841.89)<2 for w,h in dimensions)
            rows.append({'format':kind,'question_count':count,'extracted_question_markers':matches,'a4':True,'passed':passed,'pdf_sha256':core.sha256_of(pdf),'review_type':'agent_programmatic_compile_and_text_check'})
    if not all(r['passed'] for r in rows):raise ValueError('dynamic template QA failed')
    core.write_json(RUN/'quality/dynamic_section_tests.json',rows)

def protected():
    baseline=core.load_json('D:/Erdos/reports/trae/cumcm_codex_before/protected_baseline.json')
    results=[]
    for directory,data in baseline.items():
        base=Path(directory);changed=[];missing=[];added=[]
        for rel,expected in data['files'].items():
            p=base.joinpath(*rel.split('/'))
            if not p.is_file():missing.append(rel)
            elif p.stat().st_size!=expected['size'] or core.sha256_of(p)!=expected['sha256']:changed.append(rel)
        for parent,dirs,files in os.walk(str(base)):
            dirs[:]=[d for d in dirs if d not in ('__pycache__','.pytest_cache','.git')]
            for name in files:
                if name=='.env' or name.endswith('.pyc'):continue
                rel=(Path(parent)/name).relative_to(base).as_posix()
                if rel not in data['files']:added.append(rel)
        results.append({'root':directory,'expected_files':data['count'],'changed':changed,'missing':missing,'added':added,'passed':not changed and not missing and not added})
        print('protected verified '+directory,flush=True)
    report={'executed_at':core.now_utc_iso(),'roots':results,'all_passed':all(r['passed'] for r in results)}
    Path('D:/Erdos/reports/trae/protected_baseline_final_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if not report['all_passed']:raise ValueError('protected baseline drift; see report')

def structure(run=None):
    global RUN
    if run is not None: RUN=run
    problems=core.load_json(RUN/'catalog/problems.json');subs=core.load_json(RUN/'catalog/subproblems.json');papers=core.load_json(RUN/'catalog/papers.json')
    units=core.load_jsonl(RUN/'catalog/retrieval_units.jsonl');mappings=core.load_jsonl(RUN/'catalog/block_subproblem_links.jsonl')
    pids={r['problem_id'] for r in problems};sids={r['subproblem_id'] for r in subs};cids={r['case_id'] for r in papers}
    for row in subs:
        if row['problem_id'] not in pids or any(ref not in sids for ref in row['dependency_refs']):raise ValueError('unclosed subproblem relation')
    graph={r['subproblem_id']:r['dependency_refs'] for r in subs}
    def visit(n,path):
        if n in path:raise ValueError('dependency cycle')
        for child in graph[n]:visit(child,path|{n})
    for n in graph:visit(n,set())
    sources=core.load_jsonl(RUN/'sources/source_files.jsonl')+core.load_jsonl(RUN/'derived_sources.jsonl')+core.load_jsonl(RUN/'converted_sources.jsonl')
    ids={r['source_id'] for r in sources};shas={r['sha256'] for r in sources}
    for row in sources:
        if row.get('parent_source_id') and row['parent_source_id'] not in ids:raise ValueError('unclosed derivation source')
    blocks={};pages={};warnings=[];crop_checks=0;quality_counts={}
    for path in sorted((RUN/'parsed').glob('*/page-*.json')):
        page=core.load_json(path);key=(page['input_sha256'],page['page']);pages[key]=page
        quality_counts[page['quality']]=quality_counts.get(page['quality'],0)+1
        for b in page['blocks']:
            if b['block_id'] in blocks or b['source_sha256']!=key[0] or b['page']!=key[1]:raise ValueError('block identity/source drift')
            x0,y0,x1,y1=b['bbox']
            import math
            if not all(math.isfinite(x) for x in b['bbox']) or x1<x0 or y1<y0:raise ValueError('invalid block coordinates')
            if x0<-.5 or y0<-.5 or x1>page['width']+.5 or y1>page['height']+.5:warnings.append({'block_id':b['block_id'],'reason':'source bbox extends page; review source clipping'})
            if 'crop_path' in b:
                crop=core.safe_relpath(core.CONTENT_DIR,b['crop_path'])
                if core.sha256_of(crop)!=b['crop_sha256']:raise ValueError('crop evidence checksum drift')
                crop_checks+=1
            blocks[b['block_id']]=b
    for unit in units:
        if unit['source_sha256'] not in shas or (unit['source_sha256'],unit['page']) not in pages:raise ValueError('unit source/page missing')
        if unit['problem_id'] and unit['problem_id'] not in pids or unit['case_id'] and unit['case_id'] not in cids:raise ValueError('unit parent missing')
        members=[blocks[b] for b in unit['block_ids']]
        if '\n'.join(b['text'] for b in members)!=unit['text']:raise ValueError('unit text differs from whole source lines')
    uids={u['unit_id'] for u in units}
    if any(r['unit_id'] not in uids or r['subproblem_id'] not in sids for r in mappings):raise ValueError('many-to-many link missing')
    core.write_json(RUN/'quality/link_integrity_report.json',{'sources':len(sources),'pages':len(pages),'blocks':len(blocks),'units':len(units),'candidate_many_to_many_links':len(mappings),'crop_checks':crop_checks,'quality_counts':quality_counts,'coordinate_warnings':warnings,'references_closed':True,'dependency_acyclic':True,'semantic_quality_not_certified':True})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['sql','archive','templates','protected','structure'])
    args=parser.parse_args();globals()[args.phase]();print(args.phase+' QA passed')

if __name__=='__main__':main()
