"""Verified local file operations for the small sample; refuse links and overwrites."""
import hashlib
import os
import stat
import tempfile
from pathlib import Path

CHUNK_SIZE=1024*1024

def no_links(path):
    path=Path(os.path.abspath(str(path)))
    for component in [*reversed(path.parents),path]:
        try:st=os.lstat(str(component))
        except FileNotFoundError:continue
        if stat.S_ISLNK(st.st_mode) or getattr(st,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400):
            raise ValueError('linked/reparse path rejected: '+str(component))
    return path

def relative(root,rel):
    if not isinstance(rel,str) or not rel or '\\' in rel or ':' in rel or '\x00' in rel:
        raise ValueError('invalid relative path: '+str(rel))
    parts=rel.split('/')
    if any(p in ['','..','.'] for p in parts):raise ValueError('path traversal/absolute path: '+rel)
    base=no_links(root);target=no_links(base.joinpath(*parts))
    if os.path.commonpath([str(base),str(target)])!=str(base):raise ValueError('path escapes root')
    return target

def stat_file(path):
    no_links(path);st=os.lstat(str(path))
    if not stat.S_ISREG(st.st_mode):raise ValueError('not a regular file: '+str(path))
    return {'size':int(st.st_size),'mtime_ns':int(st.st_mtime_ns),'file_id':[int(st.st_dev),int(st.st_ino)]}

def sha256_of(path):
    digest=hashlib.sha256()
    with open(path,'rb') as stream:
        for chunk in iter(lambda:stream.read(CHUNK_SIZE),b''):digest.update(chunk)
    return digest.hexdigest()

def hash_verified(path):
    try:
        before=stat_file(path)
        if before['size']==0:return {'sha256':None,'size':0,'status':'empty','note':'empty file unavailable'}
        digest=sha256_of(path);after=stat_file(path)
        if before!=after:return {'sha256':None,'size':None,'status':'unstable','note':'file changed while reading'}
        return {'sha256':digest,'size':before['size'],'status':'ok','note':'','signature':before}
    except FileNotFoundError as exc:return {'sha256':None,'size':None,'status':'missing','note':str(exc)}
    except (OSError,ValueError) as exc:return {'sha256':None,'size':None,'status':'failed','note':str(exc)}

def write_exact(path,data):
    """Existing business bytes are immutable; a changed result needs a fresh output snapshot."""
    path=no_links(path)
    if path.exists():
        if path.read_bytes()!=data:raise ValueError('output conflict, preserved existing file: '+str(path))
        return 'reused'
    path.parent.mkdir(parents=True,exist_ok=True)
    with open(path,'xb') as stream:stream.write(data)
    return 'created'

def copy_verified(src,dst):
    src=Path(src);dst=Path(dst)
    info=hash_verified(src)
    result={'src':str(src),'dst':str(dst),'src_sha256':info['sha256'],'size':info['size'],'status':info['status'],'action':'source_unavailable','copy_sha256':None,'conflict':info['status']!='ok','note':info['note']}
    if result['conflict']:return result
    temporary=None
    try:
        no_links(dst)
        if dst.exists():
            existing=hash_verified(dst);result['copy_sha256']=existing['sha256']
            if existing['status']=='ok' and existing['sha256']==info['sha256']:
                # Check source again; a previously stable hash does not cover this later interval.
                after=hash_verified(src)
                if after.get('signature')!=info.get('signature') or after['sha256']!=info['sha256']:raise ValueError('source changed during reuse')
                result['action']='reused';return result
            result.update(action='conflict',conflict=True,note='existing copy differs; never overwritten');return result
        dst.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.stage03-',suffix='.part',dir=str(dst.parent));temporary=Path(name)
        with os.fdopen(fd,'wb') as target,open(src,'rb') as source:
            for chunk in iter(lambda:source.read(CHUNK_SIZE),b''):target.write(chunk)
        copied=hash_verified(temporary);after=hash_verified(src)
        if copied['sha256']!=info['sha256'] or after['sha256']!=info['sha256'] or after.get('signature')!=info.get('signature'):
            raise ValueError('source/copy changed during copying')
        # Exclusive create makes a concurrently created destination a conflict.
        with open(dst,'xb') as target,open(temporary,'rb') as source:
            for chunk in iter(lambda:source.read(CHUNK_SIZE),b''):target.write(chunk)
        final=hash_verified(dst)
        if final['sha256']!=info['sha256']:raise ValueError('published copy hash differs')
        result.update(action='copied',copy_sha256=final['sha256']);return result
    except (OSError,ValueError) as exc:
        result.update(action='conflict',conflict=True,note=str(exc));return result
    finally:
        if temporary is not None and temporary.is_file():temporary.unlink()
