"""Additive local request contract. Existing v3 consumer rights are unchanged."""
import copy
from . import core,contracts

def write_schema():
    schema=copy.deepcopy(contracts.schemas()['retrieve_request'])
    schema['properties']['usage_purpose']={'enum':['consumer','internal_audit','third_party_model','team_internal']}
    schema['$schema']='http://json-schema.org/draft-07/schema#'
    schema['description']='team_internal requires dedicated authenticated loopback role and pinned-release local usage policy; never grants distribution or remote model use'
    core.write_json(core.CONTENT_DIR/'schemas/cumcm/v4/local_retrieve_request.schema.json',schema)

if __name__=='__main__':write_schema()
