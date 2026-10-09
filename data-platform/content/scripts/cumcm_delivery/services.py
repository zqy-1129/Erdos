"""Real local PostgreSQL, S3 and pinned CPU ONNX embedding adapters."""
import asyncio
import re
import os,sys
from pathlib import Path
from . import core
from .audit import RUNTIME, deps

_models = {}
_s3 = None
_budget_tokenizer = None
_pin_cache = None
_embedding_cache_pin = None
_dll_handles = []

def configuration():
    cfg = core.load_json(RUNTIME / 'local/services.json')
    if cfg.get('environment') != 'local-audit':
        raise ValueError('only explicitly configured local-audit target is enabled')
    return cfg

def s3():
    global _s3
    if _s3 is not None:
        return _s3
    deps()
    import boto3
    from botocore.config import Config
    cfg = dict(configuration()['s3'])
    bucket = cfg.pop('bucket')
    client = boto3.client('s3', config=Config(signature_version='s3v4', s3={'addressing_style':'path'}, retries={'max_attempts':3}), **cfg)
    _s3 = client, bucket
    return _s3

async def pg():
    deps()
    import asyncpg
    cfg = dict(configuration()['postgres'])
    cfg['database'] = cfg.pop('dbname')
    return await asyncpg.connect(**cfg)

def embedding_folder():
    return core.no_links(Path(os.environ.get('ERDOS_EMBEDDING_DIR', str(RUNTIME/'embedding-model'))))

def model(query=False):
    deps()
    cfg=configuration()
    build_provider=cfg['embedding'].get('provider','CPUExecutionProvider')
    provider=cfg.get('query_embedding_provider',build_provider) if query else build_provider
    key=(str(embedding_folder()),provider)
    if key not in _models:
        if provider=='CUDAExecutionProvider':
            sys.path.insert(0,str(RUNTIME/'py38-gpu119'))
            previous=Path.resolve
            try:
                Path.resolve=lambda self,strict=False:self.absolute()
                import torch,onnxruntime as ort
            finally:Path.resolve=previous
            _dll_handles.append(os.add_dll_directory(str(Path(torch.__file__).parent/'lib')))
            providers=[('CUDAExecutionProvider',{'gpu_mem_limit':2147483648,'arena_extend_strategy':'kSameAsRequested','cudnn_conv_algo_search':'DEFAULT'}),'CPUExecutionProvider']
        elif provider=='CPUExecutionProvider':
            import onnxruntime as ort
            providers=['CPUExecutionProvider']
        else:raise ValueError('unsupported configured embedding provider')
        if query and provider != build_provider and ort.__version__ != '1.19.2':
            raise ValueError('explicit CPU query compatibility requires onnxruntime 1.19.2')
        from tokenizers import Tokenizer
        folder = embedding_folder()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(folder / 'model_optimized.onnx'), sess_options=options, providers=providers)
        if session.get_providers()[0]!=provider:raise ValueError('configured embedding provider unavailable; no silent fallback')
        tokenizer = Tokenizer.from_file(str(folder / 'tokenizer.json'))
        tokenizer.enable_truncation(max_length=128)
        tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
        _models[key] = session, tokenizer
    return _models[key]

def embed(texts, query=False):
    import numpy as np
    session, tokenizer = model(query=query)
    global _budget_tokenizer
    if _budget_tokenizer is None:
        from tokenizers import Tokenizer
        _budget_tokenizer=Tokenizer.from_file(str(embedding_folder()/'tokenizer.json'))
        _budget_tokenizer.no_truncation()
        _budget_tokenizer.no_padding()
    windows=[]
    for owner,text in enumerate(texts):
        full=_budget_tokenizer.encode(text).ids
        content=full[1:-1]
        for offset in range(0,max(1,len(content)),126):
            sequence=[full[0]]+content[offset:offset+126]+[full[-1]]
            windows.append((owner,sequence))
    accum=np.zeros((len(texts),384),dtype=np.float64)
    denominators=np.zeros((len(texts),1),dtype=np.float64)
    for offset in range(0,len(windows),32):
        batch=windows[offset:offset+32]
        length=max(len(ids) for _,ids in batch)
        ids=np.asarray([v+[1]*(length-len(v)) for _,v in batch],dtype=np.int64)
        mask=np.asarray([[1]*len(v)+[0]*(length-len(v)) for _,v in batch],dtype=np.int64)
        values={'input_ids':ids,'attention_mask':mask,'token_type_ids':np.zeros_like(ids)}
        output=session.run(None,{i.name:values[i.name] for i in session.get_inputs()})[0]
        if output.ndim==3:
            weights=mask[:,:,None]
            output=(output*weights).sum(1)/np.maximum(weights.sum(1),1)
        for wi,(owner,sequence) in enumerate(batch):
            weight=len(sequence)
            accum[owner]+=output[wi]*weight
            denominators[owner]+=weight
    output=accum/np.maximum(denominators,1)
    if output.shape != (len(texts),384) or not np.isfinite(output).all():
        raise ValueError('invalid real embedding output')
    output = output / np.maximum(np.linalg.norm(output, axis=1, keepdims=True), 1e-12)
    return output.tolist()

def tokens(text):
    return untruncated_tokens(text)

