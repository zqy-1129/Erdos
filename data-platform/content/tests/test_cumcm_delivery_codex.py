"""Independent negative regressions for path, JSON, release and consumer boundary failures."""
import json,os,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(os.path.abspath(__file__)).parent.parent
sys.path.insert(0,str(ROOT/'scripts'))
from cumcm_delivery import core,consumer,audit

class DeliveryRepairTests(unittest.TestCase):
    def test_json_duplicate_and_nonfinite_rejected(self):
        for value in ['{"a":1,"a":2}','{"a":NaN}','{"a":Infinity}']:
            with self.assertRaises(ValueError):core.json_loads(value)

    def test_windows_escape_paths_rejected(self):
        for value in ['../x','/x','C:/x','a\\b','CON.txt','a/NUL','a.','a ','a//b','a/../b','a\x01b','a:stream','x?y']:
            with self.subTest(value=value),self.assertRaises(ValueError):core.safe_relpath(ROOT,value)

    def test_old_release_and_original_data_write_rejected(self):
        for path in ['D:/Erdos_data/test.txt',str(ROOT/'out/cumcm_delivery/releases/cumcm-2010-2025-v1/corrupt.json'),str(ROOT/'normalized/stage04/cumcm_codex_v4/corrupt.json')]:
            with self.subTest(path=path),self.assertRaises(ValueError):core.write_json(path,{'bad':True})

    def test_seal_is_immutable_and_extra_file_detected(self):
        (ROOT/'tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='release-test-',dir=str(ROOT/'tmp')) as directory:
            base=Path(directory)/'cumcm-2010-2025-test'
            core.write_json(base/'entry.json',{'value':1})
            manifest={'competition_id':'cumcm','release_id':base.name,'version':3,'files':[{'path':'entry.json','sha256':core.sha256_of(base/'entry.json'),'size':(base/'entry.json').stat().st_size}]}
            core.write_json(base/'manifest.json',manifest)
            core.write_json(base/'SEALED.json',{'manifest_sha256':core.sha256_of(base/'manifest.json'),'sealed_at':core.now_utc_iso()})
            audit.validate_release(base)
            with self.assertRaises(ValueError):core.write_json(base/'entry.json',{'value':2})
            (base/'extra.txt').write_text('untracked')
            with self.assertRaises(ValueError):audit.validate_release(base)

    def test_input_scope_and_bool_integer_rejected(self):
        base={'competition_id':'cumcm','release_id':'cumcm-2010-2025-test','subproblem':{'goal':'建立优化模型','problem_types':['optimization']},'stage':'model'}
        consumer.validate_request(base)
        for key,value in [('competition_id','mcm'),('historical_year_range',[2009,2025]),('historical_year_range',[2025,2010]),('top_k',True),('token_budget',float('nan')),('stage','garbage')]:
            request=dict(base);request[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):consumer.validate_request(request)

    def test_filename_timestamp_does_not_change_parent_year(self):
        from cumcm_delivery.inventory import detect_year
        self.assertEqual(detect_year('2012年优秀论文/20220719A题.pdf','paper')[0],2012)

    def test_figure_recipe_sensitivity_is_not_problem_type(self):
        from cumcm_delivery.recipes import FIGURES,_frecipe
        for i,spec in enumerate(FIGURES):
            recipe=_frecipe(i,spec,2)
            self.assertTrue(all(t in consumer.TYPES for t in recipe['problem_types']))
            self.assertEqual(recipe['required_inputs'][0]['field'],'dataset_ref')

    def test_incremental_rename_and_changed_bytes_do_not_inherit_review(self):
        from cumcm_delivery.incremental import diff
        def source(path,sha):return {'relative_path':path,'sha256':sha,'in_scope':True,'root_kind':'paper','year':2020}
        result=diff([source('old.pdf','a'*64),source('same.pdf','b'*64)],[source('renamed.pdf','a'*64),source('same.pdf','c'*64),source('new.pdf','d'*64)])
        self.assertEqual(result['renames'][0]['content_id'],'sha256:'+'a'*64)
        self.assertEqual(result['review_invalidated_content_sha256'],['b'*64])
        self.assertEqual(result['changed'][0]['review_status'],'pending_review')

    def test_document_result_refs_require_completed_current_task(self):
        from cumcm_delivery import contracts
        node={'node_id':'n1','task_id':'current','order':1,'node_type':'paragraph','content':{'text':'说明计算结论'},'result_refs':['missing'],'release_id':'cumcm-2010-2025-test','version':1}
        with self.assertRaises(ValueError):contracts.validate_document([node],[])
        node['result_refs']=[];node['parent_node_id']='n1'
        with self.assertRaises(ValueError):contracts.validate_document([node],[])
        node.pop('parent_node_id');node['node_type']='figure'
        with self.assertRaises(ValueError):contracts.validate_document([node],[])

    def test_context_group_does_not_split_a_line_before_formula(self):
        blocks=[{'kind':'text','text':'分析。'*250},{'kind':'formula_candidate','text':'y = ax + b'},{'kind':'text','text':'变量和约束说明。'*25}]
        groups=list(audit.paragraph_groups(blocks))
        self.assertTrue(any(blocks[0] in group and blocks[1] in group for _,group in groups))

    def test_figure_inputs_reject_historical_results_and_missing_units(self):
        from cumcm_delivery.recipes import FIGURES,_frecipe,check_figure_inputs
        for index,spec in enumerate(FIGURES):
            recipe=_frecipe(index,spec,2)
            self.assertEqual(check_figure_inputs(recipe,{})['status'],'missing')
            value={'dataset_ref':{'asset_id':'asset-'+'a'*64,'sha256':'a'*64,'version':1},'computation_run_id':'current-run','source':'current_task','computation_status':'succeeded','columns':[{'role':r['role'],'name':r['role'],'unit':'explicit_test_unit'} for r in recipe['required_column_roles']]}
            self.assertEqual(check_figure_inputs(recipe,value)['status'],'ready')
            value['source']='historical_reference'
            with self.assertRaises(ValueError):check_figure_inputs(recipe,value)
            value['source']='current_task';value['columns'][0].pop('unit')
            with self.assertRaises(ValueError):check_figure_inputs(recipe,value)

    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires installed pinned real tokenizer; separately verified with the private release')
    def test_budget_tokenizer_disables_saved_inference_truncation(self):
        from cumcm_delivery import services
        text='当前问题需要完整保留模型推导和约束。'*100
        self.assertGreater(services.untruncated_tokens(text),128)

if __name__=='__main__':unittest.main()
