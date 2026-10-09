"""Detected full formula regions and local ONNX LaTeX candidates; never auto-verified."""
import argparse,base64,collections,json,io
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from . import core,audit,local_closure,local_model_api

def page_one(path,detect_only=False):
    rd=audit.run_dir(local_closure.args());page=core.load_json(path);sha=page['input_sha256'];number=page['page']
    output=path.parent/('formula-page-%04d.json'%number)
    if output.exists():
        existing=core.load_json(output)
        if existing['input_sha256']!=page['full_page_evidence']['asset_ref']['sha256']:raise ValueError('cached formula page input drift')
        if detect_only or existing['mode']=='isolated_display_formulas':return existing
    evidence=page['full_page_evidence'];image=core.safe_relpath(core.CONTENT_DIR,evidence['path'])
    if core.sha256_of(image)!=evidence['asset_ref']['sha256']:raise ValueError('formula whole-page input checksum drift')
    result=local_model_api.formula_page(image,detect_only=detect_only)
    if result['input_sha256']!=evidence['asset_ref']['sha256']:raise ValueError('formula API input pin mismatch')
    formulas=[];scale=evidence['render_scale'];full_image=None
    for index,component in enumerate(result['components']):
        pixel=component.get('crop_bbox_pixels',component['bbox']);left,top,right,bottom=pixel
        crop_ref=None;crop_path=None
        if detect_only and component['label']=='isolated':
            if full_image is None:
                audit.deps();from PIL import Image
                full_image=Image.open(str(image))
            crop=full_image.crop(tuple(pixel));buffer=io.BytesIO();crop.save(buffer,format='PNG');raw=buffer.getvalue()
            component['crop_png_base64']=base64.b64encode(raw).decode('ascii');component['crop_sha256']=core.sha256_bytes(raw)
        if 'crop_png_base64' in component:
            raw=base64.b64decode(component.pop('crop_png_base64'),validate=True)
            if core.sha256_bytes(raw)!=component['crop_sha256']:raise ValueError('formula crop checksum drift')
            cp=path.parent/'formula_crops'/('p%04d-%s.png'%(number,component['crop_sha256'][:16]));core.write_bytes(cp,raw,immutable=True)
            crop_path=cp.relative_to(core.CONTENT_DIR).as_posix();crop_ref={'asset_id':'asset-'+component['crop_sha256'],'version':1,'sha256':component['crop_sha256']}
        row={'formula_region_id':'formula-region-'+core.sha256_bytes(sha+':'+str(number)+':'+json.dumps(pixel)+':'+result['model_revisions']['mfd']+':'+component['label']),
            'source_sha256':sha,'page':number,'bbox':[left/scale,page['height']-bottom/scale,right/scale,page['height']-top/scale],
            'coordinate_system':'pdf_points_bottom_left','region_type':component['label'],'detector_score':component['score'],
            'source_page_ref':evidence['asset_ref'],'crop_ref':crop_ref,'crop_path':crop_path,
            'latex_candidate':component.get('latex_candidate'),'semantic_status':'candidate' if crop_ref else 'abstained',
            'transcription_status':'truncated' if component.get('generation_truncated') else 'model_candidate' if component.get('latex_candidate') else 'not_transcribed_display_formula' if crop_ref else 'not_transcribed_inline_symbol',
            'generation_truncated':component.get('generation_truncated'),
            'geometric_mean_token_probability':component.get('geometric_mean_token_probability'),
            'semantics_verified':False,'model_revisions':result['model_revisions'],
            'source_is_instruction':False,'current_result_value_source':False}
        formulas.append(row)
    if full_image is not None:full_image.close()
    value={'source_sha256':sha,'page':number,'input_sha256':result['input_sha256'],'mode':'detected_with_lazy_transcription' if detect_only else 'isolated_display_formulas',
        'model_revisions':result['model_revisions'],'formulas':formulas,'elapsed_seconds':result['elapsed_seconds'],
        'review_policy':'independent region and transcription candidates; source inspection required before reuse; no invented historical chart data'}
    core.write_json(output,value);return value

def run(max_pages=0,detect_only=False,workers=2):
    rd=audit.run_dir(local_closure.args());paths=sorted((rd/'parsed').glob('*/page-*.json'))
    if max_pages:paths=paths[:max_pages]
    regions=[];counts=collections.Counter();elapsed=0.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index,result in enumerate(pool.map(partial(page_one,detect_only=detect_only),paths),1):
            regions.extend(result['formulas']);counts['pages']+=1;counts['regions']+=len(result['formulas']);counts['crop_evidence']+=sum(bool(f['crop_ref']) for f in result['formulas']);counts['transcribed']+=sum(bool(f['latex_candidate']) for f in result['formulas']);elapsed+=result['elapsed_seconds']
            if index%50==0 or index==len(paths):
                core.write_json(rd/'quality/formula_enrichment_progress.json',{'complete':index==len(paths) and not max_pages,'counts':dict(counts),
                    'total_pages':len(paths),'sum_inference_seconds':elapsed,'semantics_verified':False,'display_math':'all detected display regions have exact crop evidence; LaTeX available on demand','inline_math':'retained as geometry candidates; no automatic LaTeX transcription'});print('formula pages {}/{} regions={} transcribed={}'.format(index,len(paths),counts['regions'],counts['transcribed']),flush=True)
    if not max_pages:
        core.write_jsonl(rd/'catalog/formula_regions.jsonl',regions)
        local_closure.state('full_formula_regions','candidate',['quality/formula_enrichment_progress.json','catalog/formula_regions.jsonl'],dict(counts))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--max-pages',type=int,default=0);parser.add_argument('--detect-only',action='store_true');parser.add_argument('--workers',type=int,choices=(1,2,3),default=2);ns=parser.parse_args();run(ns.max_pages,ns.detect_only,ns.workers)
