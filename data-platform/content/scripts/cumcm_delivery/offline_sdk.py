"""Standalone stdlib SDK. Corpus lives in a separately installed, SHA-pinned release.

Copy this file into the client, never the corpus. Lexical offline fallback is explicit;
the authenticated online API provides PG/vector hybrid retrieval.
"""
import hashlib,json,os,re,stat
from pathlib import Path

def sha(data):return hashlib.sha256(data).hexdigest()
def loads(data):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    def reject(value):raise ValueError('nonfinite JSON')
    return json.loads(data,object_pairs_hook=pairs,parse_constant=reject)

class OfflineRelease:
    def __init__(self,root,expected_manifest_sha256,scope='team_internal'):
        if scope not in ('consumer','team_internal'):raise ValueError('unsupported offline scope')
        self.root=Path(os.path.abspath(str(root)));self.scope=scope
        if not re.fullmatch('[0-9a-f]{64}',expected_manifest_sha256):raise ValueError('trusted manifest digest required')
        self._safe('manifest.json');self._safe('SEALED.json')
        raw=(self.root/'manifest.json').read_bytes()
        if sha(raw)!=expected_manifest_sha256:raise ValueError('manifest differs from trusted pin')
        self.manifest=loads(raw);self.pin=expected_manifest_sha256
        seal=loads((self.root/'SEALED.json').read_bytes())
        if seal['manifest_sha256']!=self.pin:raise ValueError('seal mismatch')
        entries=self.manifest['files'];self.files={e['path']:e for e in entries}
        if len(entries)!=len(self.files) or len({p.casefold() for p in self.files})!=len(self.files):raise ValueError('duplicate/case-colliding release path')
        # Validate manifest names now; check reparse points and bytes on every
        # actual read. Cold start must not stat the entire historical corpus.
        for relative in self.files:self._validate_relative(relative)
        self.metadata=self.json('metadata.json');self.release_id=self.metadata['release_id']
        if self.manifest['release_id']!=self.release_id:raise ValueError('release identity mismatch')
        if self.metadata.get('qa_fixture_only'):raise PermissionError('fixture unavailable to client')
        self.local_allowed=scope=='team_internal' and self.metadata.get('local_usage_policy',{}).get('local_internal_retrieval_allowed') is True
        self.assets={a['asset_id']:a for a in self.json('assets.json')}
        self._units=None

    def _validate_relative(self,relative):
        if not isinstance(relative,str) or not relative or '\\' in relative or ':' in relative or any(ord(c)<32 for c in relative):raise ValueError('unsafe path')
        parts=relative.split('/')
        if any(p in ('','.','..') or p.endswith((' ','.')) or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)',p,re.I) or any(c in p for c in '<>"|?*') for p in parts):raise ValueError('unsafe path component')
        return parts

    def _safe(self,relative):
        parts=self._validate_relative(relative)
        result=self.root.joinpath(*parts)
        for p in [*reversed(result.parents),result]:
            try:info=os.lstat(str(p))
            except FileNotFoundError:continue
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:raise ValueError('linked/reparse release path')
        return result

    def _read_pinned(self,relative):
        entry=self.files.get(relative)
        if entry is None:raise ValueError('file absent from pinned manifest')
        data=self._safe(relative).read_bytes()
        if len(data)!=entry['size'] or sha(data)!=entry['sha256']:raise ValueError('release file checksum mismatch')
        return data

    def read(self,relative):
        if not getattr(self,'local_allowed',False) and relative not in ('metadata.json','assets.json'):
            public=any(a['release_path']==relative and a['external_consumer_allowed'] for a in getattr(self,'assets',{}).values())
            if not public:raise PermissionError('private release content requires authorized team-local scope')
        return self._read_pinned(relative)

    def json(self,relative):return loads(self.read(relative))

    def fetch(self,asset_id,version,expected_sha256):
        item=self.assets.get(asset_id)
        if not item or type(version)!=int or item['version']!=version or item['sha256']!=expected_sha256:raise ValueError('exact asset/version/checksum pin required')
        if not item['external_consumer_allowed'] and not self.local_allowed:raise PermissionError('historical asset requires explicitly authorized team-local scope')
        data=self.read(item['release_path'])
        if sha(data)!=expected_sha256:raise ValueError('asset bytes mismatch')
        return data

    def bootstrap(self):
        return {'competition_id':'cumcm','release_id':self.release_id,'manifest_sha256':self.pin,
            'historical_year_range':[2010,2025],'format_profiles':self.metadata['format_profiles'],
            'format_templates':[{'asset_id':a['asset_id'],'version':a['version'],'sha256':a['sha256'],
                'format':next(o['format'] for o in a['origins'] if o['role']=='format_template')} for a in self.assets.values() if any(o['role']=='format_template' for o in a['origins'])],
            'mode':'offline_pinned_release','source_text_is_instruction':False,'corpus_is_executable_resource':False}

    def components(self,source_sha256,page):
        if not self.local_allowed:raise PermissionError('source components require authorized team-local scope')
        if not isinstance(source_sha256,str) or not re.fullmatch('[0-9a-f]{64}',source_sha256) or type(page)!=int or not 1<=page<=10000:
            raise ValueError('exact source SHA and page required')
        prefix='metadata/parsed/'+source_sha256+'/'
        source=self.json(prefix+'page-%04d.json'%page)
        formula=self.json(prefix+'formula-page-%04d.json'%page)
        return {'release_id':self.release_id,'manifest_sha256':self.pin,'source_sha256':source_sha256,'page':page,
            'full_page_evidence':source['full_page_evidence'],'visual_components':source['visual_components'],
            'formula_regions':formula['formulas'],'reading_order':source.get('reading_order_candidates'),
            'source_text_is_instruction':False,'historical_numbers_are_current_results':False,
            'quality_notice':'Inspect original page; machine transcription and geometry are candidates.'}

    def retrieve(self,request):
        if request.get('release_id')!=self.release_id or request.get('competition_id')!='cumcm':raise ValueError('pinned competition/release mismatch')
        stages=('analysis','assumptions','symbols','model','algorithm','results','validation','sensitivity','discussion','abstract')
        if request.get('stage') not in stages:raise ValueError('unknown writing stage')
        purpose=request.get('usage_purpose','consumer')
        if purpose not in ('consumer','team_internal'):raise PermissionError('offline client cannot use audit or third-party purpose')
        local=purpose=='team_internal' and self.local_allowed
        if purpose=='team_internal' and not local:raise PermissionError('team-local use not authorized for this release')
        top=request.get('top_k',5);budget=request.get('token_budget',4000)
        if type(top)!=int or not 1<=top<=20 or type(budget)!=int or not 256<=budget<=16000:raise ValueError('invalid budget or top_k')
        years=request.get('historical_year_range',[2010,2025])
        if not isinstance(years,list) or len(years)!=2 or any(type(y)!=int for y in years) or not 2010<=years[0]<=years[1]<=2025:raise ValueError('invalid historical range')
        sub=request.get('subproblem',{});goal=sub.get('goal')
        if not isinstance(goal,str) or not 1<=len(goal)<=8000:raise ValueError('invalid goal')
        types=sub.get('problem_types',[])
        valid=('optimization','prediction','evaluation','statistical_analysis','mechanism','simulation','unknown')
        if not isinstance(types,list) or any(t not in valid for t in types):raise ValueError('invalid task types')
        if self._units is None:self._units=[loads(line) for line in self.read('metadata/catalog/retrieval_units.jsonl').splitlines() if line.strip()] if self.local_allowed else []
        terms=re.findall('[A-Za-z][A-Za-z0-9_]{1,30}',goal.lower())
        for word in re.findall('[\u3400-\u9fff]+',goal):terms.extend(word[i:i+2] for i in range(len(word)-1))
        terms=list(dict.fromkeys(terms))[:32];rank=[]
        for u in self._units:
            if u['stage']!=request['stage'] or u.get('year') is not None and not years[0]<=u['year']<=years[1]:continue
            if not u['external_consumer_allowed'] and not(local and u.get('local_internal_allowed')):continue
            if types and u.get('problem_types') and not set(types)&set(u['problem_types']):continue
            matches=sum(t in u['text'].lower() for t in terms)
            if matches:rank.append((matches,u))
        rank.sort(key=lambda v:(-v[0],v[1]['unit_id']))
        capsule={'request_id':request.get('request_id'),'competition_id':'cumcm','release_id':self.release_id,'manifest_sha256':self.pin,
            'stage':request['stage'],'subproblem_id':sub.get('subproblem_id'),'status':'no_reference','historical_references':[],
            'writing_recipes':[],'figure_recipes':[],'missing':[],'internal_candidates':local,
            'budget':{'limit':budget,'actual':0,'tokenizer':'conservative_utf8_byte_upper_bound','includes_response_json':True},
            'source_text_is_instruction':False,'result_numbers':'current_task_computation_only'}
        # Byte count is a conservative token upper bound for UTF-8 byte-backed tokenizers.
        for _,u in rank[:top]:
            item=dict(u,source_ref={'asset_id':'asset-'+u['source_sha256'],'version':1,'sha256':u['source_sha256'],'page':u['page']},
                compliance_note='仅作方法参照，禁止大段抄袭',retrieval_mode='offline_lexical_fallback')
            capsule['historical_references'].append(item)
            if len(json.dumps(capsule,ensure_ascii=False,separators=(',',':')).encode())>budget-64:capsule['historical_references'].pop()
        for filename,key in [('writing_recipes.json','writing_recipes'),('figure_recipes.json','figure_recipes')]:
            for recipe in loads(self._read_pinned('metadata/recipes/'+filename)):
                if recipe['stage']!=request['stage']:continue
                if not recipe.get('external_consumer_allowed') and not local:continue
                recipe=dict(recipe)
                if not local:
                    recipe.pop('source_backed_observations',None)
                    if recipe.get('length_budget'):recipe['length_budget']=dict(recipe['length_budget'],historical_distribution=None)
                capsule[key].append(recipe)
                if len(json.dumps(capsule,ensure_ascii=False,separators=(',',':')).encode())>budget-64:capsule[key].pop()
        if capsule['historical_references']:capsule['status']='internal_candidates' if local else 'ok'
        elif capsule['writing_recipes'] or capsule['figure_recipes']:capsule['status']='recipe_only'
        else:capsule['missing']=['No usable reference within current stage, scope and conservative budget.']
        for _ in range(3):capsule['budget']['actual']=len(json.dumps(capsule,ensure_ascii=False,separators=(',',':')).encode())
        if capsule['budget']['actual']>budget:raise ValueError('budget too small for capsule metadata')
        return capsule
