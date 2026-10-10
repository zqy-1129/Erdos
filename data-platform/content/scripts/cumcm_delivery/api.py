"""Loopback integration API; cloud gateway must replace local bearer authentication."""
import asyncio,hmac,json,secrets,sys,uuid,re
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse,parse_qs
from . import core,services,consumer

class Unauthenticated(PermissionError):pass

def configure():
    path=services.RUNTIME/'local/services.json'
    cfg=core.load_json(path)
    if 'api' not in cfg:
        cfg['api']={'host':'127.0.0.1','port':18789,'consumer_token':secrets.token_urlsafe(40),'audit_token':secrets.token_urlsafe(40)}
        core.write_json(path,cfg)
    if 'team_token' not in cfg['api']:
        cfg['api']['team_token']=secrets.token_urlsafe(40)
        core.write_json(path,cfg)
    return cfg['api']

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def role(self):
        authorization=self.headers.get('Authorization','')
        token=authorization[7:] if authorization.startswith('Bearer ') else ''
        cfg=services.configuration()['api']
        if hmac.compare_digest(token,cfg['audit_token']):return 'internal_audit'
        if hmac.compare_digest(token,cfg['consumer_token']):return 'consumer'
        if cfg.get('team_token') and hmac.compare_digest(token,cfg['team_token']):return 'team_internal'
        raise Unauthenticated('valid local bearer credential required')
    def respond(self,code,value,content_type='application/json; charset=utf-8',headers=None):
        request_id=self.headers.get('X-Request-ID','')
        if not request_id and isinstance(value,dict):request_id=value.get('request_id') or ''
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}',request_id):request_id='content-'+uuid.uuid4().hex
        if not isinstance(value,bytes) and urlparse(self.path).path!='/health':
            value={'code':0 if code<400 else {400:40001,401:40101,403:40301,404:40401,503:50301}.get(code,50001),
                   'message':'ok' if code<400 else value.get('error','request failed'),'data':value if code<400 else None,
                   'request_id':request_id,'timestamp':core.now_utc_iso()}
        data=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
        self.send_response(code);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(data)))
        self.send_header('X-Request-ID',request_id)
        self.send_header('Cache-Control','private, no-store')
        for key,value in (headers or {}).items():self.send_header(key,str(value))
        self.end_headers();self.wfile.write(data)
    def do_GET(self):
        try:
            url=urlparse(self.path)
            if url.path=='/health':
                self.respond(200,{'status':'up','environment':'local-integration','cloud_deployed':False});return
            role=self.role();query=parse_qs(url.query,strict_parsing=True,keep_blank_values=True) if url.query else {}
            if any(len(v)!=1 for v in query.values()):raise ValueError('duplicate query parameter')
            release=query.get('release_id',[None])[0]
            if url.path=='/v1/content/competitions/cumcm/bootstrap':
                if set(query)-{'release_id'}:raise ValueError('unsupported bootstrap query')
                self.respond(200,consumer.bootstrap(release,role));return
            prefix='/v1/content/assets/'
            if url.path.startswith(prefix):
                if set(query)!={'release_id','version'}:raise ValueError('release_id and version required')
                asset=url.path[len(prefix):]
                result=asyncio.run(consumer.fetch_async(release,asset,int(query['version'][0]),trusted_role=role))
                self.respond(200,result['bytes'],'application/octet-stream',{'X-Content-SHA256':result['sha256'],'X-Asset-Version':result['version']});return
            self.respond(404,{'error':'route_not_found'})
        except Unauthenticated as exc:self.respond(401,{'error':str(exc)})
        except PermissionError as exc:self.respond(403,{'error':str(exc)})
        except (ValueError,KeyError) as exc:self.respond(400,{'error':str(exc)})
        except Exception:self.respond(503,{'error':'local_dependency_unavailable'})
    def do_POST(self):
        try:
            role=self.role()
            if self.path not in ('/v1/content/retrieve','/v1/content/components'):self.respond(404,{'error':'route_not_found'});return
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('application/json required')
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=1024*1024:raise ValueError('request body limit')
            value=core.json_loads(self.rfile.read(length).decode('utf-8'))
            if self.path=='/v1/content/components':
                from . import source_components
                self.respond(200,source_components.get(value,role))
            else:self.respond(200,consumer.retrieve(value,role))
        except Unauthenticated as exc:self.respond(401,{'error':str(exc)})
        except PermissionError as exc:self.respond(403,{'error':str(exc)})
        except (ValueError,KeyError) as exc:self.respond(400,{'error':str(exc)})
        except Exception:self.respond(503,{'error':'local_dependency_unavailable'})
    def log_message(self,format,*args):
        # No authorization or full request/query bodies in logs.
        sys.stderr.write('content API status=%s\n'%(args[1] if len(args)>1 else 'unknown'))

def main(lan_host=None):
    cfg=configure()
    from .online_sdk import validate_host
    host=lan_host or cfg['host']
    validate_host(host,allow_lan=lan_host is not None)
    services.model(query=True)  # Warm the actual pinned runtime before accepting client requests.
    print('Content API listening on %s:%s; credentials omitted'%(host,cfg['port']),flush=True)
    ThreadingHTTPServer((host,cfg['port']),Handler).serve_forever()

if __name__=='__main__':main()
