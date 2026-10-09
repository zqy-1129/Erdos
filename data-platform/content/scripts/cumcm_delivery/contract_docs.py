"""Additive local content API extension; frozen business contracts are never rewritten."""
from . import contracts,core

def main():
    envelope={'type':'object','required':['code','message','request_id','timestamp'],'properties':{'code':{'type':'integer'},'message':{'type':'string'},'data':{},'request_id':{'type':'string'},'timestamp':{'type':'string','format':'date-time'}}}
    response=lambda data:{'description':'Business response envelope; data is '+data,'content':{'application/json':{'schema':{'$ref':'#/components/schemas/Envelope'}}}}
    parameters=[{'name':'release_id','in':'query','required':True,'schema':{'type':'string'}}]
    paths={
      '/health':{'get':{'security':[],'responses':{'200':{'description':'Process/model initialized; cloud_deployed=false, not a database readiness certificate'}}}},
      '/v1/content/competitions/cumcm/bootstrap':{'get':{'description':'Omit release_id only at task start to resolve the active competition release. Pin the returned release_id and manifest for all later retrieval/download calls.','parameters':[dict(parameters[0],required=False)],'responses':{'200':response('bootstrap, including format_profiles and versioned rendering_profiles with canonical SHA256'),'401':response('null'),'400':response('null'),'503':response('null')}}},
      '/v1/content/retrieve':{'post':{'requestBody':{'required':True,'content':{'application/json':{'schema':{'$ref':'#/components/schemas/RetrieveRequest'}}}},'responses':{'200':response('ContextCapsule'),'400':response('null'),'401':response('null'),'403':response('null'),'503':response('null')}}},
      '/v1/content/assets/{asset_id}':{'get':{'parameters':parameters+[{'name':'asset_id','in':'path','required':True,'schema':{'type':'string','pattern':'^asset-[0-9a-f]{64}$'}},{'name':'version','in':'query','required':True,'schema':{'type':'integer','minimum':1}}],
        'responses':{'200':{'description':'Real S3 object bytes, SHA verified before response','headers':{'X-Content-SHA256':{'schema':{'type':'string'}},'X-Asset-Version':{'schema':{'type':'integer'}},'X-Request-ID':{'schema':{'type':'string'}}},'content':{'application/octet-stream':{'schema':{'type':'string','format':'binary'}}}},'400':response('null'),'401':response('null'),'403':response('null'),'503':response('null')}}}}
    def oas30(value):
        if isinstance(value,list) or isinstance(value,tuple):return [oas30(v) for v in value]
        if not isinstance(value,dict):return value
        result={k:oas30(v) for k,v in value.items() if k!='const'}
        if 'const' in value:result['enum']=[value['const']]
        if isinstance(result.get('type'),list):
            types=[t for t in result['type'] if t!='null']
            if len(types)!=1:raise ValueError('payload union needs explicit OAS oneOf')
            result['type']=types[0];result['nullable']='null' in value['type']
        return result
    schemas={k:oas30(v) for k,v in contracts.schemas().items() if k in ('retrieve_request','context_capsule')}
    document={'openapi':'3.0.3','info':{'title':'CUMCM local content API additive extension','version':'3.0.0','description':'Implemented loopback integration adapter. Existing business server routes/JWT/membership integration remain BE work. Request purpose never grants audit privileges.'},'servers':[{'url':'http://127.0.0.1:18789'}], 'security':[{'LocalBearer':[]}],'paths':paths,
              'components':{'securitySchemes':{'LocalBearer':{'type':'http','scheme':'bearer'}},'schemas':{'Envelope':envelope,'RetrieveRequest':schemas['retrieve_request'],'ContextCapsule':schemas['context_capsule']}},
              'x-error-codes':{'BAD_REQUEST':40001,'UNAUTHENTICATED':40101,'PERMISSION_DENIED':40301,'NOT_FOUND':40401,'DB_UNAVAILABLE':50301},'x-frozen-business-contract':'D:/Erdos/contracts/openapi.yaml (unchanged)'}
    core.write_json(core.CONTENT_DIR/'handoff/cumcm/content_api_v3.openapi.json',document)
    print('Additive content API and response envelope contract written')

if __name__=='__main__':main()