def untruncated_tokens(text):
    # Separate tokenizer: inference truncation must never hide response token usage.
    deps()
    from tokenizers import Tokenizer
    global _budget_tokenizer
    if _budget_tokenizer is None:
        _budget_tokenizer = Tokenizer.from_file(str(embedding_folder() / 'tokenizer.json'))
        _budget_tokenizer.no_truncation()
        _budget_tokenizer.no_padding()
    return len(_budget_tokenizer.encode(text).ids)

def model_manifest():
    return dict(configuration()['embedding'], pooling='attention_mask_mean_then_token_weighted_windows', implementation_version='whole_text_windows_v3_no_saved_tokenizer_truncation', normalization='L2', max_sequence_tokens=128, window_content_tokens=126,
                runtime='onnxruntime-1.19.2-cuda12' if configuration()['embedding'].get('provider')=='CUDAExecutionProvider' else 'onnxruntime-1.17.3-cpu', files={p.name:core.sha256_of(p) for p in sorted(embedding_folder().iterdir()) if p.is_file()})

def verify_model_pin(expected):
    global _pin_cache
    signature=(str(embedding_folder()), core.sha256_bytes(__import__('json').dumps(configuration()['embedding'],sort_keys=True))), tuple((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(embedding_folder().iterdir()) if p.is_file())
    if _pin_cache is None or _pin_cache[0]!=signature:
        _pin_cache=(signature,model_manifest())
    if expected!=_pin_cache[1]:
        raise ValueError('release embedding model or pooling space mismatch')

def embed_cached(texts):
    """Local resumable inference cache; the release is still imported into real PostgreSQL."""
    import json,sqlite3,numpy as np
    global _embedding_cache_pin
    if _embedding_cache_pin is None:
        _embedding_cache_pin=core.sha256_bytes(json.dumps(model_manifest(),sort_keys=True,ensure_ascii=False))
    connection=sqlite3.connect(str(RUNTIME/'embedding-cache.sqlite3'),timeout=60)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS vectors(model_pin TEXT,text_sha TEXT,payload TEXT,payload_sha TEXT,PRIMARY KEY(model_pin,text_sha))')
        keys=[core.sha256_bytes(t) for t in texts]
        found={}
        for key in set(keys):
            row=connection.execute('SELECT payload,payload_sha FROM vectors WHERE model_pin=? AND text_sha=?',(_embedding_cache_pin,key)).fetchone()
            if row:
                if core.sha256_bytes(row[0])!=row[1]:raise ValueError('inference cache checksum mismatch')
                vector=core.json_loads(row[0]);array=np.asarray(vector)
                if array.shape!=(384,) or not np.isfinite(array).all() or abs(np.linalg.norm(array)-1)>0.0001:raise ValueError('invalid inference cache vector')
                found[key]=vector
        missing={key:text for key,text in zip(keys,texts) if key not in found}
        if missing:
            values=embed(list(missing.values()))
            for key,vector in zip(missing,values):
                payload=json.dumps(vector,allow_nan=False,separators=(',',':'))
                connection.execute('INSERT OR IGNORE INTO vectors VALUES(?,?,?,?)',(_embedding_cache_pin,key,payload,core.sha256_bytes(payload)))
                found[key]=vector
            connection.commit()
        return [found[key] for key in keys]
    finally:connection.close()

def object_key(sha):
    if not re.fullmatch('[0-9a-f]{64}', sha):
        raise ValueError('invalid content digest')
    return 'content/cumcm/sha256/' + sha[:2] + '/' + sha

def put_verified(path, sha):
    client, bucket = s3()
    data = core.no_links(path).read_bytes()
    if core.sha256_bytes(data) != sha:
        raise ValueError('upload input checksum mismatch')
    key = object_key(sha)
    try:
        old = client.get_object(Bucket=bucket,Key=key)['Body'].read()
    except client.exceptions.NoSuchKey:
        old = None
    if old is not None:
        if core.sha256_bytes(old) != sha:
            raise ValueError('object conflict; never overwrite different bytes')
        # This is already a complete GET readback, not an ETag or metadata check.
        return key
    client.put_object(Bucket=bucket,Key=key,Body=data,Metadata={'sha256':sha})
    actual = client.get_object(Bucket=bucket,Key=key)['Body'].read()
    if core.sha256_bytes(actual) != sha:
        raise ValueError('S3 readback checksum mismatch')
    return key

def get_verified(sha):
    client,bucket = s3()
    body = client.get_object(Bucket=bucket,Key=object_key(sha))['Body'].read()
    if core.sha256_bytes(body) != sha:
        raise ValueError('download hash mismatch')
    return body

def smoke():
    client,bucket = s3()
    existing = [b['Name'] for b in client.list_buckets()['Buckets']]
    if bucket not in existing:
        client.create_bucket(Bucket=bucket)
    vectors = embed(['建立优化模型并求解最优方案','时间序列预测模型','优化调度的目标函数与约束'])
    async def check():
        connection = await pg()
        try:
            return await connection.fetchval('select version()')
        finally:
            await connection.close()
    version = asyncio.run(check())
    manifest = model_manifest()
    core.write_json(core.CONTENT_DIR / 'reports/trae/cumcm_codex_runtime.json', {'python':core.PY,'postgres_version':version,'s3_endpoint':'http://127.0.0.1:18333','bucket':bucket,'embedding':manifest,'vector_dimensions':[len(v) for v in vectors], 'smoke':'real services connected; no credential values included'})
    print('PostgreSQL, S3 and real embedding smoke passed')

if __name__ == '__main__':
    smoke()
