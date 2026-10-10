"""Stdlib-only local-model API client; caller provides credentials and model pins."""
import base64,hashlib,json,re,urllib.request

class LocalAIClient:
    def __init__(self,text_token,formula_token,model_revisions):
        if not all(isinstance(x,str) and x for x in (text_token,formula_token)):raise ValueError('local caller credentials required')
        if not isinstance(model_revisions,dict) or set(model_revisions)!= {'qwen','mfd','mfr'} or any(not re.fullmatch('[0-9a-f]{40}',str(x)) for x in model_revisions.values()):
            raise ValueError('trusted exact model revisions required')
        self.text_token=text_token;self.formula_token=formula_token;self.pins=dict(model_revisions)

    def _post(self,port,route,token,value):
        req=urllib.request.Request('http://127.0.0.1:'+str(port)+route,
            data=json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8'),
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=600) as r:result=json.loads(r.read().decode('utf-8'))
        if result.get('inference_scope')!='local_only':raise ValueError('local inference scope differs')
        return result

    def infer(self,prompt,images=(),max_new_tokens=768):
        if not isinstance(prompt,str) or not 1<=len(prompt)<=24000 or type(max_new_tokens)!=int or not 16<=max_new_tokens<=2048:
            raise ValueError('invalid text inference limits')
        if len(images)>3 or any(not isinstance(b,bytes) or len(b)>12*1024**2 for b in images):raise ValueError('bounded local image bytes required')
        result=self._post(18790,'/v1/infer',self.text_token,{'prompt':prompt,'images_base64':[base64.b64encode(b).decode('ascii') for b in images],'max_new_tokens':max_new_tokens})
        if result['revision']!=self.pins['qwen'] or result['image_sha256']!=[hashlib.sha256(b).hexdigest() for b in images] or result['prompt_sha256']!=hashlib.sha256(prompt.encode()).hexdigest():
            raise ValueError('text model or input pin differs')
        return result

    def transcribe_formula(self,image_bytes,expected_sha256,max_new_tokens=256):
        if not isinstance(image_bytes,bytes) or len(image_bytes)>12*1024**2 or hashlib.sha256(image_bytes).hexdigest()!=expected_sha256:
            raise ValueError('formula bytes differ from trusted source digest')
        if type(max_new_tokens)!=int or not 16<=max_new_tokens<=512:raise ValueError('invalid formula token limit')
        result=self._post(18794,'/v1/formulas',self.formula_token,{'image_base64':base64.b64encode(image_bytes).decode('ascii'),
            'transcribe_only':True,'max_new_tokens':max_new_tokens})
        if result['input_sha256']!=expected_sha256 or result['model_revisions']!={k:self.pins[k] for k in ('mfd','mfr')}:
            raise ValueError('formula input/model pin differs')
        if result.get('current_result_value_source') is not False or any(c.get('semantics_verified') is not False for c in result['components']):
            raise ValueError('candidate formula cannot become verified/current computation')
        return result
