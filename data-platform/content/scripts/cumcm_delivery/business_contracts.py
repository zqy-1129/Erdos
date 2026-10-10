"""Explicit bounded business contracts; independent of Python 3.12 server imports."""
from . import core,audit

SHA={'type':'string','pattern':'^[0-9a-f]{64}$'}
ID={'type':'string','minLength':1,'maxLength':160}
TEXT={'type':'string'}
def string(limit,minimum=0):return {'type':'string','minLength':minimum,'maxLength':limit}
def array(item):return {'type':'array','items':item}
def obj(fields,required=None):return {'type':'object','properties':fields,'required':list(fields) if required is None else required,'additionalProperties':False}
REF=obj({'asset_id':{'type':'string','pattern':'^asset-[0-9a-f]{64}$'},'version':{'type':'integer','minimum':1},'sha256':SHA})
BOX={'type':'array','minItems':4,'maxItems':4,'items':{'type':'number'}}
PAGE={'type':'integer','minimum':1}
BOOL={'type':'boolean'}
STATUS={'enum':['candidate','source_checked','verified','rejected','abstained']}
_VALIDATORS={}

def schemas():
    legacy_problem=obj({'business_id':string(64,1),'competition':{'const':'cumcm'},'year':{'type':'integer','minimum':2010,'maximum':2025},
        'problem_code':{'enum':['A','B','C','D','E']},'title':string(128,1),'tags':array(string(64,1)),
        'prompt_zh':string(8192,1),'prompt_en':string(8192),
        'attachments':array(obj({'name':string(256,1),'oss_key':string(256,1),'sha256':SHA,'media_type':string(128,1)})),
        'scoring':string(2048),'dataset_hint':string(2048),'visibility':{'enum':['public','member']}})
    legacy_template=obj({'business_id':string(64,1),'competition':{'const':'cumcm'},'format':{'enum':['typst','latex','docx']},
        'oss_key':string(256,1),'sha256':SHA,'version':{'type':'integer','minimum':1},'changelog':string(512),'tier':{'enum':['free','member_only']}})
    legacy_case=obj({'business_id':string(64,1),'problem_id':string(64,1),'title':string(128,1),'award':string(32),
        'method_tags':array(string(64,1)),'oss_key':string(256,1),'sha256':SHA,'compliance_note':string(256,1)})
    evidence=obj({'source_sha256':SHA,'page':PAGE,'quote':string(8000),'block_ids':array(ID)},['source_sha256','page','quote'])
    source=obj({'source_id':ID,'sha256':SHA,'year':{'type':['integer','null'],'minimum':2010,'maximum':2025},'role':{'enum':['problem','paper','attachment','template']},
        'asset_ref':REF,'media_type':string(128,1),'original_occurrences':array(string(2048,1))})
    problem=obj({'problem_id':ID,'year':{'type':'integer','minimum':2010,'maximum':2025},'code':{'enum':['A','B','C','D','E']},
        'title':string(1024,1),'statement_refs':array(REF),'attachment_refs':array(REF),'type_tags':array(ID),'classification_status':STATUS})
    subproblem=obj({'subproblem_id':ID,'problem_id':ID,'ordinal':PAGE,'goal':string(24000,1),'constraints':array(string(4000)),
        'deliverables':array(string(4000)),'dependencies':array(ID),'evidence':evidence,'semantic_status':STATUS})
    paper=obj({'case_id':ID,'source_sha256':SHA,'year':{'type':'integer','minimum':2010,'maximum':2025},'title':string(1024,1),
        'page_count':PAGE,'problem_id':{'type':['string','null']},'relation_status':STATUS,'award':{'type':['string','null']},
        'award_status':STATUS,'method_mentions':array(ID),'method_status':STATUS,'compliance_note':string(256,1)})
    section=obj({'section_id':ID,'case_id':ID,'parent_id':{'type':['string','null']},'level':PAGE,'title':string(1024,1),
        'start_block':ID,'end_block':ID,'block_ids':array(ID),'recognized_chars':{'type':'integer','minimum':0},'stage':ID,
        'subproblem_refs':array(ID),'semantic_status':STATUS})
    block=obj({'block_id':ID,'source_sha256':SHA,'page':PAGE,'order':PAGE,'text':TEXT,'bbox':BOX,'coordinates':{'const':'pdf_points_bottom_left'},
        'extraction_method':{'enum':['pdf_text','ocr']},'confidence':{'type':['number','null'],'minimum':0,'maximum':1},'semantic_status':STATUS})
    visual=obj({'component_id':ID,'kind':{'enum':['figure','table','formula']},'source_sha256':SHA,'page':PAGE,'anchor_block_id':ID,'anchor_text':TEXT,
        'anchor_bbox':BOX,'coordinate_system':{'const':'pdf_points_bottom_left'},'asset_ref':REF,'evidence_scope':{'enum':['complete_page','isolated_region']},
        'includes_neighbor_content':BOOL,'isolation_status':TEXT,'semantic_status':{'enum':['pending_source_review','candidate','source_checked','verified','rejected','abstained']},
        'latex':{'type':['string','null']},'data_reconstructed':{'const':False}})
    review=obj({'review_id':ID,'kind':ID,'source_sha256':SHA,'page':PAGE,'input_sha256':SHA,'reviewer_type':{'enum':['codex_source_visual_review','local_model_candidate_not_human_ground_truth','human']},
        'status':STATUS,'finding':string(16000,1),'ground_truth':{'type':['string','object','null']},'model_revision':{'type':['string','null']}})
    formula=obj({'formula_region_id':ID,'source_sha256':SHA,'page':PAGE,'bbox':BOX,
        'coordinate_system':{'const':'pdf_points_bottom_left'},'region_type':{'enum':['embedding','isolated']},
        'detector_score':{'type':'number','minimum':0,'maximum':1},'source_page_ref':REF,
        'crop_ref':{'anyOf':[REF,{'type':'null'}]},'crop_path':{'type':['string','null']},
        'latex_candidate':{'type':['string','null']},'semantic_status':STATUS,
        'transcription_status':{'enum':['model_candidate','truncated','not_transcribed_inline_symbol','not_transcribed_display_formula','source_checked']},
        'generation_truncated':{'type':['boolean','null']},
        'geometric_mean_token_probability':{'type':['number','null'],'minimum':0,'maximum':1},
        'semantics_verified':{'const':False},'transcription_verified':BOOL,'model_revisions':obj({'mfd':string(40,40),'mfr':string(40,40)}),
        'source_is_instruction':{'const':False},'current_result_value_source':{'const':False},
        'source_review_id':ID,'source_checked_latex':TEXT},
        ['formula_region_id','source_sha256','page','bbox','coordinate_system','region_type','detector_score',
         'source_page_ref','crop_ref','crop_path','latex_candidate','semantic_status','transcription_status',
         'generation_truncated','geometric_mean_token_probability','semantics_verified','model_revisions',
         'source_is_instruction','current_result_value_source'])
    return {'legacy_problem':legacy_problem,'legacy_template':legacy_template,'legacy_case':legacy_case,'source':source,
        'problem':problem,'subproblem':subproblem,'paper':paper,'section':section,'block':block,'visual':visual,'review':review,'formula_region':formula}

