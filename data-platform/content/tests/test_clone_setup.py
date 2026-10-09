"""Fresh-clone behavior and query/runtime separation; no private corpus required."""
import copy, importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

CONTENT=Path(__file__).absolute().parents[1]
sys.path.insert(0,str(CONTENT/'scripts'))
from cumcm_delivery import core,services,legacy_projection,local_config
from cumcm_delivery.online_sdk import ContentClient,NoRedirect,validate_host
spec=importlib.util.spec_from_file_location('clone_setup',CONTENT/'scripts/clone_setup.py')
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)


class CloneSetupTests(unittest.TestCase):
    def test_server_contract_uses_this_checkout(self):
        self.assertEqual(legacy_projection.PORTS,CONTENT.parents[1]/'server/app/domain/entitlement/ports.py')
        self.assertEqual(len(legacy_projection.validate_port_fields()['records']['legacy_case']),8)

    def test_sdk_allows_alternate_loopback_port_only(self):
        self.assertEqual(ContentClient('fixture','http://127.0.0.1:18791').url,'http://127.0.0.1:18791')
        for url in ('http://example.com:18789','http://127.0.0.1:18789/x','http://user@127.0.0.1:18789','http://127.0.0.1:18789?x=1','http://127.0.0.1:18789#x'):
            with self.subTest(url=url),self.assertRaises(ValueError):ContentClient('fixture',url)

    def test_lab_access_requires_explicit_opt_in_and_private_ip(self):
        for host in ('172.27.50.249','192.168.1.10','10.2.3.4'):
            with self.assertRaises(ValueError):ContentClient('fixture','http://'+host+':18789')
            ContentClient('fixture','http://'+host+':18789',allow_lan=True)
        for host in ('0.0.0.0','8.8.8.8','198.18.0.1','::1','example.com'):
            with self.assertRaises(ValueError):validate_host(host,allow_lan=True)

    def test_redirect_cannot_forward_team_credential(self):
        with self.assertRaises(ValueError):NoRedirect().redirect_request(None,None,None,302,'m',{},'http://outside.invalid')

    def test_private_config_is_consistent_and_never_rotates(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(core,'CONTENT_DIR',Path(tmp)):
            local_config.create(55434,18334,18791)
            folder=Path(tmp)/'.runtime/local';cfg=core.load_json(folder/'services.json')
            self.assertIn('POSTGRES_PASSWORD='+cfg['postgres']['password'],(folder/'postgres.env').read_text())
            self.assertEqual(cfg['s3']['aws_access_key_id'],core.load_json(folder/'s3.json')['identities'][0]['credentials'][0]['accessKey'])
            self.assertEqual(cfg['embedding']['provider'],'CUDAExecutionProvider')
            self.assertEqual(cfg['query_embedding_provider'],'CPUExecutionProvider')
            before=(folder/'services.json').read_bytes()
            with self.assertRaises(ValueError):local_config.create()
            self.assertEqual(before,(folder/'services.json').read_bytes())

    def test_untrusted_release_rejected_before_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp);(source/'manifest.json').write_text('{}')
            with self.assertRaises(ValueError):setup.install_release(source)

    def test_pin_cache_invalidated_when_profile_changes(self):
        cfg={'environment':'local-audit','embedding':{'model':'fixture','revision':'one','dimensions':384}}
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'ERDOS_EMBEDDING_DIR':tmp}),patch.object(services,'configuration',side_effect=lambda:cfg):
            (Path(tmp)/'tokenizer.json').write_text('fixture')
            old=services.model_manifest();services._pin_cache=None
            services.verify_model_pin(old)
            cfg['embedding']['revision']='two'
            with self.assertRaises(ValueError):services.verify_model_pin(old)
            services._pin_cache=None

    def test_query_runtime_does_not_change_index_manifest(self):
        cfg={'environment':'local-audit','embedding':{'model':'fixture','revision':'one','dimensions':384,'provider':'CUDAExecutionProvider'}}
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'ERDOS_EMBEDDING_DIR':tmp}),patch.object(services,'configuration',side_effect=lambda:cfg):
            before=services.model_manifest();cfg['query_embedding_provider']='CPUExecutionProvider'
            self.assertEqual(before,services.model_manifest())


if __name__=='__main__':unittest.main()
