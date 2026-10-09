"""Precompute real inference for completed page checkpoints; never invent vectors."""
import json,time
from . import audit,core,services

def shard(values):
    index,texts=values
    for offset in range(0,len(texts),64):
        services.embed_cached(texts[offset:offset+64])
        if offset%1024==0:print('real inference worker {} cached {}/{}'.format(index,min(offset+64,len(texts)),len(texts)),flush=True)
    return len(texts)

def main():
    root=core.CONTENT_DIR/'normalized/cumcm_delivery/cumcm-2010-2025-codex-r002'
    units=core.load_jsonl(root/'catalog/retrieval_units.jsonl')
    texts=[u['text'] for u in units]
    for name in ('writing_recipes','figure_recipes'):
        texts.extend(json.dumps(r,ensure_ascii=False) for r in core.load_json(root/'recipes'/(name+'.json')))
    start=time.monotonic()
    from concurrent.futures import ProcessPoolExecutor
    workers=1 if services.configuration()['embedding'].get('provider')=='CUDAExecutionProvider' else 2
    with ProcessPoolExecutor(max_workers=workers) as pool:
        list(pool.map(shard,[(i,texts[i::workers]) for i in range(workers)]))
    core.write_json(root/'quality/embedding_precompute.json',{'input_texts':len(texts),'elapsed_seconds':time.monotonic()-start,'complete':True,'cache_role':'resumable local ONNX inference only; release targets real PostgreSQL','model_manifest':services.model_manifest()})

if __name__=='__main__':main()
