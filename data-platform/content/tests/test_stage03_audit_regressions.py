"""Audit regressions for missing texts, stale evidence, copy races and read-only QC."""
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

CONTENT=Path(os.path.abspath(__file__)).parents[1]
SPEC=importlib.util.spec_from_file_location('stage03_audit',str(CONTENT/'scripts/normalize_cumcm_sample.py'))
n=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(n)

class Stage03AuditTests(unittest.TestCase):
    def setUp(self):
        (CONTENT/'out').mkdir(exist_ok=True)
        self.manager=tempfile.TemporaryDirectory(dir=str(CONTENT/'out'));self.addCleanup(self.manager.cleanup)
        self.root=Path(self.manager.name)

    def context(self,text=True):
        fake=self.root/'content';(fake/'out/pack').mkdir(parents=True)
        pp,cc=n.load_seed_records()
        (fake/'out/pack/problems.json').write_text(json.dumps(list(pp.values())),encoding='utf-8')
        (fake/'out/pack/cases.json').write_text(json.dumps(list(cc.values())),encoding='utf-8')
        (fake/'config').mkdir()
        shutil.copyfile(str(CONTENT/'config/stage03_parent_evidence.json'),str(fake/'config/stage03_parent_evidence.json'))
        if text:
            (fake/'out/texts').mkdir();shutil.copyfile(str(CONTENT/'out/texts/cumcm-2018-B.txt'),str(fake/'out/texts/cumcm-2018-B.txt'))
        paths=[n.SAMPLE_PROBLEMS[0]['source']]+n.SAMPLE_PROBLEMS[0]['attachments']
        return fake,{x['rel']:x['seed_sha256'] for x in paths}

    def run_one(self,fake,inventory,out=None):
        with patch.object(n,'CONTENT_DIR',fake),patch.object(n,'inventory_hashes',return_value=inventory):
            return n.run_normalize('D:/Erdos_data',out or self.root/'sample',['cumcm-2018-B'],[])

    def check_one(self,fake,inventory,out=None):
        with patch.object(n,'CONTENT_DIR',fake),patch.object(n,'inventory_hashes',return_value=inventory):
            return n.check_normalized('D:/Erdos_data',out or self.root/'sample')

    def test_empty_file_unavailable(self):
        f=self.root/'empty';f.write_bytes(b'')
        self.assertEqual(n.hash_verified(f)['status'],'empty')
        self.assertTrue(n.copy_verified(f,self.root/'outfile')['conflict'])
        self.assertFalse((self.root/'outfile').exists())

    def test_after_stat_failure_does_not_crash(self):
        f=self.root/'data';f.write_bytes(b'abc')
        with patch.object(n.fileio,'stat_file',side_effect=[{'size':3,'mtime_ns':1,'file_id':[1,1]},OSError('unavailable after read')]):
            info=n.hash_verified(f)
        self.assertEqual(info['status'],'failed');self.assertIsNone(info['sha256'])

    def test_source_changes_during_copy_not_published(self):
        f=self.root/'data';f.write_bytes(b'v1');dst=self.root/'copied'
        original=n.fileio.hash_verified;calls=[]
        def changing(path):
            if Path(path)==f:
                calls.append(1)
                if len(calls)==2:f.write_bytes(b'v2 changed')
            return original(path)
        with patch.object(n.fileio,'hash_verified',side_effect=changing):r=n.copy_verified(f,dst)
        self.assertTrue(r['conflict']);self.assertFalse(dst.exists())

    def test_reparse_source_rejected(self):
        f=self.root/'linked';f.write_bytes(b'not linked physically')
        original=os.lstat
        def pretend(path):
            if os.path.abspath(str(path))==os.path.abspath(str(f)):
                return SimpleNamespace(st_mode=stat.S_IFREG,st_file_attributes=0x400)
            return original(path)
        with patch.object(n.fileio.os,'lstat',side_effect=pretend):
            self.assertEqual(n.hash_verified(f)['status'],'failed')

    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires private seed snapshots')
    def test_missing_text_is_failure_and_no_bundle(self):
        fake,index=self.context(text=False);report,_=self.run_one(fake,index)
        self.assertFalse(report['qc']['passed']);self.assertTrue(any(e['code']=='TEXT_INPUT' for e in report['errors']))
        self.assertFalse((self.root/'sample/bundle.json').exists())

    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires private seed snapshots')
    def test_wrong_text_hash_is_failure(self):
        fake,index=self.context();(fake/'out/texts/cumcm-2018-B.txt').write_text('corrupted text',encoding='utf-8')
        report,_=self.run_one(fake,index);self.assertFalse(report['qc']['passed'])

    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires private seed snapshots')
    def test_missing_inventory_hash_blocks_snapshot(self):
        fake,index=self.context();report,_=self.run_one(fake,{})
        self.assertFalse(report['qc']['passed']);self.assertFalse((self.root/'sample/bundle.json').exists())

    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires private seed snapshots')
    def test_check_is_read_only(self):
        fake,index=self.context();report,_=self.run_one(fake,index);self.assertTrue(report['qc']['passed'],report['errors'])
        out=self.root/'sample'
        before={str(p.relative_to(out)):(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) for p in out.rglob('*') if p.is_file()}
        self.assertTrue(self.check_one(fake,index)['passed'])
        after={str(p.relative_to(out)):(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) for p in out.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires private seed snapshots')
    def test_check_missing_resource_fails_without_repair(self):
        fake,index=self.context();self.run_one(fake,index)
        out=self.root/'sample';binding=json.loads((out/'provenance.json').read_text(encoding='utf-8'))[0]
        target=out/binding['local_rel'];target.unlink()
        result=self.check_one(fake,index);self.assertFalse(result['passed']);self.assertFalse(target.exists())

    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires private seed snapshots')
    def test_prompt_tamper_rejected(self):
        fake,index=self.context();self.run_one(fake,index)
        p=self.root/'sample/problems/cumcm-2018-B/extracted/prompt.zh.txt';p.write_bytes(b'changed')
        result=self.check_one(fake,index);self.assertFalse(result['passed']);self.assertEqual(p.read_bytes(),b'changed')

    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires private seed snapshots')
    def test_existing_profile_changes_preserved(self):
        fake,index=self.context();self.run_one(fake,index)
        p=self.root/'sample/problems/cumcm-2018-B/profile.json';d=json.loads(p.read_text(encoding='utf-8'));d['profile_version']=2;p.write_text(json.dumps(d),encoding='utf-8');before=p.read_bytes()
        report,_=self.run_one(fake,index)
        self.assertFalse(report['qc']['passed']);self.assertTrue(any(e['code']=='OUTPUT_CONFLICT' for e in report['errors']));self.assertEqual(p.read_bytes(),before)

    def test_parent_evidence_source_lock_required(self):
        c=n.SAMPLE_CASES[0];ledger={e['case_id']:e for e in n.json_read(CONTENT/'config/stage03_parent_evidence.json')['records']}
        evidence,reason=n.parent_decision(c,'a'*64,'b'*64,{'results':{}},ledger)
        self.assertIsNone(evidence);self.assertIn('source/version',reason)

    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires private seed snapshots')
    def test_unconfirmed_parent_kept_null_in_staging(self):
        fake,index=self.context();case=n.SAMPLE_CASES[0];index[case['source']['rel']]=case['source']['seed_sha256']
        (fake/'config/stage03_parent_evidence.json').write_text('{"records":[]}',encoding='utf-8')
        with patch.object(n,'CONTENT_DIR',fake),patch.object(n,'inventory_hashes',return_value=index):
            report,bundle=n.run_normalize('D:/Erdos_data',self.root/'sample',['cumcm-2018-B'],[case['case_id']])
        profile=bundle['cases'][0];self.assertIsNone(profile['problem_id']);self.assertEqual(profile['resolution_status'],'unresolved');self.assertEqual(profile['publication_status'],'staging');self.assertFalse(report['qc']['passed'])

    def test_sample_selection_rejects_unselected_parent(self):
        with self.assertRaises(ValueError):n.select_samples(['cumcm-2018-B'],['cumcm-2023-A-092'])
        with self.assertRaises(ValueError):n.select_samples(['invented'],[])

    def test_paths_and_overlapping_roots_rejected(self):
        for rel in ['../escape','C:/escape','/absolute','a\\..\\b']:
            with self.assertRaises(ValueError):n.fileio.relative(self.root,rel)
        with self.assertRaises(ValueError):n.disjoint_roots(self.root,self.root/'out')

    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires private seed snapshots')
    def test_cli_report_cannot_overwrite_business_file(self):
        out=self.root/'sample';out.mkdir();target=out/'bundle.json';target.write_bytes(b'preserve me')
        run=subprocess.run([sys.executable,str(CONTENT/'scripts/normalize_cumcm_sample.py'),'--check','--out',str(out),'--json-report',str(target)],capture_output=True,encoding='utf-8',env=dict(os.environ,PYTHONIOENCODING='utf-8'))
        self.assertEqual(run.returncode,1);self.assertEqual(target.read_bytes(),b'preserve me')

if __name__=='__main__':unittest.main()
