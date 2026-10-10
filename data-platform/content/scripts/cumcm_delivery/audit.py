"""Source-bound repair pipeline. Local/internal candidates never imply publication clearance."""
import collections
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from . import core

RUNTIME = Path(os.path.abspath(os.environ.get('ERDOS_RUNTIME_DIR', str(core.CONTENT_DIR / '.runtime'))))
SEVEN = RUNTIME / 'native/sevenzip/Files/7-Zip/7z.exe'
TYPST = RUNTIME / 'native/typst/typst-x86_64-pc-windows-msvc/typst.exe'
PARSER_VERSION = 'pdfium-rapidocr-layout-candidate-v2'
_OCR = None
_DLL_HANDLES = []

def deps():
    sys.path.insert(0, str(RUNTIME / 'py38'))
    sys.path.insert(0, str(core.CONTENT_DIR / 'scripts'))

def run_dir(args):
    return core.safe_relpath(core.CONTENT_DIR, 'normalized/cumcm_delivery/' + args.run_id)

def command(argv, timeout=300):
    p = subprocess.run([str(a) for a in argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode:
        raise ValueError('command failed: ' + p.stderr.decode('utf-8', 'replace')[-1500:])
    return p.stdout

def source_path(row, args):
    if row.get('storage_path'):
        return core.safe_relpath(core.CONTENT_DIR, row['storage_path'])
    return core.safe_relpath(args.data_root, row['relative_path'])

def seven_members(src, scope):
    listing = command([SEVEN, 'l', '-slt', '-sccUTF-8', '--', src]).decode('utf-8-sig')
    if '\n----------\n' not in listing.replace('\r', ''):
        raise ValueError('invalid archive listing')
    body = listing.replace('\r', '').split('\n----------\n', 1)[1]
    out, seen, total = [], set(), 0
    for item in body.strip().split('\n\n'):
        fields = dict(line.split(' = ', 1) for line in item.splitlines() if ' = ' in line)
        if 'Path' not in fields:
            continue
        member = fields['Path'].replace('\\', '/').rstrip('/')
        core.safe_relpath(core.CONTENT_DIR / 'tmp_archive_validation', member)
        if member.casefold() in seen:
            raise ValueError('duplicate/case-colliding archive path')
        seen.add(member.casefold())
        if fields.get('Encrypted') == '+' or any(k.lower().endswith('link') for k in fields):
            raise ValueError('encrypted/link member rejected')
        if fields.get('Folder') == '+' or fields.get('Attributes', '').startswith('D'):
            continue
        size = int(fields.get('Size', '0'))
        total += size
        out.append((fields['Path'], member, size))
    if len(seen) > scope['max_archive_members'] or total > scope['max_expand_bytes_per_archive']:
        raise ValueError('archive resource limit exceeded')
    return out

def expand(args):
    from . import inventory
    rd = run_dir(args)
    if not (rd / 'sources/source_files.jsonl').exists():
        inventory.main(args)
    records = core.load_jsonl(rd / 'sources/source_files.jsonl')
    scope = core.load_scope(args.scope_config)
    queue = [(r, 0) for r in records if r['in_scope'] and r['status'] == 'ok' and r['extension'] in ('.rar', '.zip', '.7z')]
    derived, failures, archive_rows = [], [], []
    for a, depth in queue:
        try:
            src = source_path(a, args)
            if core.sha256_of(src) != a['sha256']:
                raise ValueError('source changed since inventory')
            if depth >= scope['max_archive_depth']:
                raise ValueError('archive nesting limit')
            members = seven_members(src, scope)
            for original, member, size in members:
                dest = rd / 'expanded' / a['sha256'] / member
                if dest.exists():
                    core.no_links(dest)
                    data = dest.read_bytes()
                else:
                    data = command([SEVEN, 'x', '-so', '-spd', '--', src, original])
                    if len(data) != size:
                        raise ValueError('extracted length mismatch: ' + member)
                    core.write_bytes(dest, data, immutable=True)
                if len(data) != size:
                    raise ValueError('cached member length mismatch')
                sha = core.sha256_bytes(data)
                row = dict(a, source_id='cumcm-derived-' + core.sha256_bytes(a['source_id'] + '/' + member)[:24],
                           content_id='sha256:' + sha, sha256=sha, size=size,
                           extension=Path(member).suffix.lower(), storage_path=dest.relative_to(core.CONTENT_DIR).as_posix(),
                           parent_source_id=a['source_id'], archive_member=member, derivation='7zip-26.03',
                           relative_path=a['relative_path'] + '!/' + member)
                derived.append(row)
                if row['extension'] in ('.rar', '.zip', '.7z'):
                    queue.append((row, depth + 1))
            archive_rows.append({'source_id': a['source_id'], 'sha256': a['sha256'], 'members': len(members), 'depth': depth, 'status': 'ok'})
        except Exception as e:
            failures.append({'source_id': a['source_id'], 'reason': str(e), 'depth': depth})
        print('archive {}/{} derived={} failures={}'.format(len(archive_rows) + len(failures), len(queue), len(derived), len(failures)), flush=True)
    core.write_jsonl(rd / 'derived_sources.jsonl', derived)
    core.write_json(rd / 'quality/archive_report.json', {'archives': archive_rows, 'failures': failures, 'tool_sha256': core.sha256_of(SEVEN)})
    return 1 if failures else 0

def all_sources(args):
    rd = run_dir(args)
    rows = core.load_jsonl(rd / 'sources/source_files.jsonl')
    if (rd / 'derived_sources.jsonl').exists():
        rows += core.load_jsonl(rd / 'derived_sources.jsonl')
    return [r for r in rows if r['in_scope'] and r['status'] == 'ok' and r['size']]

def convert(args):
    rd = run_dir(args)
    converted, failures = [], []
    unique = {}
    for row in all_sources(args):
        unique.setdefault(row['sha256'],row)
    jobs = []
    for row in unique.values():
        if row['extension'] in ('.doc', '.docx'):
            target = rd / 'converted' / row['sha256'] / 'source.pdf'
            if not target.exists():
                copy = target.parent / ('source' + row['extension'])
                core.write_bytes(copy, source_path(row, args).read_bytes(), immutable=True)
                jobs.append({'id':row['source_id'],'input':str(copy),'output':str(target)})
    if jobs:
        jobs_path = rd / 'quality/conversion_jobs.json'
        core.write_json(jobs_path, jobs)
        try:
            log = command(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',core.CONTENT_DIR/'scripts/cumcm_convert_batch.ps1','-JobsPath',jobs_path],timeout=1800)
            core.write_bytes(rd/'quality/conversion.log',log)
        except Exception as e:
            failures.append({'source_id':'batch','reason':str(e)})
    for row in unique.values():
        if row['extension'] not in ('.doc', '.docx'):
            continue
        target = rd / 'converted' / row['sha256'] / 'source.pdf'
        try:
            if not target.exists():
                raise ValueError('batch conversion produced no PDF')
            if not target.is_file() or not target.read_bytes().startswith(b'%PDF'):
                raise ValueError('conversion produced no PDF')
            converted.append(dict(row, source_id='cumcm-converted-' + row['sha256'], parent_source_id=row['source_id'],content_id='sha256:'+core.sha256_of(target),
                                  storage_path=target.relative_to(core.CONTENT_DIR).as_posix(), extension='.pdf',
                                  sha256=core.sha256_of(target), size=target.stat().st_size, derivation='WPS-PDF-copy'))
        except Exception as e:
            failures.append({'source_id': row['source_id'], 'reason': str(e)})
        print('converted {} failures={}'.format(len(converted),len(failures)), flush=True)
        core.write_jsonl(rd / 'converted_sources.jsonl', converted)
        core.write_json(rd / 'quality/conversion_report.json', {'converted':len(converted),'failures':failures,'complete':False})
    core.write_jsonl(rd / 'converted_sources.jsonl', converted)
    core.write_json(rd / 'quality/conversion_report.json', {'converted': len(converted), 'failures': failures,'complete':True})
    return converted

def ocr_engine():
    global _OCR
    if _OCR is not None:
        return _OCR
    deps()
    gpu=RUNTIME/'py38-gpu119'
    if gpu.is_dir():
        sys.path.insert(0,str(gpu))
        import torch
        if not torch.cuda.is_available():
            raise ValueError('configured GPU OCR runtime has no available CUDA device')
        if hasattr(os,'add_dll_directory'):
            _DLL_HANDLES.append(os.add_dll_directory(str(Path(torch.__file__).parent/'lib')))
        torch.cuda.init()
    previous = Path.resolve
    try:
        Path.resolve = lambda self, strict=False: self.absolute()
        from rapidocr_onnxruntime import RapidOCR
        from rapidocr_onnxruntime.utils import OrtInferSession
        original=OrtInferSession._get_ep_list
        def providers(instance):
            entries=original(instance)
            for provider,options in entries:
                if provider=='CUDAExecutionProvider':
                    options.update(cudnn_conv_algo_search='DEFAULT',gpu_mem_limit=1536*1024*1024,arena_extend_strategy='kSameAsRequested')
            return entries
        OrtInferSession._get_ep_list=providers
        _OCR = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1, det_use_cuda=gpu.is_dir(), cls_use_cuda=gpu.is_dir(), rec_use_cuda=gpu.is_dir())
        if gpu.is_dir():
            sessions=(_OCR.text_det.infer.session,_OCR.text_cls.infer.session,_OCR.text_rec.session.session)
            if any(s.get_providers()[0]!='CUDAExecutionProvider' for s in sessions):
                raise ValueError('GPU OCR was configured but CUDA provider failed; no silent fallback')
        return _OCR
    finally:
        Path.resolve = previous

def text_lines(tp):
    text = tp.get_text_range()
    rows, offset = [], 0
    for line in text.splitlines(keepends=True):
        boxes = []
        try:
            # PDFium computes text span rectangles in native code; per-character Python FFI is prohibitively slow.
            length = min(len(line.rstrip()), max(0,tp.count_chars()-offset))
            for i in range(tp.count_rects(offset,length)) if length else []:
                b=tp.get_rect(i)
                if b[2]>b[0] and b[3]>b[1]:
                    boxes.append(b)
        except Exception:
            pass
        if line.strip() and boxes:
            box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
            rows.append({'text': line.strip(), 'bbox': box, 'confidence': None, 'method': 'pdf_text'})
        offset += len(line)
    return text, rows

def parse(args):
    deps()
    import profile_extraction as pe
    pdfium = pe.load_pdfium()
    rd = run_dir(args)
    converted = core.load_jsonl(rd/'converted_sources.jsonl') if getattr(args,'worker_shas',None) else convert(args)
    unique = {}
    for row in all_sources(args) + converted:
        if row['extension'] == '.pdf':
            unique.setdefault(row['sha256'], row)
    pdfs = sorted(unique.values(), key=lambda r: (r['root_kind'] != 'problem', r['year'], r['relative_path']))
    if args.max_documents:
        pdfs = pdfs[:args.max_documents]
    if getattr(args,'worker_shas',None):
        pdfs=[r for r in pdfs if r['sha256'] in args.worker_shas]
    elif args.ocr and len(pdfs)>8:
        from concurrent.futures import ProcessPoolExecutor,wait,FIRST_COMPLETED
        import copy
        workers=min(2,max(1,(os.cpu_count() or 4)//2))
        jobs=[]
        for i in range(workers):
            worker=copy.deepcopy(args)
            worker.worker_shas=[r['sha256'] for r in pdfs[i::workers]]
            worker.worker_index=i
            jobs.append(worker)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            pending={pool.submit(parse,w) for w in jobs}
            while pending:
                done,pending=wait(pending,timeout=15,return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
                reports=[core.load_json(rd/('quality/parse_shard_%d.json'%i)) for i in range(workers) if (rd/('quality/parse_shard_%d.json'%i)).exists()]
                combined={'parser_version':PARSER_VERSION,'input_pdf_count':len(pdfs),'processed':sum(r['processed'] for r in reports),'documents':[d for r in reports for d in r['documents']],
                          'failures':[d for r in reports for d in r['failures']],'complete':not pending and len(reports)==workers and all(r['complete'] for r in reports),'workers':workers}
                core.write_json(rd/'quality/parse_report.json',combined)
        return 1 if combined['failures'] else 0
    engine = ocr_engine() if args.ocr else None
    coverage, failures = [], []
    for index, row in enumerate(pdfs):
        out = rd / 'parsed' / row['sha256']
        out.mkdir(parents=True, exist_ok=True)
        try:
            src = source_path(row, args)
            if core.sha256_of(src) != row['sha256']:
                raise ValueError('source hash changed')
            doc = pdfium.PdfDocument(str(src))
            page_count = len(doc)
            page_rows = []
            try:
                for pi in range(page_count):
                    checkpoint = out / ('page-%04d.json' % (pi + 1))
                    if checkpoint.exists():
                        cached = core.load_json(checkpoint)
                        if cached['input_sha256'] == row['sha256'] and cached['parser_version'] == PARSER_VERSION and (cached['method'] == 'ocr' or not args.ocr or cached['quality'] == 'readable'):
                            page_rows.append(cached)
                            continue
                    page = doc[pi]
                    tp = page.get_textpage()
                    try:
                        text, lines = text_lines(tp)
                        readable = len(re.sub(r'\s', '', text)) >= 20 and '\ufffd' not in text
                        method = 'pdf_text'
                        width, height = page.get_size()
                        image = None
                        if not readable and engine:
                            image = page.render(scale=2).to_pil().convert('RGB')
                            result, _ = engine(image)
                            lines = []
                            for polygon, value, confidence in result or []:
                                xs, ys = zip(*polygon)
                                box = [min(xs)/2, height-max(ys)/2, max(xs)/2, height-min(ys)/2]
                                lines.append({'text': value, 'bbox': box, 'confidence': float(confidence), 'method': 'ocr'})
                            text = '\n'.join(l['text'] for l in lines)
                            method = 'ocr'
                            readable = len(re.sub(r'\s', '', text)) >= 20
                        blocks = []
                        for li, line in enumerate(lines):
                            value = line['text']
                            kind = 'text'
                            if re.match(r'^\s*(图|表)\s*[0-9一二三四五六七八九十]+', value):
                                kind = 'figure_caption' if value.lstrip().startswith('图') else 'table_caption'
                            elif re.match(r'^(摘要|参考文献|附录|[一二三四五六七八九十]+[、.]|\d+(?:\.\d+)*\s+[^\d])', value) and len(value) < 70:
                                kind = 'heading_candidate'
                            elif re.search(r'[=∑∫√≤≥∂±]|\([0-9]+\)\s*$', value):
                                kind = 'formula_candidate'
                            block = dict(line, block_id='block-' + row['sha256'] + '-p%d-l%d' % (pi+1, li+1), kind=kind,
                                         page=pi+1, bbox_coordinate_system='pdf_points_bottom_left', source_sha256=row['sha256'],
                                         review_status='machine_candidate', transcription_verified=False)
                            if kind in ('formula_candidate', 'figure_caption', 'table_caption'):
                                # A crop is source evidence, not an inferred formula transcription or chart series.
                                b = line['bbox']
                                margin = 3
                                crop = (max(0,b[0]-margin), max(0,b[1]-margin), min(width,b[2]+margin), min(height,b[3]+margin))
                                if image is None:
                                    image=page.render(scale=2).to_pil().convert('RGB')
                                evidence=image.crop((round(crop[0]*2),round((height-crop[3])*2),round(crop[2]*2),round((height-crop[1])*2)))
                                buf = io.BytesIO(); evidence.save(buf, format='PNG')
                                cp = out / 'crops' / ('p%d-l%d-%s.png' % (pi+1,li+1,core.sha256_bytes(buf.getvalue())[:12]))
                                core.write_bytes(cp, buf.getvalue(), immutable=True)
                                block['crop_path'] = cp.relative_to(core.CONTENT_DIR).as_posix()
                                block['crop_sha256'] = core.sha256_of(cp)
                                block['crop_scope'] = 'caption_or_formula_line_only'
                            blocks.append(block)
                        payload = {'input_sha256': row['sha256'], 'parser_version': PARSER_VERSION, 'page': pi+1,
                                   'width': width, 'height': height, 'method': method, 'quality': 'readable' if readable else 'needs_review',
                                   'inference_provider':engine.text_det.infer.session.get_providers()[0] if method=='ocr' else None,
                                   'text': text, 'blocks': blocks, 'formula_semantics_verified': False, 'figure_series_reconstructed': False}
                        core.write_json(checkpoint, payload)
                        page_rows.append(payload)
                    finally:
                        tp.close(); page.close()
                    if (pi + 1) % 20 == 0:
                        print('parse document {}/{} page {}/{}'.format(index+1,len(pdfs),pi+1,page_count), flush=True)
            finally:
                doc.close()
            core.write_json(out / 'document.json', {'source': row, 'input_sha256': row['sha256'], 'parser_version': PARSER_VERSION,
                                                  'page_count': page_count, 'ocr_pages': sum(p['method']=='ocr' for p in page_rows),
                                                  'needs_review_pages': sum(p['quality']!='readable' for p in page_rows)})
            coverage.append({'source_id': row['source_id'], 'sha256': row['sha256'], 'year': row['year'], 'root_kind': row['root_kind'],
                             'pages':page_count,'ocr_pages':sum(p['method']=='ocr' for p in page_rows),'needs_review_pages':sum(p['quality']!='readable' for p in page_rows)})
        except Exception as e:
            failures.append({'source_id': row['source_id'], 'reason': str(e)})
        reportname='parse_shard_%d.json'%args.worker_index if getattr(args,'worker_shas',None) else 'parse_report.json'
        core.write_json(rd / 'quality' / reportname, {'parser_version':PARSER_VERSION, 'input_pdf_count':len(pdfs), 'processed':len(coverage), 'documents':coverage,'failures':failures, 'complete':index+1==len(pdfs)})
        print('parsed {}/{} failures={}'.format(index+1,len(pdfs),len(failures)), flush=True)
    return 1 if failures else 0

def main(args):
    if args.phase == 'expand':
        return expand(args)
    if args.phase == 'parse':
        return parse(args)
    if args.phase == 'import':
        return import_release(args)
    return globals()[args.phase](args)


def normalize_sources(args):
    """Portable asset references belong in every new run, including incremental builds."""
    rd=run_dir(args)
    converted=core.load_jsonl(rd/'converted_sources.jsonl')
    for row in converted:row['content_id']='sha256:'+row['sha256']
    core.write_jsonl(rd/'converted_sources.jsonl',converted)
    for path in (rd/'parsed').glob('*/document.json'):
        doc=core.load_json(path)
        doc['source']['content_id']='sha256:'+doc['input_sha256']
        doc['source']['asset_ref']={'asset_id':'asset-'+doc['input_sha256'],'version':1,'sha256':doc['input_sha256']}
        core.write_json(path,doc)
    for path in (rd/'parsed').glob('*/page-*.json'):
        page=core.load_json(path);changed=False
        for block in page['blocks']:
            if 'crop_sha256' in block:
                block['crop_asset_ref']={'asset_id':'asset-'+block['crop_sha256'],'version':1,'sha256':block['crop_sha256']}
                block['crop_path_role']='local_build_checkpoint_provenance; consumer downloads crop_asset_ref'
                changed=True
        if changed:core.write_json(path,page)
    stale=rd/'quality/parse_shard_2.json'
    if stale.exists():
        record=core.load_json(stale);record['obsolete_checkpoint']=True
        core.write_json(rd/'quality/diagnostics/parse_shard_2_obsolete.json',record)
        core.output_path(stale).unlink()
    return 0

def templates(args):
    from . import templates as old
    out = run_dir(args) / 'templates'
    old._copy_tree(old.SRC_TEMPLATE/'zh-latex',out/'latex')
    old._copy_tree(old.SRC_TEMPLATE/'zh',out/'typst')
    fonts = command([TYPST,'fonts']).decode('utf-8')
    required = ['SimSun','SimHei','KaiTi','Times New Roman','Courier New']
    if any(f not in fonts.splitlines() for f in required):
        raise ValueError('required Windows fonts missing')
    tp = out / 'typst/main.typ'
    text = tp.read_text(encoding='utf-8')
    text = text.replace('#let hei-font = ("Heiti SC", "STHeiti", "Songti SC", "STSong")', '#let hei-font = ("SimHei",)')
    text = text.replace('#toc-page()\n', '// No table of contents in submission profile.\n')
    text = text.replace('#show heading.where(level: 1): set text(size:', '#show heading.where(level: 1): set text(font: hei-font, size:')
    text = text.replace('#show heading.where(level: 2): set text(size:', '#show heading.where(level: 2): set text(font: hei-font, size:')
    text = text.replace('#show heading.where(level: 3): set text(size:', '#show heading.where(level: 3): set text(font: hei-font, size:')
    text=text.replace('text(size: 17.3pt, weight: "bold")','text(font: hei-font, size: 17.3pt, weight: "bold")')
    text=text.replace('text(size: 14pt, weight: "bold")','text(font: hei-font, size: 14pt, weight: "bold")')
    text=text.replace('block(above: 0.15em)[#body]','block(above: 0.8em)[#body]')
    text=text.replace('#include("sections/5_problem1.typ")\n#include("sections/6_problem2.typ")\n#include("sections/7_problem3.typ")', '#include("sections/question_sections.typ")')
    core.write_bytes(tp,text.encode())
    lp = out / 'latex/main.tex'
    text = lp.read_text(encoding='utf-8').replace('fontset=mac','fontset=windows')
    text = text.replace('\\thispagestyle{empty}', '\\thispagestyle{plain}')
    text = re.sub(r'^\\tocpage\s*$', '% No table of contents in submission profile.', text, flags=re.M)
    text = text.replace('\\IfFontExistsTF{Menlo}{\\setmonofont{Menlo}}{}', '\\setmonofont{Courier New}')
    text=text.replace('\\input{sections/5_problem1}\n\\input{sections/6_problem2}\n\\input{sections/7_problem3}', '\\input{sections/question_sections}')
    text='% CUMCM Windows format scaffold; compile with XeTeX-compatible Tectonic or XeLaTeX.\n% Product font defaults are not national mandatory font rules. No table of contents.\n'+text[re.search(r'^\\documentclass',text,re.M).start():]
    text=text.replace('\\usepackage{amsmath}','\\usepackage{amsmath}\n\\usepackage{graphicx}')
    text=text.replace('\\IfFontExistsTF{Times New Roman}{\\setmainfont{Times New Roman}}{}','\\setmainfont{Times New Roman}')
    text=text.replace('\\fontsize{17.3pt}{22pt}\\bfseries','\\fontsize{17.3pt}{22pt}\\heiti\\bfseries')
    core.write_bytes(lp,text.encode())
    # The original samples contain invented model scores. A format bundle must never seed current-task results.
    for kind,suffix in [('typst','.typ'),('latex','.tex')]:
        for section in (out/kind/'sections').glob('*'+suffix):
            lines=section.read_text(encoding='utf-8').splitlines()
            heading=next((l for l in lines if l.startswith('= ') if kind=='typst'),None) if kind=='typst' else next((l for l in lines if l.startswith('\\section{')),None)
            if not heading:
                heading='= 附录' if kind=='typst' else '\\section{附录}'
            comment='// 内容由当前任务结果与 document_node 生成。此模板不提供历史数值或指定模型。\n' if kind=='typst' else '% 内容由当前任务结果与 document_node 生成，不提供历史数值或指定模型。\n'
            core.write_bytes(section,((heading+'\n' if section.stem!='A_code' else '')+comment).encode())
        references=out/kind/('references'+suffix)
        if references.exists():core.write_bytes(references,('// 仅插入当前论文实际引用的参考文献。\n' if kind=='typst' else '\\section*{参考文献}\n% 仅插入当前论文实际引用的参考文献。\n').encode())
        entry=out/kind/'sections'/('question_sections'+suffix)
        core.write_bytes(entry,('// 客户端根据实际小问数生成章节列表。\n' if kind=='typst' else '% 客户端根据实际小问数生成章节列表。\n').encode())
        for name in ('5_problem1','6_problem2','7_problem3'):
            core.output_path(out/kind/'sections'/(name+suffix)).unlink()
    core.write_json(out/'dynamic_section_contract.json',{'entry':{'typst':'sections/question_sections.typ','latex':'sections/question_sections.tex'},'question_count':'from current task, never fixed to three','source':'document_node with current result_card refs','template_status':'unfilled scaffold; not submission-ready','forbidden_seed_values':['historical_result_numbers','original example model scores'],'required_before_export':['no empty result sections','all numeric conclusions trace current computation','no identity disclosure','selected edition validation']})
    preview = out / 'typst-preview.pdf'
    command([TYPST,'compile','--root',out / 'typst',tp,preview])
    tectonic = RUNTIME/'native/tectonic/tectonic.exe'
    latex_out = out/'latex-preview'
    latex_out.mkdir(parents=True,exist_ok=True)
    env = dict(os.environ, TECTONIC_CACHE_DIR=str(RUNTIME/'tex-cache'), FONTCONFIG_FILE=str(RUNTIME/'local/fonts.conf'))
    compiled = subprocess.run([str(tectonic),'--untrusted','--keep-logs','--outdir',str(latex_out),str(lp)],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=900)
    core.write_bytes(run_dir(args)/'quality/latex_compile.log',compiled.stdout)
    if compiled.returncode or not (latex_out/'main.pdf').is_file():
        raise ValueError('LaTeX template compilation failed; see quality/latex_compile.log')
    # Bundles are complete download units; compiler/runtime and licensed fonts remain client prerequisites.
    core.write_json(out / 'font_manifest.json', {'fonts':required,'available':True,'selection':{'body':'SimSun and Times New Roman','heading':'SimHei','emphasis':'KaiTi'},'policy':'product typography defaults; not national mandatory font rules','font_binary_distribution':False,'typst_version':command([TYPST,'--version']).decode().strip(),'preview_sha256':core.sha256_of(preview)})
    core.write_json(out / 'ruleset.json', {'ruleset_id':'cumcm-format-2025-2026-v2','historical_scope':[2010,2025],'target_format_edition_required':True,
        'editions':[{'year':2025,'source_url':'https://www.cmathc.org.cn/mcm/tz/303.html','source_kind':'CUMCM attributed mirror','body_page_limit':20,'body_page_limit_kind':'recommendation','requires_primary_archive_verification':True},
                    {'year':2026,'source_url':'https://www.mcm.edu.cn/upload_cn/node/775/cQMeL0YY905244c8bd4b9af832f1699446d8385e.pdf','source_kind':'organizer_pdf','body_page_limit':30,'body_page_limit_kind':'maximum'}],
        'common_constraints':{'paper':'A4','margin_min_cm':2.5,'toc_allowed':False,'abstract_max_pages':1,'electronic_first_page':'abstract','identity_in_body_allowed':False,'file_size_max_mb':20,'national_font_size_uniform_requirement':False},
        'preview_is_submission_certification':False,'district_requirements':'must be supplied separately','verified_date':'2026-10-06'})
    packages=[]
    for kind in ('typst','latex'):
        for original,inside in [('ruleset.json','erdos-ruleset.json'),('font_manifest.json','erdos-fonts.json'),('dynamic_section_contract.json','erdos-dynamic-sections.json')]:
            core.write_json(out/kind/inside,core.load_json(out/original))
        zipfile=out/('cumcm-'+kind+'-v2.zip')
        old._deterministic_zip(out/kind,zipfile)
        packages.append({'kind':kind,'file':zipfile.name,'sha256':core.sha256_of(zipfile),'compiled':True})
    core.write_json(out/'dependency_manifest.json',{'packages':packages,'client_prerequisites':{'typst':'0.15.1','latex':'XeTeX-compatible compiler; tested Tectonic 0.15.0','fonts':required},'font_files_bundled':False,'included_files':{k:[p.relative_to(out/k).as_posix() for p in sorted((out/k).rglob('*')) if p.is_file()] for k in ('typst','latex')}})
    core.write_json(out / 'format_readiness.json', {'typst':{'compiled':True,'fonts_available':True,'visual_review':'pending'},'latex':{'compiled':True,'fonts_available':True,'source_windows_fixed':True,'visual_review':'pending','preview_sha256':core.sha256_of(latex_out/'main.pdf')},'packages':packages,'official_submission_validation':'requires filled paper and selected edition; no TOC; metadata/source text must be checked for identity'})
    print('Typst and LaTeX compiled, Windows fonts selected, both download packages built')
    return 0

TYPE_WORDS = {'optimization':['最优','最大','最小','调度','规划','优化'], 'prediction':['预测','预报'],
              'evaluation':['评价','评估','排序'], 'statistical_analysis':['回归','统计','估计','拟合'],
              'mechanism':['机理','运动','动力','几何','微分','轨迹'], 'simulation':['模拟','仿真']}

def feature_types(text):
    return [kind for kind, words in TYPE_WORDS.items() if any(word in text for word in words)] or ['unknown']

def paragraph_groups(blocks):
    """Candidate context groups retain complete lines and adjacent derivation evidence."""
    start=0;size=0
    for index,block in enumerate(blocks):
        heading=block['kind']=='heading_candidate'
        if index>start and ((heading and size>=70) or size>=1800):
            yield start,blocks[start:index]
            start=index;size=0
        size+=len(block['text'])+1
        sentence_end=bool(re.search(r'[。；.!?！？]$',block['text'].strip()))
        next_visual=index+1<len(blocks) and blocks[index+1]['kind'] in ('formula_candidate','figure_caption','table_caption')
        if size>=700 and sentence_end and not next_visual:
            yield start,blocks[start:index+1]
            start=index+1;size=0
    if start<len(blocks):yield start,blocks[start:]

def problem_code(row, text=''):
    match=re.search(r'^\s*([A-E])\s*题(?:\s|[：:]|[\u3400-\u9fff])',text[:1000],re.M)
    if match:return match[1]
    name = row.get('archive_member', row['relative_path'].split('/')[-1])
    matches = re.findall(r'(?:^|[^A-Za-z])([A-E])\s*(?:题|\.(?:pdf|docx?)$|论文|[0-9])', name, re.I)
    if matches:
        return matches[-1].upper()
    match=re.search(r'(?:problem[-_ ]*|cumcm[-_ ]*\d{4}[-_ ]*|\d{4})([A-E])(?:[-_ .]|$)',name,re.I)
    if match:return match[1].upper()
    match = re.search(r'([A-E])\s*题',text[:1000])
    if match:return match[1]
    for part in reversed(row['relative_path'].split('/')[:-1]):
        match=re.fullmatch(r'(?:\d{4}[-_])?([A-E])(?:题|[-_]\d{4}中文|[-_]Chinese)?',part,re.I)
        if match:return match[1].upper()
    return None

def catalog(args):
    deps()
    import profile_extraction as pe
    rd = run_dir(args)
    source_rows = all_sources(args)
    if (rd/'converted_sources.jsonl').exists():
        source_rows += core.load_jsonl(rd/'converted_sources.jsonl')
    source_by_id={r['source_id']:r for r in source_rows}
    documents = {}
    for path in sorted((rd/'parsed').glob('*/document.json')):
        doc = core.load_json(path)
        pages = [core.load_json(p) for p in sorted(path.parent.glob('page-*.json'))]
        if len(pages) != doc['page_count']:
            raise ValueError('document checkpoint incomplete')
        documents[doc['input_sha256']] = doc,pages
        if len(documents)%50==0:print('catalog read {} completed documents'.format(len(documents)),flush=True)
    problems, subproblems, papers, links, units, mappings = {},[],[],[],[],[]
    for sha,(doc,pages) in documents.items():
        row = doc['source']
        if row['root_kind'] != 'problem':
            continue
        text = '\n'.join(p['text'] for p in pages)
        name = row.get('archive_member',row['relative_path'].split('/')[-1])
        code = problem_code(row,text)
        if not code or re.search(r'附件|说明|支撑|补充|appendix|readme|format|参赛|规范',name,re.I):
            continue
        pid = 'cumcm-%d-%s' % (row['year'],code)
        title_lines = [s.strip() for s in pages[0]['text'].splitlines() if s.strip()]
        title = next((s for s in title_lines if re.search(r'[A-E]\s*题',s)), title_lines[0] if title_lines else name)
        profile = problems.setdefault(pid,{'problem_id':pid,'competition_id':'cumcm','year':row['year'],'problem_code':code,'title':title,
                                         'statement_sources':[],'problem_types':feature_types(text),'classification_status':'machine_candidate',
                                         'attachments':[],'review_status':'pending_review','version':2})
        profile['statement_sources'].append({'sha256':sha,'source_id':row['source_id'],'pages':len(pages)})
        if any(s['problem_id']==pid for s in subproblems):
            continue
        # Preserve locators and original task language. Numbering candidates need semantic review.
        occurrences = []
        for page in pages:
            for block in page['blocks']:
                m = re.match(r'^\s*(?:问题|任务|问)\s*([一二三四五六七八九十1-9])(?:[：:、.\s]|$)',block['text'])
                if m:
                    number = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}.get(m[1],int(m[1]) if m[1].isdigit() else 0)
                    occurrences.append((number,page['page'],block))
        if not occurrences:
            for page in pages:
                for block in page['blocks']:
                    m=re.match(r'^\s*(?:[（(]([1-9])[）)]|([1-9])[．.、])\s*(.{8,})',block['text'])
                    if m and re.search(r'建立|模型|计算|分析|确定|求|评价|设计|预测|评估|估计|给出|研究',m[3]):
                        occurrences.append((int(m[1] or m[2]),page['page'],block))
        seen = set()
        for number,pi,block in occurrences:
            if number in seen:
                continue
            seen.add(number)
            remaining = '\n'.join(p['text'] for p in pages if p['page']>=pi)
            start = remaining.find(block['text'])
            goal = remaining[start:start+2200] if start>=0 else block['text']
            # Stop at the next explicitly numbered task rather than guessing dependencies.
            next_match = re.search(r'\n\s*(?:(?:问题|任务|问)\s*[一二三四五六七八九十1-9][：:、.\s]|[（(][1-9][）)]\s*|[1-9][．.、]\s*)',goal[len(block['text']):])
            if next_match:
                goal = goal[:len(block['text'])+next_match.start()]
            deliverables=[s.strip() for s in re.split('[。；\n]',goal) if re.search('计算|求出|给出|确定|预测|估计|评价|设计|建立',s)]
            constraints=[s.strip() for s in re.split('[。；\n]',goal) if re.search('要求|不超过|至少|至多|不能|必须|约束|假设|满足',s)]
            dependencies=[]
            for m in re.finditer(r'问题\s*([一二三四五六七八九1-9])\s*(?:的|中|所得|结果)',goal):
                previous={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}.get(m[1],int(m[1]) if m[1].isdigit() else 0)
                if previous<number:dependencies.append(pid+':q'+str(previous))
            subproblems.append({'subproblem_id':pid+':q'+str(number),'problem_id':pid,'ordinal':number,'goal':goal.strip(),
                                'problem_types':feature_types(goal),'constraints':constraints,'deliverables':deliverables,'dependency_refs':list(dict.fromkeys(dependencies)),
                                'semantic_fields_status':'pending_review','evidence':{'source_sha256':sha,'page':pi,'block_id':block['block_id'],'bbox':block['bbox']},'version':2})
    verified = core.CONTENT_DIR / 'normalized/stage04/cumcm_codex_v4'
    previous_papers = {core.load_json(p)['source_sha256']:core.load_json(p) for p in (verified/'cases').glob('*/profile.json')}
    for sha,(doc,pages) in documents.items():
        row = doc['source']
        if row['root_kind'] != 'paper':
            continue
        text = '\n'.join(p['text'] for p in pages)
        if len(pages)<3 or len(re.sub(r'\s','',text))<400:
            continue
        canonical_sha=source_by_id.get(row.get('parent_source_id'),{}).get('sha256',sha) if row.get('derivation')=='WPS-PDF-copy' else sha
        prior = previous_papers.get(sha) or previous_papers.get(canonical_sha)
        cid = prior['case_id'] if prior else 'cumcm-paper-' + canonical_sha
        headings,toc = pe.detect_structure([p['text'] for p in pages],sha)
        code = problem_code(row,text)
        candidate = 'cumcm-%d-%s' % (row['year'],code) if code else None
        related = prior.get('problem_id') if prior else candidate if candidate in problems else None
        relation_status = 'verified_prior_snapshot' if prior else 'candidate' if related else 'unresolved'
        title = next((l.strip() for l in pages[0]['text'].splitlines() if len(l.strip())>=6 and not re.search('摘要|编号|承诺',l)),Path(row['relative_path']).name)
        profile = {'case_id':cid,'year':row['year'],'competition_id':'cumcm','title':title,'source_sha256':sha,'source_id':row['source_id'],
                   'canonical_source_sha256':canonical_sha,'source_occurrences':[{'source_id':r['source_id'],'year':r['year'],'relative_path':r['relative_path']} for r in source_rows if r['sha256']==canonical_sha],
                   'problem_id':related,'relation_status':relation_status,'profile_version':4,'review_status':'machine_candidate',
                   'structure':headings,'recognized_length':pe.text_metrics([p['text'] for p in pages]),
                   'toc_pages':toc,
                   'page_count':len(pages),'ocr_pages':doc['ocr_pages'],'figure_captions':[b for p in pages for b in p['blocks'] if b['kind']=='figure_caption'],
                   'table_captions':[b for p in pages for b in p['blocks'] if b['kind']=='table_caption'],
                   'formula_candidates':[b for p in pages for b in p['blocks'] if b['kind']=='formula_candidate'],
                   'compliance_note':'仅作方法参照，禁止大段抄袭','award':None,'award_verification_status':'not_established',
                   'license':{'internal_analysis':'user_authorized_local_processing','distribute_original':False,'distribute_profile':False,'send_to_third_party_model':False}}
        method_terms=['层次分析','TOPSIS','主成分分析','神经网络','随机森林','支持向量机','线性规划','整数规划','动态规划','遗传算法','粒子群','模拟退火','蒙特卡洛','微分方程','时间序列','ARIMA','灰色预测','马尔可夫','最小二乘','回归分析','聚类','有限差分','Runge-Kutta']
        profile['method_tags']=[term for term in method_terms if term.lower() in text.lower()]
        profile['method_tags_status']='literal_mentions_candidate; may include methods only discussed or cited'
        profile['method_evidence']=[{'method':term,'source_sha256':sha,'pages':[p['page'] for p in pages if term.lower() in p['text'].lower()]} for term in profile['method_tags']]
        papers.append(profile)
        if len(papers)%25==0:print('catalog paper profiles {}'.format(len(papers)),flush=True)
        links.append({'case_id':cid,'problem_id':related,'status':relation_status,'evidence':{'source_sha256':sha,'filename':row['relative_path'],'title_page':1},
                      'not_automatic_confirmation':not bool(prior)})
    # Attach all raw/expanded datasets to edition and candidate problem without copying per topic.
    for row in source_rows:
        code = problem_code(row)
        pid = 'cumcm-%d-%s' % (row['year'],code) if code else None
        if row['root_kind']=='problem' and pid in problems and row['extension'] not in ('.pdf','.doc','.docx'):
            problems[pid]['attachments'].append({'source_id':row['source_id'],'sha256':row['sha256'],'relation_status':'path_candidate'})
    paper_by_sha = {p['source_sha256']:p for p in papers}
    for sha,(doc,pages) in documents.items():
        row = doc['source']
        case = paper_by_sha.get(sha)
        pid = case.get('problem_id') if case else 'cumcm-%d-%s' % (row['year'],problem_code(row)) if problem_code(row) else None
        if pid not in problems:
            pid = None
        headings = case['structure'] if case else []
        for page in pages:
            stage = 'analysis'
            available = [h for h in headings if h['page_range'][0]<page['page']]
            if available:
                kind = available[-1]['section_type']
                stage = {'solve':'algorithm','model':'model','validation':'validation','sensitivity':'sensitivity','discussion':'discussion','conclusion':'discussion','abstract':'abstract','restatement':'analysis','analysis':'analysis','assumptions':'assumptions','symbols':'symbols','references':'references','appendix':'appendix'}.get(kind,'analysis')
            blocks = page['blocks']
            for offset,group in paragraph_groups(blocks):
                text = '\n'.join(b['text'] for b in group)
                current_headings=[h for h in headings if h['page_range'][0]==page['page'] and any(h['page_evidence'].strip()==b['text'].strip() for b in blocks[:offset+len(group)])]
                if current_headings:
                    available=current_headings
                if available:
                    kind=available[-1]['section_type']
                    stage={'solve':'algorithm','model':'model','validation':'validation','sensitivity':'sensitivity','discussion':'discussion','conclusion':'discussion','abstract':'abstract','restatement':'analysis','analysis':'analysis','assumptions':'assumptions','symbols':'symbols','references':'references','appendix':'appendix','toc':'toc'}.get(kind,'analysis')
                    if re.search('结果|实验|计算输出|results',available[-1]['section_title'],re.I):stage='results'
                if case and page['page'] in case['toc_pages']:stage='toc'
                # Fixed small chunks keep formula/caption provenance adjacent, never include source code as instructions.
                for ci in (0,):
                    # Keep all lines of a paragraph/nearby formula group intact.
                    chunk = text
                    if len(chunk.strip())<12:
                        continue
                    uid = 'unit-' + core.sha256_bytes(sha+':%d:%d:%d'%(page['page'],offset,ci))
                    mentions=text+'\n'+(available[-1]['section_title'] if available else '')
                    refs=[]
                    for match in re.finditer(r'(?:问题|任务|第)\s*([一二三四五六七八九1-9])\s*(?:问|的|中|求解|模型|[：:、\s]|$)',mentions):
                        number={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}.get(match[1],int(match[1]) if match[1].isdigit() else 0)
                        ref=str(pid)+':q'+str(number)
                        if any(s['subproblem_id']==ref for s in subproblems):refs.append(ref)
                    refs=list(dict.fromkeys(refs))
                    for ref in refs:mappings.append({'unit_id':uid,'subproblem_id':ref,'block_ids':[b['block_id'] for b in group],'status':'explicit_mention_candidate','evidence':{'source_sha256':sha,'page':page['page']},'not_automatic_confirmation':True})
                    units.append({'unit_id':uid,'competition_id':'cumcm','year':row['year'],'problem_id':pid,'case_id':case['case_id'] if case else None,
                                  'kind':'historical_paper' if row['root_kind']=='paper' else 'historical_problem','stage':stage,'text':chunk,'problem_types':feature_types(chunk),
                                  'source_sha256':sha,'page':page['page'],'block_ids':[b['block_id'] for b in group],
                                  'context_before':'\n'.join(b['text'] for b in blocks[max(0,offset-2):offset]),
                                  'context_after':'\n'.join(b['text'] for b in blocks[offset+len(group):offset+len(group)+2]),
                                  'parent_section':available[-1] if available else None,
                                  'subproblem_refs':refs,'grouping_basis':'whole lines grouped by heading and sentence boundaries; contextual neighbors preserved; derivation semantics pending review',
                                  'review_status':'machine_candidate','external_consumer_allowed':False,'source_text_is_instruction':False,'version':2})
    # Approved source goal tags can be carried forward without claiming full new corpus review.
    for path in (verified/'problems').glob('*/profile.json'):
        old = core.load_json(path)
        if old['problem_id'] in problems:
            problems[old['problem_id']]['prior_verified_profile'] = old
    entities = {'problems':list(problems.values()),'subproblems':subproblems,'papers':papers,'paper_problem_links':links}
    subids={s['subproblem_id'] for s in subproblems}
    for sub in subproblems:
        unresolved=[d for d in sub['dependency_refs'] if d not in subids]
        sub['unresolved_dependency_mentions']=unresolved
        sub['dependency_refs']=[d for d in sub['dependency_refs'] if d in subids]
    for name,rows in entities.items():
        core.write_json(rd/'catalog'/('%s.json'%name),rows)
    core.write_jsonl(rd/'catalog/retrieval_units.jsonl',units)
    core.write_jsonl(rd/'catalog/block_subproblem_links.jsonl',mappings)
    counts = {'problem_entities':len(problems),'subproblem_candidates':len(subproblems),'paper_candidates':len(papers),'retrieval_units':len(units),
              'confirmed_prior_links':sum(l['status']=='verified_prior_snapshot' for l in links),'pending_links':sum(l['status']!='verified_prior_snapshot' for l in links),
              'year_coverage':[{ 'year':y,'problems':sum(p['year']==y for p in problems.values()),'papers':sum(p['year']==y for p in papers)} for y in range(2010,2026)],
              'parse_report':core.load_json(rd/'quality/parse_report.json') if (rd/'quality/parse_report.json').exists() else None,
              'quality_status':'machine_candidates; independent content QA required'}
    core.write_json(rd/'quality/catalog_report.json',counts)
    from . import recipes
    recipes.main(args)
    print(json.dumps({k:counts[k] for k in ('problem_entities','subproblem_candidates','paper_candidates','retrieval_units')},ensure_ascii=False))
    return 0

def release_dir(args):
    return core.safe_relpath(core.CONTENT_DIR,'out/cumcm_delivery/releases/'+args.release_id)

def copy_verified(src,dest,sha):
    src=core.no_links(src);dest=core.output_path(dest)
    if core.sha256_of(src)!=sha:
        raise ValueError('copy source hash mismatch')
    if dest.exists():
        if core.sha256_of(dest)!=sha:
            raise ValueError('release content conflict')
        return
    dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(str(src),str(dest))
    if core.sha256_of(dest)!=sha:
        raise ValueError('copy readback mismatch')

def unit_rows(root):
    units=core.load_jsonl(root/'catalog/retrieval_units.jsonl')
    for name in ('writing_recipes','figure_recipes'):
        for recipe in core.load_json(root/'recipes'/(name+'.json')):
            units.append({'unit_id':recipe['recipe_id'],'competition_id':'cumcm','year':None,'kind':'writing_recipe' if name.startswith('writing') else 'figure_recipe',
                          'stage':recipe['stage'],'text':json.dumps(recipe,ensure_ascii=False),'problem_types':recipe['problem_types'],'source_sha256':None,
                          'external_consumer_allowed':True,'recipe':recipe,'review_status':'self_authored_reviewed'})
    return units

def prepare_embeddings(args):
    from . import services
    import numpy as np
    rd=run_dir(args);units=unit_rows(rd);values=[];index=[]
    for offset in range(0,len(units),64):
        batch=units[offset:offset+64];vectors=services.embed_cached([u['text'] for u in batch])
        for unit,vector in zip(batch,vectors):
            data=np.asarray(vector,dtype='<f4').tobytes();values.append(data)
            index.append({'unit_id':unit['unit_id'],'text_sha256':core.sha256_bytes(unit['text']),'vector_sha256':core.sha256_bytes(data)})
        if offset%1024==0:print('sealed vector preparation {}/{}'.format(min(offset+64,len(units)),len(units)),flush=True)
    core.write_bytes(rd/'vectors/vectors.f32',b''.join(values))
    core.write_jsonl(rd/'vectors/index.jsonl',index)
    core.write_json(rd/'vectors/profile.json',{'model_manifest':services.model_manifest(),'dtype':'little-endian float32','dimension':384,'rows':len(units),'vectors_sha256':core.sha256_of(rd/'vectors/vectors.f32'),'index_sha256':core.sha256_of(rd/'vectors/index.jsonl'),'generated_by':'actual ONNX inference, cached by text SHA and full model pin'})
    return 0

def validate_release(path):
    path=core.no_links(path)
    seal=core.load_json(path/'SEALED.json')
    manifest_path=path/'manifest.json'
    if core.sha256_of(manifest_path)!=seal['manifest_sha256']:
        raise ValueError('sealed manifest checksum mismatch')
    manifest=core.load_json(manifest_path)
    from . import contracts
    contracts.validate('seal',seal)
    contracts.validate('manifest',manifest)
    if manifest['release_id']!=path.name or manifest['competition_id']!='cumcm':
        raise ValueError('release identity mismatch')
    known={'manifest.json','SEALED.json'}
    for entry in manifest['files']:
        if entry['path'] in known:
            raise ValueError('duplicate manifest path')
        known.add(entry['path'])
    # Preserve all path/reparse/byte checks, while overlapping independent file
    # reads. A large release has tens of thousands of small evidence objects.
    def check_entry(entry):
        target=core.safe_relpath(path,entry['path'])
        if not target.is_file() or target.stat().st_size!=entry['size'] or core.sha256_of(target)!=entry['sha256']:
            raise ValueError('release entry corrupt: '+entry['path'])
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as pool:
        for count,_ in enumerate(pool.map(check_entry,manifest['files']),1):
            if len(manifest['files'])>1000 and count%10000==0:
                print('sealed files verified {}/{}'.format(count,len(manifest['files'])),file=sys.stderr,flush=True)
    actual=set()
    file_paths=[]
    for directory,dirs,files in os.walk(str(path)):
        core.no_links(directory)
        for d in dirs:
            core.no_links(Path(directory)/d)
        for file in files:
            file_paths.append(Path(directory)/file)
    with ThreadPoolExecutor(max_workers=6) as pool:
        for p in pool.map(core.no_links,file_paths):
            actual.add(p.relative_to(path).as_posix())
    if known!=actual:
        raise ValueError('untracked/missing release paths')
    return manifest

def pack(args):
    from . import services
    rd=run_dir(args);dest=release_dir(args)
    if (dest/'SEALED.json').exists():
        validate_release(dest)
        print('existing immutable release verified')
        return 0
    report=core.load_json(rd/'quality/parse_report.json')
    if not report['complete'] or report['failures'] or report['processed']!=report['input_pdf_count']:
        raise ValueError('full parsing has not completed successfully')
    if (rd/'quality/local_usage_policy.json').exists():
        components=core.load_json(rd/'quality/formula_enrichment_progress.json')
        if not components['complete'] or components['counts']['pages']!=sum(d['pages'] for d in report['documents']):
            raise ValueError('local full source/region evidence has not completed')
        business_qa=core.load_json(rd/'quality/full_business_contract_validation.json')
        if not business_qa['formulas_included'] or business_qa['status']!='pass':raise ValueError('all local business contracts must be validated before seal')
    current_units=unit_rows(rd)
    if not (rd/'vectors/profile.json').exists():prepare_embeddings(args)
    profile=core.load_json(rd/'vectors/profile.json');index=core.load_jsonl(rd/'vectors/index.jsonl')
    services.verify_model_pin(profile['model_manifest'])
    if len(index)!=len(current_units) or any(u['unit_id']!=i['unit_id'] or core.sha256_bytes(u['text'])!=i['text_sha256'] for u,i in zip(current_units,index)):
        raise ValueError('vectors do not match current catalog; run prepare_embeddings before packing')
    rows=all_sources(args)+core.load_jsonl(rd/'converted_sources.jsonl')
    assets={};asset_jobs=[]
    def add_asset(path,sha,metadata):
        if sha in assets:
            assets[sha]['origins'].append(metadata)
            return
        rel='objects/'+sha[:2]+'/'+sha
        asset_jobs.append((path,dest/rel,sha))
        assets[sha]={'asset_id':'asset-'+sha,'version':1,'sha256':sha,'size':Path(path).stat().st_size,'object_key':services.object_key(sha),
                     'release_path':rel,'origins':[metadata],'external_consumer_allowed':metadata.get('external_consumer_allowed',False)}
    for row in rows:
        add_asset(source_path(row,args),row['sha256'],{'source_id':row['source_id'],'year':row['year'],'role':row['root_kind'],'relative_path':row['relative_path'],
                                                     'media_type':row.get('media_type'),'external_consumer_allowed':False})
    # A resumed parser can leave obsolete crop versions. Publish only evidence
    # referenced by current page blocks, and verify the declared source bytes.
    crop_paths = {}
    for page_path in sorted((rd/'parsed').glob('*/page-*.json')):
        page = core.load_json(page_path)
        if page.get('full_page_evidence'):
            evidence=page['full_page_evidence']
            crop_paths[evidence['path']]=(evidence['asset_ref']['sha256'],{'role':'complete_page_evidence',
                'source_sha256':page['input_sha256'],'page':page['page'],'external_consumer_allowed':False})
        for block in page['blocks']:
            if 'crop_path' in block:
                crop_paths[block['crop_path']] = (block['crop_sha256'],{'role':'source_crop',
                    'source_sha256':page['input_sha256'],'page':page['page'],'external_consumer_allowed':False})
    from concurrent.futures import ThreadPoolExecutor
    def checked_evidence(item):
        relative,sha,origin=item;p=core.safe_relpath(core.CONTENT_DIR,relative)
        if core.sha256_of(p)!=sha:raise ValueError('referenced evidence checksum mismatch: '+relative)
        return p,sha,origin
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs=((relative,sha,origin) for relative,(sha,origin) in sorted(crop_paths.items()))
        for count,(p,sha,origin) in enumerate(pool.map(checked_evidence,jobs),1):
            add_asset(p,sha,origin)
            if count%5000==0:print('referenced page/crop evidence SHA verified {}/{}'.format(count,len(crop_paths)),flush=True)
    formula_catalog=rd/'catalog/formula_regions.jsonl'
    if formula_catalog.exists():
        formula_jobs=[(f['crop_path'],f['crop_ref']['sha256'],{'role':'formula_region','source_sha256':f['source_sha256'],
            'page':f['page'],'external_consumer_allowed':False}) for f in core.load_jsonl(formula_catalog) if f['crop_ref']]
        with ThreadPoolExecutor(max_workers=6) as pool:
            for count,(p,sha,origin) in enumerate(pool.map(checked_evidence,formula_jobs),1):
                add_asset(p,sha,origin)
                if count%2000==0:print('formula crop evidence SHA verified {}/{}'.format(count,len(formula_jobs)),flush=True)
    for review_path in sorted((rd/'quality/local_model_reviews').glob('*.json')):
        review=core.load_json(review_path);p=core.safe_relpath(core.CONTENT_DIR,review['input_path'])
        if core.sha256_of(p)!=review['input_sha256']:raise ValueError('local review evidence checksum mismatch')
        add_asset(p,review['input_sha256'],{'role':'quality_review_input','source_sha256':review['source_sha256'],'page':review['page'],'external_consumer_allowed':False})
    for p in sorted((rd/'quality/complex_formats').rglob('*')):
        if p.is_file() and p.suffix.lower() in ('.pdf','.png'):
            add_asset(p,core.sha256_of(p),{'role':'format_qa_evidence','relative_path':p.relative_to(rd).as_posix(),'external_consumer_allowed':False})
    package_manifest=core.load_json(rd/'templates/dependency_manifest.json')
    for package in package_manifest['packages']:
        p=core.safe_relpath(rd/'templates',package['file'])
        if core.sha256_of(p)!=package['sha256']:raise ValueError('format package pin drift')
        add_asset(p,package['sha256'],{'role':'format_template','format':package['kind'],'format_version':package.get('version',2),'external_consumer_allowed':True})
    for p in sorted((rd/'templates/evidence').glob('*.pdf')):
        add_asset(p,core.sha256_of(p),{'role':'format_rule_evidence','external_consumer_allowed':False})
    from concurrent.futures import ThreadPoolExecutor
    def copy_asset(job):copy_verified(*job)
    with ThreadPoolExecutor(max_workers=6) as pool:
        for count,_ in enumerate(pool.map(copy_asset,asset_jobs),1):
            if count%2000==0:print('release objects copied and SHA verified {}/{}'.format(count,len(asset_jobs)),flush=True)
    for folder in ('catalog','recipes','templates','quality','sources','parsed'):
        for p in sorted((rd/folder).rglob('*')):
            if p.is_file() and p.suffix.lower() in ('.json','.jsonl'):
                copy_verified(p,dest/'metadata'/p.relative_to(rd),core.sha256_of(p))
    for name in ('derived_sources.jsonl','converted_sources.jsonl'):
        p=rd/name
        copy_verified(p,dest/'metadata/sources'/name,core.sha256_of(p))
    for p in sorted((rd/'vectors').glob('*')):
        copy_verified(p,dest/'embeddings'/p.name,core.sha256_of(p))
    core.write_json(dest/'assets.json',sorted(assets.values(),key=lambda a:a['asset_id']))
    core.write_json(dest/'model_manifest.json',services.model_manifest())
    metadata={'competition_id':'cumcm','release_id':args.release_id,'schema_version':3,'historical_year_range':[2010,2025],
              'source_run_id':args.run_id,'quality':'machine_candidates; historical consumers gated pending QA and rights',
              'self_contained':True,'cloud_deployed':False,'qa_fixture_only':getattr(args,'qa_fixture_only',False),'parsing':report,'catalog':core.load_json(rd/'quality/catalog_report.json'),
              'embedding_dimension':384,'embedding_manifest':services.model_manifest(),'format_profiles':{name:core.load_json(rd/'templates'/(name+'.json')) for name in ('ruleset','font_manifest','dynamic_section_contract')},'format_target_edition_required':True,'created_at':core.now_utc_iso()}
    policy=rd/'quality/local_usage_policy.json'
    if policy.exists():
        metadata['local_usage_policy']=core.load_json(policy)
        metadata['structured_store_required']=True;metadata['component_counts']=business_qa['counts']
    core.write_json(dest/'metadata.json',metadata)
    file_paths=[p for p in sorted(dest.rglob('*')) if p.is_file() and p.name not in ('manifest.json','SEALED.json')]
    def manifest_entry(p):return {'path':p.relative_to(dest).as_posix(),'sha256':core.sha256_of(p),'size':p.stat().st_size}
    with ThreadPoolExecutor(max_workers=6) as pool:entries=list(pool.map(manifest_entry,file_paths))
    core.write_json(dest/'manifest.json',{'competition_id':'cumcm','release_id':args.release_id,'version':3,'files':entries})
    core.write_json(dest/'SEALED.json',{'manifest_sha256':core.sha256_of(dest/'manifest.json'),'sealed_at':core.now_utc_iso()})
    validate_release(dest)
    print('self-contained immutable release: files={} assets={}'.format(len(entries),len(assets)))
    return 0

def import_release(args):
    from . import services
    from concurrent.futures import ThreadPoolExecutor
    import asyncio
    if getattr(args,'target','local-audit')!='local-audit':raise ValueError('only configured local-audit import target is available')
    dest=release_dir(args);manifest=validate_release(dest)
    if core.load_json(dest/'metadata.json').get('structured_store_required') and getattr(args,'activate',False):
        raise ValueError('structured release must be staged, typed-imported and verified before explicit activation')
    async def existing_release():
        connection=await services.pg()
        try:
            exists=await connection.fetchval("SELECT to_regclass('content_de_codex.releases') IS NOT NULL")
            if not exists:return None
            return await connection.fetchval('SELECT manifest_sha256 FROM content_de_codex.releases WHERE release_id=$1',args.release_id)
        finally:await connection.close()
    existing=asyncio.run(existing_release())
    if existing:
        if existing!=core.sha256_of(dest/'manifest.json'):raise ValueError('release ID conflict')
        verified=read_only_check(args)
        core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_idempotency.json',dict(verified,status='same_release_content_verified_no_rewrite',activation_requested=bool(getattr(args,'activate',False))))
        print('existing database release verified without rewriting')
        if getattr(args,'activate',False):
            activate(args,'activate')
        return 0
    assets=core.load_json(dest/'assets.json')
    services.s3()  # initialize before worker threads
    def upload(a):
        key=services.put_verified(core.safe_relpath(dest,a['release_path']),a['sha256'])
        if key!=a['object_key']:
            raise ValueError('object key mismatch')
        return a['asset_id']
    with ThreadPoolExecutor(max_workers=6) as pool:
        for count,_ in enumerate(pool.map(upload,assets),1):
            if count%100==0:
                print('S3 verified {}/{}'.format(count,len(assets)),flush=True)
    units=unit_rows(dest/'metadata')
    import numpy as np
    vprofile=core.load_json(dest/'embeddings/profile.json');vindex=core.load_jsonl(dest/'embeddings/index.jsonl')
    services.verify_model_pin(vprofile['model_manifest'])
    if core.sha256_of(dest/'embeddings/vectors.f32')!=vprofile['vectors_sha256'] or core.sha256_of(dest/'embeddings/index.jsonl')!=vprofile['index_sha256']:raise ValueError('pinned vector artifact drift')
    all_vectors=np.frombuffer((dest/'embeddings/vectors.f32').read_bytes(),dtype='<f4').reshape(len(units),384)
    if len(vindex)!=len(units):raise ValueError('vector row count mismatch')
    for unit,entry,vector in zip(units,vindex,all_vectors):
        if entry['unit_id']!=unit['unit_id'] or entry['text_sha256']!=core.sha256_bytes(unit['text']) or entry['vector_sha256']!=core.sha256_bytes(vector.tobytes()):raise ValueError('unit/vector pin mismatch')
    async def load():
        conn=await services.pg()
        try:
            await conn.execute((core.CONTENT_DIR/'sql/cumcm/0002_codex_local.sql').read_text(encoding='utf-8'))
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(621032025)')
                msha=core.sha256_of(dest/'manifest.json')
                existing=await conn.fetchval('SELECT manifest_sha256 FROM content_de_codex.releases WHERE release_id=$1',args.release_id)
                if existing and existing!=msha:
                    raise ValueError('database release identity conflict')
                if existing:
                    total=await conn.fetchval('SELECT count(*) FROM content_de_codex.units WHERE release_id=$1',args.release_id)
                    if total!=len(units):
                        raise ValueError('existing release has incomplete units')
                    return {'status':'idempotent_verified','units':total,'assets':len(assets),'release_id':args.release_id}
                await conn.execute('INSERT INTO content_de_codex.releases VALUES($1,$2,$3,$4::jsonb,now())',args.release_id,msha,'staging',json.dumps(core.load_json(dest/'metadata.json'),ensure_ascii=False))
                await conn.executemany('INSERT INTO content_de_codex.assets VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)',[(args.release_id,a['asset_id'],a['version'],a['sha256'],a['size'],a['object_key'],json.dumps(a,ensure_ascii=False)) for a in assets])
                await conn.executemany("INSERT INTO content_de_codex.editions VALUES($1,$2,'cumcm')",[(args.release_id,y) for y in range(2010,2026)])
                for kind,idfield in [('problems','problem_id'),('subproblems','subproblem_id'),('papers','case_id'),('paper_problem_links','case_id')]:
                    rows=core.load_json(dest/'metadata/catalog'/(kind+'.json'))
                    if kind=='problems':
                        await conn.executemany('INSERT INTO content_de_codex.problems VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)',[(args.release_id,r['problem_id'],r['year'],r['problem_code'],r['title'],r['version'],r['problem_types'],json.dumps(r,ensure_ascii=False)) for r in rows])
                    elif kind=='subproblems':
                        await conn.executemany('INSERT INTO content_de_codex.subproblems VALUES($1,$2,$3,$4,$5,$6::jsonb)',[(args.release_id,r['subproblem_id'],r['problem_id'],r['ordinal'],r['goal'],json.dumps(r,ensure_ascii=False)) for r in rows])
                    elif kind=='papers':
                        await conn.executemany('INSERT INTO content_de_codex.papers VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)',[(args.release_id,r['case_id'],r['year'],r['problem_id'],r['source_sha256'],r['title'],r['relation_status'],r['profile_version'],json.dumps(r,ensure_ascii=False)) for r in rows])
                    else:
                        await conn.executemany('INSERT INTO content_de_codex.paper_problem_links VALUES($1,$2,$3,$4,$5::jsonb)',[(args.release_id,r['case_id'],r['problem_id'],r['status'],json.dumps(r['evidence'],ensure_ascii=False)) for r in rows])
                    values=[]
                    for row in rows:
                        payload=json.dumps(row,ensure_ascii=False,sort_keys=True,allow_nan=False)
                        values.append((args.release_id,kind,row[idfield],payload,core.sha256_bytes(payload)))
                    await conn.executemany('INSERT INTO content_de_codex.entities VALUES($1,$2,$3,$4::jsonb,$5)',values)
                attachments=core.load_jsonl(dest/'metadata/catalog/attachment_profiles.jsonl')
                await conn.executemany('INSERT INTO content_de_codex.entities VALUES($1,$2,$3,$4::jsonb,$5)',[(args.release_id,'attachments',r['source_id'],json.dumps(r,ensure_ascii=False,sort_keys=True),core.sha256_bytes(json.dumps(r,ensure_ascii=False,sort_keys=True))) for r in attachments])
                for offset in range(0,len(units),64):
                    batch=units[offset:offset+64]
                    vectors=all_vectors[offset:offset+64]
                    values=[]
                    for unit,vector in zip(batch,vectors):
                        vectorstr='['+','.join(format(v,'.9g') for v in vector)+']'
                        values.append((args.release_id,unit['unit_id'],'cumcm',unit.get('year'),unit['kind'],unit['stage'],unit.get('problem_id'),unit.get('case_id'),unit['text'],unit.get('source_sha256'),unit['external_consumer_allowed'],vectorstr,services.configuration()['embedding']['model'],json.dumps(unit,ensure_ascii=False)))
                    await conn.executemany('INSERT INTO content_de_codex.units VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::vector,$13,$14::jsonb)',values)
                    if offset%1024==0:
                        print('embedding+PG {}/{}'.format(min(offset+64,len(units)),len(units)),flush=True)
                # Explicit activation is atomic with the fully populated import; historical flags stay false.
                if getattr(args,'activate',False):
                    previous=await conn.fetchval("SELECT release_id FROM content_de_codex.activation WHERE competition_id='cumcm'")
                    await conn.execute("UPDATE content_de_codex.releases SET status='retired' WHERE release_id=$1",previous)
                    await conn.execute("INSERT INTO content_de_codex.activation VALUES('cumcm',$1,now()) ON CONFLICT(competition_id) DO UPDATE SET release_id=excluded.release_id,updated_at=now()",args.release_id)
                    await conn.execute("UPDATE content_de_codex.releases SET status='active' WHERE release_id=$1",args.release_id)
                    await conn.execute("INSERT INTO content_de_codex.release_events(competition_id,previous_release,next_release,event_kind) VALUES('cumcm',$1,$2,'local_audit_activation')",previous,args.release_id)
            return {'status':'applied_local','units':len(units),'assets':len(assets),'release_id':args.release_id,'postgres_schema':'content_de_codex','historical_consumer_enabled':False}
        finally:
            await conn.close()
    result=asyncio.run(load())
    if result['status']=='idempotent_verified' and getattr(args,'activate',False):
        activate(args,'activate')
    core.write_json(core.CONTENT_DIR/'reports/trae'/('cumcm_codex_import_'+args.release_id+'.json'),result)
    if not core.load_json(dest/'metadata.json').get('qa_fixture_only'):
        core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_import.json',result)
    print(json.dumps(result))
    return 0

def dispatch(args):
    import asyncio
    if getattr(args,'target','local-audit')!='local-audit':raise ValueError('only configured local-audit target is available; cloud adapter is not configured')
    from . import consumer
    cmd=args.cmd
    if getattr(args,'target','local-audit')!='local-audit':
        raise ValueError('cloud target is not configured; no silent local fallback')
    mapping={'expand':expand,'relate':catalog,'templates':templates,'pack':pack,'evaluate':evaluate}
    if cmd in mapping:
        return mapping[cmd](args)
    if cmd=='parse':
        args.ocr=True
        return parse(args)
    if cmd=='recipes':
        from . import recipes
        return recipes.main(args)
    if cmd=='import':
        if args.dry_run:
            manifest=validate_release(release_dir(args))
            print(json.dumps({'status':'dry_run_validated','release_id':args.release_id,'files':len(manifest['files'])}))
            return 0
        if not args.apply:
            raise ValueError('import requires --dry-run or --apply')
        return import_release(args)
    if cmd=='retrieve':
        if not args.request:
            result=consumer.bootstrap(args.release_id)
        else:
            req=core.load_json(args.request)
            if req['release_id']!=args.release_id:
                raise ValueError('request/CLI release mismatch')
            result=consumer.retrieve(req,'internal_audit' if args.internal_audit else 'consumer')
        if args.output:
            core.write_json(core.CONTENT_DIR/args.output,result)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    if cmd=='fetch':
        result=asyncio.run(consumer.fetch_async(args.release_id,args.asset_id,args.version,args.output,'internal_audit' if args.internal_audit else 'consumer'))
        result.pop('bytes')
        print(json.dumps(result,ensure_ascii=False))
        return 0
    if cmd=='check':
        result=read_only_check(args)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    if cmd in ('activate','rollback'):
        return activate(args,cmd)
    if cmd=='preflight':
        from . import services
        services.smoke()
        core.write_json(run_dir(args)/'quality/preflight.json',{'python':core.PY,'actual_version':sys.version,'scope':core.load_scope(args.scope_config),
            'protected_baseline':'D:/Erdos/reports/trae/cumcm_codex_before/protected_baseline.json','cloud_configured':False,
            'local_services_config':'.runtime/local/services.json','secrets_in_report':False,'original_deliveries_protected':True})
        return 0
    if cmd in ('build','update'):
        if cmd=='update':
            if not args.previous or args.previous==args.release_id:
                raise ValueError('update requires distinct --previous release')
            validate_release(core.safe_relpath(core.CONTENT_DIR,'out/cumcm_delivery/releases/'+args.previous))
            from . import incremental,inventory
            inventory.main(args)
            incremental.record(args)
        # Existing sealed releases cannot be rebuilt under the same version.
        if (release_dir(args)/'SEALED.json').exists():
            validate_release(release_dir(args));return import_release(args)
        args.ocr=True;args.max_documents=0
        from . import inventory,attachments,patterns,review_queue,qa
        def quality_inputs(current):
            attachments.main(current)
            patterns.main(run_dir(current))
            review_queue.main(run_dir(current))
            qa.structure(run_dir(current))
            return 0
        for step in (inventory.main,expand,parse,normalize_sources,catalog,quality_inputs,templates,prepare_embeddings,pack,import_release,evaluate):
            result=step(args)
            if result:
                raise ValueError('pipeline stopped at '+step.__name__)
        return 0
    raise ValueError('unknown audited command')

def validate_database_inputs(path):
    """Activation depends on pinned metadata and actual PG, not local copies of S3 bytes.

    Full package byte/untracked-path validation stays in validate_release and QC.
    Every file consumed below by database verification must match the sealed index.
    """
    from . import contracts
    path=core.no_links(path);seal=core.load_json(path/'SEALED.json')
    if core.sha256_of(path/'manifest.json')!=seal['manifest_sha256']:raise ValueError('sealed manifest checksum mismatch')
    manifest=core.load_json(path/'manifest.json');contracts.validate('seal',seal);contracts.validate('manifest',manifest)
    if manifest['release_id']!=path.name or manifest['competition_id']!='cumcm':raise ValueError('release identity mismatch')
    entries={}
    for entry in manifest['files']:
        if entry['path'] in entries or entry['path'] in ('manifest.json','SEALED.json'):raise ValueError('duplicate manifest path')
        entries[entry['path']]=entry
    required=['assets.json','metadata.json','model_manifest.json','embeddings/profile.json','embeddings/index.jsonl','embeddings/vectors.f32',
              'metadata/catalog/retrieval_units.jsonl','metadata/catalog/attachment_profiles.jsonl']
    required += ['metadata/catalog/'+name+'.json' for name in ('problems','subproblems','papers','paper_problem_links')]
    required += ['metadata/recipes/'+name+'.json' for name in ('writing_recipes','figure_recipes')]
    for relative in required:
        entry=entries.get(relative);file=core.safe_relpath(path,relative)
        if not entry or not file.is_file() or file.stat().st_size!=entry['size'] or core.sha256_of(file)!=entry['sha256']:
            raise ValueError('pinned database input corrupt: '+relative)
    return manifest


def read_only_check(args, _activation_database_inputs=False):
    import asyncio
    from . import services
    root=release_dir(args)
    manifest=validate_database_inputs(root) if _activation_database_inputs else validate_release(root)
    assets=core.load_json(root/'assets.json')
    units=core.load_jsonl(root/'metadata/catalog/retrieval_units.jsonl')
    async def check():
        connection=await services.pg()
        try:
            async with connection.transaction(readonly=True):
                release=await connection.fetchrow('SELECT manifest_sha256,metadata FROM content_de_codex.releases WHERE release_id=$1',args.release_id)
                if not release or release['manifest_sha256']!=core.sha256_of(root/'manifest.json'):
                    raise ValueError('database manifest pin mismatch')
                if core.json_loads(release['metadata'])!=core.load_json(root/'metadata.json'):raise ValueError('database release metadata drift')
                rows=await connection.fetch('SELECT asset_id,version,sha256,size,object_key,payload FROM content_de_codex.assets WHERE release_id=$1',args.release_id)
                if len(rows)!=len(assets):
                    raise ValueError('database asset coverage mismatch')
                expected={a['asset_id']:a for a in assets}
                for row in rows:
                    a=expected[row['asset_id']]
                    payload=core.json_loads(row['payload'])
                    if payload!=a or (row['version'],row['sha256'],row['size'],row['object_key'])!=(a['version'],a['sha256'],a['size'],a['object_key']):
                        raise ValueError('database asset roundtrip mismatch')
                total=await connection.fetchval('SELECT count(*) FROM content_de_codex.units WHERE release_id=$1',args.release_id)
                recipes=sum(len(core.load_json(root/'metadata/recipes'/(n+'.json'))) for n in ('writing_recipes','figure_recipes'))
                if total!=len(units)+recipes:
                    raise ValueError('database unit coverage mismatch')
                expected_units={u['unit_id']:u for u in units}
                for name in ('writing_recipes','figure_recipes'):
                    for recipe in core.load_json(root/'metadata/recipes'/(name+'.json')):
                        expected_units[recipe['recipe_id']]={'unit_id':recipe['recipe_id'],'competition_id':'cumcm','year':None,'kind':'writing_recipe' if name.startswith('writing') else 'figure_recipe','stage':recipe['stage'],'text':json.dumps(recipe,ensure_ascii=False),'problem_types':recipe['problem_types'],'source_sha256':None,'external_consumer_allowed':True,'recipe':recipe,'review_status':'self_authored_reviewed'}
                records=await connection.fetch('SELECT unit_id,text_content,payload,embedding_model,embedding::text AS vector_text,stage,year,external_consumer_allowed,competition_id,kind,problem_id,case_id,source_sha256 FROM content_de_codex.units WHERE release_id=$1',args.release_id)
                vector_index={r['unit_id']:r for r in core.load_jsonl(root/'embeddings/index.jsonl')}
                import numpy as np
                for row in records:
                    expected_unit=expected_units[row['unit_id']]
                    if core.json_loads(row['payload'])!=expected_unit or row['text_content']!=expected_unit['text'] or row['embedding_model']!=services.configuration()['embedding']['model']:
                        raise ValueError('database retrieval payload/model drift')
                    if (row['stage'],row['year'],row['external_consumer_allowed'])!=(expected_unit['stage'],expected_unit['year'],expected_unit['external_consumer_allowed']):raise ValueError('typed database scope/purpose drift')
                    if (row['competition_id'],row['kind'],row['problem_id'],row['case_id'],row['source_sha256'])!=('cumcm',expected_unit['kind'],expected_unit.get('problem_id'),expected_unit.get('case_id'),expected_unit.get('source_sha256')):raise ValueError('typed database unit relation drift')
                    if core.sha256_bytes(np.asarray(core.json_loads(row['vector_text']),dtype='<f4').tobytes())!=vector_index[row['unit_id']]['vector_sha256']:raise ValueError('database vector roundtrip drift')
                services.verify_model_pin(core.load_json(root/'model_manifest.json'))
                entities=0
                for kind,idfield in [('problems','problem_id'),('subproblems','subproblem_id'),('papers','case_id'),('paper_problem_links','case_id')]:
                    values=core.load_json(root/'metadata/catalog'/(kind+'.json'))
                    rows=await connection.fetch('SELECT entity_id,payload,payload_sha256 FROM content_de_codex.entities WHERE release_id=$1 AND kind=$2',args.release_id,kind)
                    expected={v[idfield]:v for v in values}
                    if len(rows)!=len(expected):
                        raise ValueError('entity count mismatch '+kind)
                    for row in rows:
                        value=core.json_loads(row['payload'])
                        if expected[row['entity_id']]!=value or core.sha256_bytes(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False))!=row['payload_sha256']:
                            raise ValueError('entity payload drift')
                    typed=await connection.fetch('SELECT * FROM content_de_codex.'+kind+' WHERE release_id=$1',args.release_id)
                    if len(typed)!=len(values):raise ValueError('typed entity count drift '+kind)
                    fields={
                        'problems':('year','code','title','profile_version','problem_types'),
                        'subproblems':('problem_id','ordinal','goal'),
                        'papers':('year','problem_id','source_sha256','title','relation_status','profile_version'),
                        'paper_problem_links':('problem_id','status')}[kind]
                    for row in typed:
                        value=expected[row[idfield]]
                        for field in fields:
                            key='problem_code' if kind=='problems' and field=='code' else 'version' if kind=='problems' and field=='profile_version' else field
                            if row[field]!=value[key]:raise ValueError('typed database scalar drift '+kind+'.'+field)
                        if kind=='paper_problem_links':
                            if core.json_loads(row['evidence'])!=value['evidence']:raise ValueError('typed link evidence drift')
                        elif core.json_loads(row['payload'])!=value:raise ValueError('typed entity payload drift '+kind)
                    entities+=len(rows)
                attachments=core.load_jsonl(root/'metadata/catalog/attachment_profiles.jsonl')
                rows=await connection.fetch("SELECT entity_id,payload FROM content_de_codex.entities WHERE release_id=$1 AND kind='attachments'",args.release_id)
                expected_attachments={r['source_id']:r for r in attachments}
                if len(rows)!=len(attachments) or any(core.json_loads(r['payload'])!=expected_attachments[r['entity_id']] for r in rows):raise ValueError('attachment profile roundtrip drift')
                entities+=len(rows)
                # Vector data must be populated with the configured dimension and finite norms.
                invalid=await connection.fetchval('SELECT count(*) FROM content_de_codex.units WHERE release_id=$1 AND (vector_dims(embedding)<>384 OR vector_norm(embedding)<0.99 OR vector_norm(embedding)>1.01)',args.release_id)
                if invalid:
                    raise ValueError('invalid database embedding')
                return {'database_units':total,'database_assets':len(assets),'database_entities':entities}
        finally:
            await connection.close()
    result=asyncio.run(check())
    return dict(result,release_id=args.release_id,manifest_files=len(manifest['files']),read_only=True,cloud_deployed=False,integrity_scope='pinned_database_inputs_and_all_pg_fields' if _activation_database_inputs else 'all_sealed_files_and_all_pg_fields')

def evaluate(args):
    from . import consumer
    root=release_dir(args)
    result=read_only_check(args)
    units=core.load_jsonl(root/'metadata/catalog/retrieval_units.jsonl')
    # These are source-bound integration probes. They are not independent human relevance gold.
    candidates=[u for u in units if u['kind']=='historical_paper' and u['stage'] in consumer.STAGES]
    probes=[]
    seen=set()
    for unit in candidates:
        bucket=(unit['year'],unit['case_id'],unit['stage'])
        if bucket in seen:
            continue
        seen.add(bucket)
        if len(probes)>=40:
            break
        request={'request_id':'probe-%d'%len(probes),'competition_id':'cumcm','release_id':args.release_id,'historical_year_range':[unit['year'],unit['year']],
                 'subproblem':{'subproblem_id':'probe:q1','goal':unit['text'],'problem_types':[]},'stage':unit['stage'],'top_k':5,'token_budget':5000,'usage_purpose':'internal_audit'}
        response=consumer.retrieve(request,'internal_audit')
        references=response['historical_references']
        probes.append({'request_id':request['request_id'],'stage':unit['stage'],'year':unit['year'],'source_unit_id':unit['unit_id'],
                       'returned':len(references),'self_unit_at_5':unit['unit_id'] in [r['unit_id'] for r in references],
                       'token_budget_actual':response['budget']['actual'],'token_budget_limit':5000,
                       'pins_valid':all(r['source_ref']['sha256']==r['source_sha256'] for r in references)})
    import copy
    template={'competition_id':'cumcm','release_id':args.release_id,'subproblem':{'goal':'建立优化调度模型','problem_types':['optimization']},'stage':'model','usage_purpose':'consumer','top_k':5,'token_budget':4000}
    boundaries=[]
    for field,value in [('competition_id','mcm'),('stage','bad'),('release_id','../escape'),('historical_year_range',[2009,2025]),('historical_year_range',[2020,2026]),('top_k',False),('top_k',21),('token_budget',-1),('token_budget',float('nan'))]:
        request=copy.deepcopy(template);request[field]=value
        try:
            consumer.validate_request(request);passed=False
        except ValueError:
            passed=True
        boundaries.append({'field':field,'value':repr(value),'rejected':passed})
    request=copy.deepcopy(template);request['usage_purpose']='internal_audit'
    try:
        consumer.retrieve(request,'consumer');passed=False
    except PermissionError:
        passed=True
    boundaries.append({'field':'untrusted_internal_audit','rejected':passed})
    consumer_response=consumer.retrieve(template)
    boundaries.append({'field':'consumer_historical_gate','rejected':len(consumer_response['historical_references'])==0,'self_authored_recipes_returned':len(consumer_response['writing_recipes'])+len(consumer_response['figure_recipes'])})
    stats=dict(result,positive_probes=probes,boundaries=boundaries,semantic_probe_success=sum(p['returned']>0 and p['pins_valid'] for p in probes),
               independent_recall_at_5=None,independent_ndcg_at_5=None,evaluation_limit='40 source-text integration probes, not held-out semantic relevance gold; independent quality acceptance pending')
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_evaluation.json',stats)
    if len(probes)<40 or not all(p['returned'] and p['pins_valid'] and p['token_budget_actual']<=p['token_budget_limit'] for p in probes) or not all(b['rejected'] for b in boundaries):
        raise ValueError('integration evaluation failed; see report')
    print('40 real database retrieval probes and {} boundaries passed; independent relevance QA remains pending'.format(len(boundaries)))
    return 0

def activate(args,event_kind):
    import asyncio
    from . import services
    read_only_check(args,_activation_database_inputs=True)
    async def change():
        connection=await services.pg()
        try:
            async with connection.transaction():
                await connection.execute('SELECT pg_advisory_xact_lock(621032025)')
                sha=await connection.fetchval('SELECT manifest_sha256 FROM content_de_codex.releases WHERE release_id=$1',args.release_id)
                if sha!=core.sha256_of(release_dir(args)/'manifest.json'):raise ValueError('target release not fully imported')
                from .structured_store import activation_guard
                await activation_guard(connection,args.release_id)
                previous=await connection.fetchval("SELECT release_id FROM content_de_codex.activation WHERE competition_id='cumcm'")
                if previous==args.release_id:return {'status':'unchanged','release_id':args.release_id}
                await connection.execute("UPDATE content_de_codex.releases SET status='retired' WHERE release_id=$1",previous)
                await connection.execute("UPDATE content_de_codex.releases SET status='active' WHERE release_id=$1",args.release_id)
                await connection.execute("INSERT INTO content_de_codex.activation VALUES('cumcm',$1,now()) ON CONFLICT(competition_id) DO UPDATE SET release_id=excluded.release_id,updated_at=now()",args.release_id)
                await connection.execute("INSERT INTO content_de_codex.release_events(competition_id,previous_release,next_release,event_kind) VALUES('cumcm',$1,$2,$3)",previous,args.release_id,event_kind)
                return {'status':event_kind,'previous_release_id':previous,'release_id':args.release_id,'scope':'local-audit'}
        finally:await connection.close()
    print(json.dumps(asyncio.run(change())))
    return 0
