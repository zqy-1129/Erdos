"""Generate private loopback service config without printing credentials."""
import json, secrets, argparse
from . import core

def create(pg_port=55433,s3_port=18333,api_port=18789):
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in (pg_port,s3_port,api_port)):
        raise ValueError('ports must be integers in 1024..65535')
    if len({pg_port,s3_port,api_port})!=3:raise ValueError('ports must differ')
    directory=core.CONTENT_DIR/'.runtime/local'
    directory.mkdir(parents=True,exist_ok=True)
    if (directory/'services.json').exists():
        raise ValueError('configuration already exists; never rotate working credentials automatically')
    key,secret,password=secrets.token_hex(16),secrets.token_urlsafe(40),secrets.token_urlsafe(32)
    cfg={'environment':'local-audit','postgres_container':'erdos-cumcm-postgres-1',
         'postgres':{'host':'127.0.0.1','port':pg_port,'dbname':'cumcm_codex','user':'cumcm','password':password},
         's3':{'endpoint_url':'http://127.0.0.1:'+str(s3_port),'bucket':'cumcm-private','aws_access_key_id':key,'aws_secret_access_key':secret,'region_name':'us-east-1'},
         'embedding':{'model':'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2','revision':'faf4aa4225822f3bc6376869cb1164e8e3feedd0','dimensions':384,'provider':'CUDAExecutionProvider'},
         'query_embedding_provider':'CPUExecutionProvider',
         'api':{'host':'127.0.0.1','port':api_port,'consumer_token':secrets.token_urlsafe(40),'audit_token':secrets.token_urlsafe(40),'team_token':secrets.token_urlsafe(40)}}
    core.write_json(directory/'s3.json',{'identities':[{'name':'cumcm-local','credentials':[{'accessKey':key,'secretKey':secret}],'actions':['Admin','Read','Write','List','Tagging']}]})
    core.write_bytes(directory/'postgres.env',('POSTGRES_DB=cumcm_codex\nPOSTGRES_USER=cumcm\nPOSTGRES_PASSWORD='+password+'\n').encode())
    core.write_bytes(directory/'compose.env',('CUMCM_PG_PORT='+str(pg_port)+'\nCUMCM_S3_PORT='+str(s3_port)+'\n').encode())
    core.write_json(directory/'services.json',cfg)
    print('Created private loopback configuration; credentials omitted')

def main():
    p=argparse.ArgumentParser();p.add_argument('--pg-port',type=int,default=55433);p.add_argument('--s3-port',type=int,default=18333);p.add_argument('--api-port',type=int,default=18789)
    a=p.parse_args();create(a.pg_port,a.s3_port,a.api_port)

if __name__=='__main__':main()
