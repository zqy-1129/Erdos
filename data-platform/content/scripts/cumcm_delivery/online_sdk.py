"""Standalone stdlib client for the local content API; no DB, S3 or model dependency."""
import hashlib,json,re,urllib.error,urllib.request
from urllib.parse import urlsplit
from ipaddress import IPv4Address,IPv4Network


def validate_host(host,allow_lan=False):
    if host=='127.0.0.1':return
    if allow_lan:
        try:
            ip=IPv4Address(host)
            if any(ip in IPv4Network(net) for net in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):return
        except ValueError:pass
    raise ValueError('explicit loopback or authorized RFC1918 lab IPv4 address required')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('content SDK rejects redirects; credentials stay at the configured endpoint')


class ContentClient:
    def __init__(self,token,base_url='http://127.0.0.1:18789',allow_lan=False):
        url=urlsplit(base_url)
        if url.scheme!='http' or url.username or url.password or url.path or url.query or url.fragment or not url.port:
            raise ValueError('local SDK requires an explicit HTTP host/port and no URL credentials or path')
        validate_host(url.hostname,allow_lan)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        if not isinstance(token,str) or not token:raise ValueError('caller credential required')
        self.url=base_url;self.token=token;self.pin=None

    def request(self,path,value=None):
        body=None if value is None else json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'}
        req=urllib.request.Request(self.url+path,data=body,headers=headers)
        with self.opener.open(req,timeout=120) as response:
            wire=json.loads(response.read().decode('utf-8'))
        if wire.get('code')!=0:raise ValueError('content API did not return success')
        return wire['data']

    def bootstrap(self,release_id=None):
        if release_id is not None and not re.fullmatch('cumcm-2010-2025-[a-z0-9-]+',release_id):raise ValueError('invalid release')
        value=self.request('/v1/content/competitions/cumcm/bootstrap'+('?release_id='+release_id if release_id else ''))
        if release_id and value['release_id']!=release_id:raise ValueError('bootstrap release mismatch')
        if not re.fullmatch('[0-9a-f]{64}',value['manifest_sha256']):raise ValueError('invalid manifest pin')
        self.pin={'release_id':value['release_id'],'manifest_sha256':value['manifest_sha256']}
        return value

    def _check(self,value):
        if not self.pin:raise ValueError('bootstrap once and pin release before use')
        if any(value[k]!=v for k,v in self.pin.items()):raise ValueError('response differs from pinned release')
        return value

    def retrieve(self,value):
        if not self.pin:raise ValueError('bootstrap required')
        request=dict(value)
        if request.get('release_id',self.pin['release_id'])!=self.pin['release_id']:raise ValueError('request release mismatch')
        request['release_id']=self.pin['release_id'];request['competition_id']='cumcm'
        return self._check(self.request('/v1/content/retrieve',request))

    def components(self,source_sha256,page):
        if not self.pin:raise ValueError('bootstrap required')
        value=self.request('/v1/content/components',{'release_id':self.pin['release_id'],'source_sha256':source_sha256,'page':page,'usage_purpose':'team_internal'})
        return self._check(value)

    def fetch(self,asset_id,version,expected_sha256,max_bytes=128*1024**2):
        if not self.pin:raise ValueError('bootstrap required')
        if asset_id!='asset-'+expected_sha256 or not re.fullmatch('[0-9a-f]{64}',expected_sha256) or type(version)!=int or version<1:
            raise ValueError('exact asset/version/SHA required')
        path='/v1/content/assets/'+asset_id+'?release_id='+self.pin['release_id']+'&version='+str(version)
        request=urllib.request.Request(self.url+path,headers={'Authorization':'Bearer '+self.token})
        with self.opener.open(request,timeout=180) as response:
            data=response.read(max_bytes+1)
            if response.headers.get('X-Content-SHA256')!=expected_sha256 or response.headers.get('X-Asset-Version')!=str(version):
                raise ValueError('asset response pin mismatch')
        if len(data)>max_bytes or hashlib.sha256(data).hexdigest()!=expected_sha256:raise ValueError('asset bytes checksum or size mismatch')
        return data
