"""Independent visual-model review candidates, durable inputs and explicit abstentions.

These observations are not human ground truth. Source-inspected corrections are stored
separately and never inferred from model agreement.
"""
import argparse,io,time
from . import core,audit,local_closure,local_model_api

PROMPTS={
    'paragraph':'请逐字转写图中正文，不作总结，不补写看不清的字；看不清标为[不清]。保留数学符号。只输出转写文字。',
    'formula':'请读取图中可见的完整数学公式，转写为LaTeX，并说明每个可见变量定义。不要猜测截断部分；如果截图只是公式片段，输出「公式片段，无法确认完整式」。只依据图像，不补历史计算结果。',
    'figure':'请检查本页是否有完整图像。用短句列出可见图号、标题、图型、横纵坐标单位和图例。不要从像素反推数值。标题与图的对应关系不清时明确说无法确认。',
    'table':'请检查本页是否有完整表格。用短句列出可见表号、标题、列名、单位、是否跨页。不要补造表格数值。表题与表的对应关系不清时明确说无法确认。',
    'structure':'只按本页实际阅读顺序列出章节标题和层级。忽略公式行、页码、正文交叉引用和图题表题。没有章节标题则只输出「无标题」。不要续写下一页标题。'}

def crop_for(row,rd):
    audit.deps();from PIL import Image
    sha=row['source_sha256'];page=core.load_json(rd/'parsed'/sha/('page-%04d.json'%row['page']))
    evidence=page.get('full_page_evidence')
    if not evidence:raise ValueError('complete source page not yet rendered')
    full=core.safe_relpath(core.CONTENT_DIR,evidence['path'])
    if core.sha256_of(full)!=evidence['asset_ref']['sha256']:raise ValueError('review source image checksum drift')
    if row['kind'] in ('figure','table','structure'):return full
    selected=set(row.get('block_ids') or [row.get('block_id')]);blocks=[b for b in page['blocks'] if b['block_id'] in selected]
    if not blocks:raise ValueError('review block locator missing')
    top=max(b['bbox'][3] for b in blocks);bottom=min(b['bbox'][1] for b in blocks)
    if row['kind']=='formula':top+=38;bottom-=38
    else:top+=5;bottom-=5
    image=Image.open(str(full));scale=evidence['render_scale']
    crop=image.crop((0,max(0,int((page['height']-top)*scale)),image.width,min(image.height,int((page['height']-bottom)*scale))))
    if crop.height<=0:raise ValueError('review context crop empty')
    data=io.BytesIO();crop.save(data,format='PNG');raw=data.getvalue()
    path=rd/'quality/review_inputs'/(core.sha256_bytes(raw)+'.png');core.write_bytes(path,raw,immutable=True)
    return path

def review_one(row,rd):
    key=core.sha256_bytes(row['review_id']);path=rd/'quality/local_model_reviews'/(key+'.json')
    if path.exists():return core.load_json(path)
    image=crop_for(row,rd);kind=row['kind'];started=core.now_utc_iso()
    response=local_model_api.infer(PROMPTS[kind],[image],max_new_tokens=384 if kind in ('paragraph','formula') else 160)
    value={'review_id':row['review_id'],'kind':kind,'source_sha256':row['source_sha256'],'page':row['page'],
        'input_sha256':core.sha256_of(image),'input_path':image.relative_to(core.CONTENT_DIR).as_posix(),
        'reviewer_type':'local_model_candidate_not_human_ground_truth','status':'candidate',
        'finding':response['text'] or '模型未输出；弃权','ground_truth':None,'model_revision':response['revision'],
        'inference':response,'started_at':started,'finished_at':core.now_utc_iso(),'requires_source_inspection':True}
    core.write_json(path,value);return value

def run(kind=None):
    rd=audit.run_dir(local_closure.args());base=core.load_jsonl(rd/'quality/quality_review_queue.jsonl');queue=[]
    for row in base:
        if row['kind']=='full_paper_structure':
            if kind and kind!='structure':continue
            for page in range(row['page_range'][0],row['page_range'][1]+1):queue.append({'review_id':row['review_id']+':p'+str(page),'kind':'structure','source_sha256':row['source_sha256'],'page':page,'case_id':row['case_id']})
        elif not kind or row['kind']==kind:queue.append(row)
    # Review local components first; full-paper structure follows without tuning extraction on heldout labels.
    queue.sort(key=lambda r:({'formula':0,'figure':1,'table':2,'paragraph':3,'structure':4}[r['kind']],r['review_id']))
    missing=[];completed=0
    for row in queue:
        path=rd/'parsed'/row['source_sha256']/('page-%04d.json'%row['page'])
        if not core.load_json(path).get('full_page_evidence'):missing.append(row);continue
        review_one(row,rd);completed+=1
        if completed%50==0:
            core.write_json(rd/'quality/local_model_review_progress.json',{'total':len(queue),'completed':completed,'waiting_for_render':len(missing),
                'all_queue_items_attempted':False,'reviewer_type':'local_model_candidate_not_human_ground_truth','human_ground_truth_claimed':False})
            print('local visual review {}/{} ({})'.format(completed,len(queue),row['kind']),flush=True)
    core.write_json(rd/'quality/local_model_review_progress.json',{'total':len(queue),'completed':completed,'waiting_for_render':len(missing),
        'all_queue_items_attempted':not missing,'reviewer_type':'local_model_candidate_not_human_ground_truth','human_ground_truth_claimed':False})
    local_closure.state('local_model_visual_review','candidate' if not missing else 'running',['quality/local_model_review_progress.json'],{'completed':completed,'total':len(queue),'waiting_for_render':len(missing),'ground_truth':False})

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--kind',choices=list(PROMPTS));ns=parser.parse_args();run(ns.kind)
