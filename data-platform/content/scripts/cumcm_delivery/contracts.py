"""v3 schemas are additive; existing v1/v2 contracts remain unchanged."""
from . import core,consumer,audit
ID={'type':'string','minLength':1}
SHA={'type':'string','pattern':'^[0-9a-f]{64}$'}
def obj(properties,required=None):
    return {'type':'object','properties':properties,'required':list(properties) if required is None else required,'additionalProperties':False}
def array(items):return {'type':'array','items':items}
def schemas():
    asset=obj({'asset_id':{'type':'string','pattern':'^asset-[0-9a-f]{64}$'},'version':{'type':'integer','minimum':1},'sha256':SHA})
    subfields={'subproblem_id':ID,'goal':{'type':'string','minLength':1,'maxLength':8000},'problem_types':array({'enum':consumer.TYPES}),
               'constraints':array({'type':'string'}),'deliverables':array({'type':'string'}),'method_tags':array(ID),'data_tags':array(ID),'domain_tags':array(ID),
               'dependency_refs':array(ID),'current_results':array({'type':'object'})}
    request=obj({'request_id':ID,'competition_id':{'const':'cumcm'},'release_id':{'type':'string','pattern':'^cumcm-2010-2025-[a-z0-9-]+$'},
                 'historical_year_range':{'type':'array','minItems':2,'maxItems':2,'items':{'type':'integer','minimum':2010,'maximum':2025}},
                 'subproblem':obj(subfields,['goal']),'stage':{'enum':consumer.STAGES},'language':{'enum':['zh']},
                 'top_k':{'type':'integer','minimum':1,'maximum':20},'token_budget':{'type':'integer','minimum':256,'maximum':16000},
                 'usage_purpose':{'enum':['consumer','internal_audit','third_party_model']}},['competition_id','release_id','subproblem','stage'])
    capsule=obj({'request_id':{'type':['string','null']},'competition_id':{'const':'cumcm'},'release_id':ID,'manifest_sha256':SHA,'stage':{'enum':consumer.STAGES},
                 'subproblem_id':{'type':['string','null']},'status':{'enum':['ok','no_reference','internal_candidates','recipe_only']},
                 'historical_references':array({'type':'object','required':['unit_id','source_sha256','source_ref','page','text'],'properties':{'source_ref':dict(asset,properties=dict(asset['properties'],page={'type':'integer','minimum':1}),required=['asset_id','version','sha256','page'])}}),
                 'writing_recipes':array({'type':'object'}),'figure_recipes':array({'type':'object'}),'missing':array({'type':'string'}),'internal_candidates':{'type':'boolean'},
                 'budget':obj({'limit':{'type':'integer','minimum':256},'actual':{'type':'integer','minimum':0},'tokenizer':ID,'includes_response_json':{'const':True}}),
                 'source_text_is_instruction':{'const':False},'result_numbers':{'const':'current_task_computation_only'}})
    seal=obj({'manifest_sha256':SHA,'sealed_at':ID})
    manifest=obj({'competition_id':{'const':'cumcm'},'release_id':ID,'version':{'const':3},'files':array(obj({'path':ID,'sha256':SHA,'size':{'type':'integer','minimum':0}}))})
    result=obj({'result_card_id':ID,'task_id':ID,'subproblem_id':ID,'status':{'enum':['planned','running','succeeded','failed','reviewed']},
                'input_refs':array(asset),'run':obj({'run_id':ID,'code_sha256':SHA,'environment_lock_sha256':SHA,'started_at':ID,'finished_at':{'type':['string','null']}},['run_id','code_sha256','environment_lock_sha256','started_at']),
                'method':ID,'outputs':array(obj({'name':ID,'value':{'type':['number','string','array','object']},'unit':ID})),
                'artifact_refs':array(asset),'validation':obj({'status':{'enum':['not_run','passed','failed']},'checks':array(ID)}),'limitations':array(ID),'schema_version':{'const':3}})
    node=obj({'node_id':ID,'task_id':ID,'parent_node_id':{'type':['string','null']},'order':{'type':'integer','minimum':1},
              'node_type':{'enum':['heading','paragraph','equation','figure','table','reference','appendix']},'subproblem_ref':{'type':['string','null']},
              'content':obj({'text':{'type':'string'},'latex':{'type':'string'},'asset_ref':asset,'caption':{'type':'string'},'units':array(ID)},[]),
              'result_refs':array(ID),'release_id':ID,'version':{'type':'integer','minimum':1}},['node_id','task_id','order','node_type','content','result_refs','release_id','version'])
    return {'retrieve_request':request,'context_capsule':capsule,'asset_ref':asset,'manifest':manifest,'seal':seal,'result_card':result,'document_node':node}

def write_schemas():
    root=core.CONTENT_DIR/'schemas/cumcm/v3'
    for name,schema in schemas().items():
        schema=dict(schema,**{'$schema':'http://json-schema.org/draft-07/schema#','$id':'https://erdos.local/content/schemas/cumcm/v3/'+name+'.schema.json'})
        core.write_json(root/(name+'.schema.json'),schema)
    print('v3 schemas written; v1/v2 retained')

def validate(kind,data):
    audit.deps()
    from validate_content_contracts import load_jsonschema
    _,validator,_,_=load_jsonschema()
    validator(schemas()[kind]).validate(data)

def validate_document(nodes,cards):
    """Consumer-side example guard; does not implement a user-task business store."""
    results={c['result_card_id']:c for c in cards}
    if len(results)!=len(cards):raise ValueError('duplicate result_card_id')
    ids={n['node_id'] for n in nodes}
    if len(ids)!=len(nodes):raise ValueError('duplicate document node')
    for card in cards:
        validate('result_card',card)
        if card['status'] in ('succeeded','reviewed') and (not card['outputs'] or not card['run'].get('finished_at')):
            raise ValueError('completed computation requires outputs and finish time')
    for node in nodes:
        validate('document_node',node)
        if node.get('parent_node_id') and node['parent_node_id'] not in ids:raise ValueError('missing parent node')
        if node['node_type'] in ('figure','table') and 'asset_ref' not in node['content']:raise ValueError('figure/table needs a current asset')
        for ref in node['result_refs']:
            card=results.get(ref)
            if not card or card['task_id']!=node['task_id'] or card['status'] not in ('succeeded','reviewed'):
                raise ValueError('result reference must point to completed computation in this task')
    parents={n['node_id']:n.get('parent_node_id') for n in nodes}
    for nid in parents:
        seen=set();current=nid
        while current:
            if current in seen:raise ValueError('document parent cycle')
            seen.add(current);current=parents[current]

if __name__=='__main__':write_schemas()
