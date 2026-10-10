"""Independent audit regressions: reject malformed input and broken invocation graphs."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(os.path.abspath(__file__)).parents[1]
SPEC=importlib.util.spec_from_file_location('contracts_audit',str(ROOT/'scripts/validate_content_contracts.py'))
v=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(v)

class AuditContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT/'out').mkdir(exist_ok=True)
        _,*vv=v.load_jsonschema();v._v=tuple(vv)
        cls.store,cls.schemas=v.load_store(v.SCHEMAS_DIR)
        cls.taxonomy=json.loads(v.DEFAULT_TAXONOMY.read_text(encoding='utf-8'))
        cls.bundle=json.loads((v.DEFAULT_EXAMPLES/'positive/bundle_ok.json').read_text(encoding='utf-8'))

    def errors(self,doc):return v.validate_bundle(doc,self.store,self.schemas,self.taxonomy)
    def validator(self,name):return v.make_validator(self.schemas[name],self.store,*v._v)
    def cli(self,*args):
        return subprocess.run([sys.executable,'-S',str(ROOT/'scripts/validate_content_contracts.py')]+list(args),cwd=str(ROOT),env=dict(os.environ,PYTHONIOENCODING='utf-8'),capture_output=True,encoding='utf-8')

    def test_graph_errors_include_record_field(self):
        b=copy.deepcopy(self.bundle);b['families'][0]['layout_refs'][0]['asset_ref']['version']=9
        errors=self.errors(b)
        self.assertTrue(any('[ASSET_LOCK]' in e and '/families/0/layout_refs/0' in e for e in errors),errors)

    def test_uri_and_path_boundaries(self):
        a=copy.deepcopy(self.bundle['assets'][0])
        for bad in ['../x','a/../../x','/abs','C:/x','C:x','\\\\host\\x','a\\..\\x','https://x/a?sig=token','a/%2e%2e/x','a\x00b']:
            a['oss_key']=bad
            self.assertTrue(v.schema_error_strings(self.validator('asset'),a),bad)
        a['oss_key']='content/国赛/论文.pdf'
        self.assertEqual(v.schema_error_strings(self.validator('asset'),a),[])

    def test_source_percent_filename_is_raw_not_url(self):
        d=json.loads((v.DEFAULT_EXAMPLES/'positive/source_file.json').read_text(encoding='utf-8'))
        d['relative_path']='CPMCM/12辆车(80%).mat'
        self.assertEqual(v.schema_error_strings(self.validator('source_file'),d),[])

    def test_all_source_failure_statuses_supported(self):
        d=json.loads((v.DEFAULT_EXAMPLES/'positive/source_file_failed.json').read_text(encoding='utf-8'))
        for status in ['failed','unstable','pending']:
            d['status']=status
            self.assertEqual(v.schema_error_strings(self.validator('source_file'),d),[])

    def test_source_ok_cannot_have_unknown_hash(self):
        d=json.loads((v.DEFAULT_EXAMPLES/'positive/source_file_failed.json').read_text(encoding='utf-8'));d['status']='ok'
        self.assertTrue(v.schema_error_strings(self.validator('source_file'),d))

    def test_invalid_timestamp_rejected(self):
        for value in ['2026-99-40T00:00:00Z','2026-10-04T12:00:00+00:99']:
            b=copy.deepcopy(self.bundle);b['release']['created_at']=value
            self.assertTrue(any('date-time' in e for e in self.errors(b)),value)

    def test_unrecognized_graph_collection_rejected(self):
        b=copy.deepcopy(self.bundle);b['fake_families']=[{'anything':1}]
        self.assertTrue(any('[SCHEMA]' in e for e in self.errors(b)))

    def test_unknown_draft_version_is_not_silently_accepted(self):
        b=copy.deepcopy(self.bundle);b['cases'][0]['schema_version']=999
        self.assertTrue(any('/cases/0/schema_version' in e for e in self.errors(b)))

    def test_staging_unresolved_allowed_published_rejected(self):
        b=copy.deepcopy(self.bundle);b['cases'][1].update(problem_id=None,resolution_status='unresolved')
        self.assertEqual(self.errors(b),[])
        b['cases'][1]['publication_status']='published'
        self.assertTrue(any('[UNRESOLVED_PARENT]' in e for e in self.errors(b)))

    def test_asset_repeated_and_missing_owner_rejected(self):
        b=copy.deepcopy(self.bundle);b['assets'].append(copy.deepcopy(b['assets'][0]))
        self.assertTrue(any('[DUPLICATE_ID]' in e for e in self.errors(b)))
        b=copy.deepcopy(self.bundle);b['assets'][0]['owner_id']='missing'
        self.assertTrue(any('[ASSET_OWNER]' in e for e in self.errors(b)))

    def test_taxonomy_duplicate_alias_and_legacy_coverage(self):
        t=copy.deepcopy(self.taxonomy);t['method_tags']['entries'][1]['aliases'].append(t['method_tags']['entries'][0]['aliases'][0])
        self.assertTrue(any('[TAXONOMY_ALIAS]' in e for e in v.semantics.taxonomy_issues(t)))
        self.assertTrue(any('[METHOD_DICTIONARY]' in e for e in v.semantics.taxonomy_issues(self.taxonomy,'- tag: invented')))

    def test_release_role_and_removed_item_consistency(self):
        b=copy.deepcopy(self.bundle);b['release']['items'][0]['role']='template_layout'
        self.assertTrue(any('[ASSET_ROLE]' in e for e in self.errors(b)))
        b=copy.deepcopy(self.bundle);b['release']['removed']=[b['release']['items'][0]['asset_id']]
        self.assertTrue(any('[REMOVAL_STATE]' in e for e in self.errors(b)))

    def test_schema_refs_cannot_access_network(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT/'out')) as temp:
            schema={'$schema':'http://json-schema.org/draft-07/schema#','$id':'https://local.test/a','$ref':'https://remote.test/forbidden'}
            (Path(temp)/'a.json').write_text(json.dumps(schema),encoding='utf-8')
            with self.assertRaises(ValueError):v.load_store(Path(temp))

    def test_json_nonstandard_numbers_and_duplicate_keys(self):
        for data in ['{"value":NaN}','{"value":Infinity}','{"a":1,"a":2}']:
            with self.assertRaises(ValueError):v.parse_json(data)

    def test_stream_errors_report_line_and_do_not_crash(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT/'out')) as temp:
            path=Path(temp)/'sources.jsonl';path.write_text('[]\nnot JSON\n',encoding='utf-8')
            issues,total,ok=v.check_sources(path,self.store,self.schemas)
            self.assertEqual((total,ok),(2,0));self.assertTrue(any('第1行' in e for e in issues));self.assertTrue(any('第2行' in e for e in issues))
            r=self.cli('--check-sources',str(path));self.assertEqual(r.returncode,1,r.stdout+r.stderr);self.assertNotIn('Traceback',r.stderr)

    def test_cli_bundle_rejects_real_semantic_error(self):
        good=self.cli('--check-bundle',str(v.DEFAULT_EXAMPLES/'positive/bundle_ok.json'))
        bad=self.cli('--check-bundle',str(v.DEFAULT_EXAMPLES/'negative/bundle_layout_version.json'))
        self.assertEqual(good.returncode,0,good.stdout+good.stderr);self.assertEqual(bad.returncode,1,bad.stdout+bad.stderr)
        self.assertIn('[ASSET_LOCK]',bad.stdout)

    def test_cli_missing_dependencies_and_input(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT/'out')) as temp:
            r=self.cli('--deps',temp,'--check-schemas');self.assertEqual(r.returncode,2,r.stdout+r.stderr)
        r=self.cli('--check-bundle',str(ROOT/'out/does-not-exist.json'));self.assertEqual(r.returncode,1,r.stdout+r.stderr)
        self.assertNotIn('Traceback',r.stderr)

    def test_actual_schema_optional_and_required_evolution(self):
        schema=copy.deepcopy(self.schemas['asset']);old=copy.deepcopy(self.bundle['assets'][0])
        schema['properties']['new_optional']={'type':'string'}
        self.assertEqual(v.schema_error_strings(v.make_validator(schema,self.store,*v._v),old),[])
        schema['required'].append('new_optional')
        self.assertTrue(v.schema_error_strings(v.make_validator(schema,self.store,*v._v),old))

if __name__=='__main__':unittest.main()
