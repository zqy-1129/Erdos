"""Shared strict JSON, immutable snapshots, resource verification and draft contract checks."""
import hashlib,json,os,re,zipfile,io,sys,subprocess,tempfile
from pathlib import Path
import stage03_fileio as fio
CONTENT=Path(os.path.abspath(__file__)).parent.parent
for stream in [sys.stdout,sys.stderr]:
 if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf8')
def disjoint_sources(source,output):
 a=os.path.normcase(str(fio.no_links(source)));b=os.path.normcase(str(fio.no_links(output)))
 try:common=os.path.commonpath([a,b])
 except ValueError:return
 if common in [a,b]:raise ValueError('SOURCE_OUTPUT_OVERLAP')
def json_bytes(obj):return (json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf8')
def pairs(items):
 out={}
 for k,v in items:
  if k in out:raise ValueError('DUPLICATE_JSON_KEY: '+k)
  out[k]=v
 return out
def parse(data):
 return json.loads(data,object_pairs_hook=pairs,parse_constant=lambda s:(_ for _ in ()).throw(ValueError('NONFINITE_JSON: '+s)))
def load(path):return parse(fio.no_links(path).read_text(encoding='utf8'))
def write(path,obj):return fio.write_exact(path,json_bytes(obj))
def checked(path,sha=None,size=None):
 info=fio.hash_verified(path)
 if info['status']!='ok':raise ValueError('RESOURCE_UNAVAILABLE: '+str(path)+' '+info['status'])
 if sha is not None and info['sha256']!=sha:raise ValueError('RESOURCE_HASH: '+str(path))
 if size is not None and info['size']!=size:raise ValueError('RESOURCE_SIZE: '+str(path))
 return info
def identifier(value):
 if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,200}',value) or value in ['.','..'] or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',value,re.I):raise ValueError('INVALID_ID: '+str(value))
 return value
def validate(bundle):
 loaded=[sys.modules.get(name) for name in ['jsonschema','attr','attrs','pyrsistent','importlib_resources','zipp']]
 if any(m is not None and '.deps' not in str(getattr(m,'__file__','')) for m in loaded):
  fd,name=tempfile.mkstemp(suffix='.json');os.close(fd)
  try:
   Path(name).write_bytes(json_bytes(bundle));r=subprocess.run([sys.executable,'-S',str(CONTENT/'scripts/validate_content_contracts.py'),'--check-bundle',name],capture_output=True,text=True,encoding='utf8',env=dict(os.environ,PYTHONIOENCODING='utf-8'))
   if r.returncode:raise ValueError('CONTRACT: '+r.stdout+r.stderr)
   return
  finally:Path(name).unlink()
 from normalize_cumcm_sample import validate_contract
 errors=validate_contract(bundle)
 if errors:raise ValueError('CONTRACT: '+'; '.join(errors[:12]))
def seal(root,inputs=None,metadata=None):
 root=fio.no_links(root)
 outputs={str(p.relative_to(root)).replace('\\','/'):checked(p)['sha256'] for p in sorted(root.rglob('*')) if p.is_file() and p.name!='integrity.json'}
 proof={'version':1,'inputs':inputs or {},'outputs':outputs,'metadata':metadata or {}}
 write(root/'integrity.json',proof)
 return proof
def check_snapshot(root,full=True):
 root=fio.no_links(root);proof=load(root/'integrity.json')
 if not proof.get('outputs'):raise ValueError('EMPTY_SNAPSHOT')
 if full:
  actual={str(p.relative_to(root)).replace('\\','/') for p in root.rglob('*') if p.is_file() and p.name!='integrity.json'}
  if actual!=set(proof['outputs']):raise ValueError('SNAPSHOT_COVERAGE: missing/extra business files')
  for rel,sha in proof['outputs'].items():checked(fio.relative(root,rel),sha)
 return proof
def locked_json(root,rel,proof):
 if rel not in proof['outputs']:raise ValueError('UNTRACKED_METADATA: '+rel)
 path=fio.relative(root,rel);data=path.read_bytes()
 if hashlib.sha256(data).hexdigest()!=proof['outputs'][rel]:raise ValueError('METADATA_HASH: '+rel)
 return parse(data.decode('utf8'))
def check_zip(data):
 with zipfile.ZipFile(io.BytesIO(data)) as z:
  names=z.namelist()
  if len(names)!=len(set(names)):raise ValueError('DUPLICATE_ZIP_MEMBER')
  if len(names)>20000 or sum(i.file_size for i in z.infolist())>256*1024*1024:raise ValueError('ZIP_LIMIT')
  for i in z.infolist():
   rel=i.orig_filename.rstrip('/')
   if not rel:raise ValueError('INVALID_ZIP_MEMBER')
   if '\\' in rel:raise ValueError('ZIP_BACKSLASH_MEMBER')
   if any(part.endswith(('.', ' ')) or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',part,re.I) for part in rel.split('/')):raise ValueError('ZIP_WINDOWS_MEMBER')
   fio.relative(CONTENT/'tmp/zip-check',rel)
   if (i.external_attr>>16)&0o170000==0o120000:raise ValueError('ZIP_LINK')
   if i.flag_bits&1:raise ValueError('ENCRYPTED_ZIP')
  if z.testzip() is not None:raise ValueError('ZIP_CRC')
 return names
def deterministic_zip(folder):
 root=fio.no_links(folder);buf=io.BytesIO()
 with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
  for p in sorted(root.rglob('*')):
   if not p.is_file():continue
   fio.no_links(p);rel=str(p.relative_to(root)).replace('\\','/')
   info=zipfile.ZipInfo(rel,(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
   z.writestr(info,p.read_bytes())
 data=buf.getvalue();check_zip(data);return data
