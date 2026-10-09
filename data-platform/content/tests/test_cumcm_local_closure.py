import copy,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).absolute().parents[1]/'scripts'))
from cumcm_delivery import core,business_contracts as bc,legacy_projection,consumer,contracts
from cumcm_delivery.offline_sdk import OfflineRelease,sha

class LocalClosureTests(unittest.TestCase):
    def test_long_case_ids_have_stable_distinct_aliases_without_losing_original(self):
        a='cumcm-paper-'+'a'*64;b='cumcm-paper-'+'a'*63+'b'
        aa=legacy_projection.alias('case',a);bb=legacy_projection.alias('case',b)
        self.assertNotEqual(aa,bb);self.assertLessEqual(len(aa),64)
        self.assertEqual(aa,legacy_projection.alias('case',a))
        self.assertEqual(legacy_projection.alias('case','prior-existing-case'),'prior-existing-case')

    def test_actual_server_record_fields_match_contract(self):
        pin=legacy_projection.validate_port_fields();self.assertEqual(len(pin['records']['legacy_problem']),12)

    def test_visual_full_page_cannot_claim_isolation(self):
        row={'component_id':'v','kind':'figure','source_sha256':'a'*64,'page':1,'anchor_block_id':'b','anchor_text':'图1',
            'anchor_bbox':[1,1,20,20],'coordinate_system':'pdf_points_bottom_left',
            'asset_ref':{'asset_id':'asset-'+'b'*64,'version':1,'sha256':'b'*64},'evidence_scope':'complete_page',
            'includes_neighbor_content':False,'isolation_status':'pending','semantic_status':'candidate','latex':None,'data_reconstructed':False}
        with self.assertRaises(ValueError):bc.validate('visual',row)
        row['includes_neighbor_content']=True;bc.validate('visual',row)
        row['data_reconstructed']=True
        with self.assertRaises(Exception):bc.validate('visual',row)

    def test_v3_rights_enum_preserved_while_local_purpose_is_validated(self):
        self.assertNotIn('team_internal',contracts.schemas()['retrieve_request']['properties']['usage_purpose']['enum'])
        request={'competition_id':'cumcm','release_id':'cumcm-2010-2025-codex-v3','stage':'model','subproblem':{'goal':'标定模型'},'usage_purpose':'team_internal'}
        consumer.validate_request(request)
        request['token_budget']=True
        with self.assertRaises(ValueError):consumer.validate_request(request)

    def fixture(self,root,local=True):
        asset_data=b'local historical evidence';digest=sha(asset_data);asset_id='asset-'+digest
        files={'objects/'+digest:asset_data,'assets.json':json.dumps([{'asset_id':asset_id,'version':1,'sha256':digest,'release_path':'objects/'+digest,'external_consumer_allowed':False,'origins':[]}]).encode(),
            'metadata.json':json.dumps({'release_id':'cumcm-2010-2025-fixture','format_profiles':{},'local_usage_policy':{'local_internal_retrieval_allowed':local}}).encode()}
        entries=[]
        for rel,data in files.items():
            p=Path(root)/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
            entries.append({'path':rel,'sha256':sha(data),'size':len(data)})
        manifest=json.dumps({'release_id':'cumcm-2010-2025-fixture','files':entries}).encode();pin=sha(manifest)
        (Path(root)/'manifest.json').write_bytes(manifest);(Path(root)/'SEALED.json').write_text(json.dumps({'manifest_sha256':pin}))
        return pin,asset_id,digest

    def test_offline_requires_trusted_pin_and_verifies_bytes_not_local_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            pin,aid,digest=self.fixture(directory);sdk=OfflineRelease(directory,pin)
            self.assertEqual(sdk.fetch(aid,1,digest),b'local historical evidence')
            with self.assertRaises(ValueError):OfflineRelease(directory,'0'*64)
            with self.assertRaises(ValueError):sdk.read('../anything')
            (Path(directory)/('objects/'+digest)).write_bytes(b'altered data')
            with self.assertRaises(ValueError):sdk.fetch(aid,1,digest)

    def test_offline_local_scope_does_not_create_rights_on_old_release(self):
        with tempfile.TemporaryDirectory() as directory:
            pin,aid,digest=self.fixture(directory,local=False);sdk=OfflineRelease(directory,pin)
            with self.assertRaises(PermissionError):sdk.fetch(aid,1,digest)

    def test_offline_public_read_cannot_bypass_private_asset_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            pin,aid,digest=self.fixture(directory);sdk=OfflineRelease(directory,pin,scope='consumer')
            with self.assertRaises(PermissionError):sdk.read('objects/'+digest)
            with self.assertRaises(PermissionError):sdk.fetch(aid,1,digest)
            self.assertEqual(sdk.json('metadata.json')['release_id'],sdk.release_id)
            with self.assertRaises(ValueError):OfflineRelease(directory,pin,scope='audit')

if __name__=='__main__':unittest.main()