def validate(kind,value):
    if kind not in _VALIDATORS:
        audit.deps();from validate_content_contracts import load_jsonschema
        _,validator,_,_=load_jsonschema();_VALIDATORS[kind]=validator(schemas()[kind])
    _VALIDATORS[kind].validate(value)
    if kind in ('block','visual','formula_region'):
        box=value.get('bbox',value.get('anchor_bbox'))
        # PDF content may extend outside the crop box; preserve its coordinates.
        # A valid rectangle is not a claim that the glyph is visible on the page.
        if box[2]<=box[0] or box[3]<=box[1]:raise ValueError('invalid PDF bounding box')
    if kind=='visual' and value['evidence_scope']=='complete_page' and not value['includes_neighbor_content']:
        raise ValueError('complete-page evidence necessarily includes neighboring content')
    if kind=='subproblem' and value['subproblem_id'] in value['dependencies']:raise ValueError('self-dependent task')
    if kind=='formula_region':
        if bool(value['crop_ref'])!=bool(value['crop_path']):raise ValueError('formula crop ref/path mismatch')
        if value.get('transcription_verified') and not (value.get('source_review_id') and value.get('source_checked_latex') and value['semantic_status']=='source_checked'):
            raise ValueError('verified formula needs independent source inspection')

def write_schemas():
    for kind,value in schemas().items():
        value=dict(value,**{'$schema':'http://json-schema.org/draft-07/schema#'})
        core.write_json(core.CONTENT_DIR/'schemas/cumcm/v4/business'/(kind+'.schema.json'),value)

if __name__=='__main__':write_schemas()
