"""HTTP-only client; all new generative model artifacts live outside this project."""
import base64,json,time,urllib.request,urllib.error
from pathlib import Path
from . import core

SETTINGS=Path('D:/Erdos_LocalAI/api.json')

def formula_request(request,timeout):
    # Inference is read-only. A brief local service restart must not discard a
    # complete corpus run; persistent errors still surface after three attempts.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request,timeout=timeout) as response:
                return core.json_loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            if error.code not in (502,503,504) or attempt==2:raise
        except (urllib.error.URLError,TimeoutError,ConnectionError):
            if attempt==2:raise
        time.sleep(1+attempt)

def infer(prompt,images=(),max_new_tokens=768,timeout=300):
    cfg=core.load_json(SETTINGS)
    if cfg['host']!='127.0.0.1':raise ValueError('local-only model service required')
    value={'prompt':prompt,'max_new_tokens':max_new_tokens,
           'images_base64':[base64.b64encode(Path(p).read_bytes()).decode('ascii') for p in images]}
    req=urllib.request.Request('http://127.0.0.1:'+str(cfg['port'])+'/v1/infer',
        data=json.dumps(value,ensure_ascii=False).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['token']})
    with urllib.request.urlopen(req,timeout=timeout) as response:
        return core.json_loads(response.read().decode('utf-8'))

def formula_page(image,detect_only=False,isolated_only=True,timeout=600,settings_path=None):
    cfg=core.load_json(Path(settings_path or 'D:/Erdos_LocalAI/formula-api.json'))
    if cfg['host']!='127.0.0.1':raise ValueError('local-only formula service required')
    value={'image_base64':base64.b64encode(Path(image).read_bytes()).decode('ascii'),
        'detect_only':detect_only,'isolated_only':isolated_only,'max_new_tokens':256,'max_formulas':100}
    request=urllib.request.Request('http://127.0.0.1:'+str(cfg['port'])+'/v1/formulas',data=json.dumps(value).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['token']})
    return formula_request(request,timeout)

def formula_crop(image,expected_sha256,timeout=600):
    raw=Path(image).read_bytes()
    if core.sha256_bytes(raw)!=expected_sha256:raise ValueError('on-demand formula crop differs from pinned source')
    cfg=core.load_json(Path('D:/Erdos_LocalAI/formula-api.json'))
    if cfg['host']!='127.0.0.1':raise ValueError('local-only formula service required')
    request=urllib.request.Request('http://127.0.0.1:'+str(cfg['port'])+'/v1/formulas',
        data=json.dumps({'image_base64':base64.b64encode(raw).decode('ascii'),'transcribe_only':True,'max_new_tokens':256}).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['token']})
    value=formula_request(request,timeout)
    if value['input_sha256']!=expected_sha256:raise ValueError('on-demand formula input pin mismatch')
    return value
