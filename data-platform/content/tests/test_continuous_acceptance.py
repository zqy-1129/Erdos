"""Independent regressions for the failures reproduced during final acceptance."""
import copy,hashlib,io,json,os,subprocess,sys,tempfile,unittest,zipfile
from pathlib import Path
ROOT=Path(os.path.abspath(__file__)).parent.parent
sys.path.insert(0,str(ROOT/'scripts'))
import content_runtime as rt
import stage03_fileio as fio
from consumer_v2 import ReleaseStore,startup
from profile_extraction import detect_structure,empty_length,text_metrics
from continuous_fixture import build
PY=sys.executable
def cli(root,*args):return subprocess.run([PY,'-S','scripts/local_content_consumer.py','--release',str(root)]+list(args),cwd=str(ROOT),capture_output=True,text=True,encoding='utf8')
def fixture(root,third=False):
 rows=[dict(case_id='cumcm-2023-A-fixture',problem_id='cumcm-2023-A',profile_version=3,tags=['optimization'],resolution_status='resolved',license=dict(internal_analysis=True,distribute_original=False,distribute_profile=False,send_to_third_party_model=third))];build(root,rows)
@unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires separately installed private seed snapshots; see README')
class Acceptance(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def test_tampered_style_never_verified(self):
  root=self.base/'rel';fixture(root);s=ReleaseStore(root);_,refs,_=startup(s,'cumcm','latex','local_only');p=s.resolve_path('cumcm',refs['figure_style']['asset_id']);p.write_bytes(p.read_bytes()+b' ');r=cli(root,'outline','cumcm');self.assertNotEqual(r.returncode,0);self.assertIn('RESOURCE_HASH',r.stderr);self.assertNotIn('"verified": true',r.stdout)
 def test_tampered_metadata_rejected(self):
  root=self.base/'rel';fixture(root);p=root/'competitions/cumcm/index/family_index.json';p.write_bytes(p.read_bytes()+b' ');r=cli(root,'select','cumcm');self.assertNotEqual(r.returncode,0);self.assertIn('METADATA_HASH',r.stderr)
 def test_missing_layout_rejected(self):
  root=self.base/'rel';fixture(root);s=ReleaseStore(root);_,refs,_=startup(s,'cumcm','latex','local_only');s.resolve_path('cumcm',refs['layout']['asset_id']).unlink();self.assertNotEqual(cli(root,'select','cumcm','--format','latex').returncode,0)
 def test_positive_profile_bytes_returned(self):
  root=self.base/'rel';fixture(root);r=cli(root,'search','cumcm','--tags','optimization,optimization');self.assertEqual(r.returncode,0,r.stderr);d=json.loads(r.stdout)['matched_references'][0];self.assertEqual(d['matched_tag_count'],1);self.assertIn('profile_asset_ref',d)
 def test_third_party_reference_filter(self):
  root=self.base/'rel';fixture(root);r=cli(root,'search','cumcm','--tags','optimization','--model-execution','third_party');self.assertEqual(r.returncode,1);self.assertEqual(json.loads(r.stdout)['result'],'no_reference')
 def test_third_party_authorized_fixture(self):
  root=self.base/'rel';fixture(root,True);r=cli(root,'search','cumcm','--tags','optimization','--model-execution','third_party');self.assertEqual(r.returncode,0,r.stderr)
 def test_corrupt_cache_preserved_rejected(self):
  root=self.base/'rel';fixture(root);s=ReleaseStore(root);_,refs,_=startup(s,'cumcm','latex','local_only');aid=refs['ruleset']['asset_id'];cache=self.base/'cache';self.assertEqual(cli(root,'get',aid,'--cache-dir',str(cache)).returncode,0);p=next(cache.iterdir());p.write_bytes(b'corrupt');r=cli(root,'get',aid,'--cache-dir',str(cache));self.assertNotEqual(r.returncode,0);self.assertEqual(p.read_bytes(),b'corrupt')
 def test_cache_cannot_target_release(self):
  root=self.base/'rel';fixture(root);s=ReleaseStore(root);aid=next(iter(s.family_index('cumcm')['start_resources']));before=rt.checked(root/'integrity.json')['sha256'];asset=s._bundles['cumcm']['assets'][0]['asset_id'];r=cli(root,'get',asset,'--cache-dir',str(root/'cache'));self.assertNotEqual(r.returncode,0);self.assertEqual(before,rt.checked(root/'integrity.json')['sha256']);self.assertFalse((root/'cache').exists())
 def test_existing_snapshot_conflict_preserves_bytes(self):
  p=self.base/'out.json';fio.write_exact(p,b'old');with_error=False
  with self.assertRaises((OSError,ValueError,RuntimeError)):fio.write_exact(p,b'new')
  self.assertEqual(p.read_bytes(),b'old')
 def test_strict_duplicate_and_nan_json(self):
  for data in ['{"x":1,"x":2}','{"x":NaN}']:
   with self.assertRaises(ValueError):rt.parse(data)
 def test_zip_traversal_and_duplicate(self):
  for names in [['../bad'],['x','x'],['a\\b']]:
   buf=io.BytesIO()
   with zipfile.ZipFile(buf,'w') as z:
    for name in names:
     info=zipfile.ZipInfo('member');info.filename=name;z.writestr(info,b'test')
   with self.assertRaises(ValueError):rt.check_zip(buf.getvalue())
 def test_latex_dependency_packaged(self):
  root=self.base/'source';root.mkdir();(root/'main.tex').write_text(r'\documentclass{custom}',encoding='utf8');(root/'custom.cls').write_text('test',encoding='utf8');data=rt.deterministic_zip(root);self.assertEqual(set(rt.check_zip(data)),{'main.tex','custom.cls'});self.assertEqual(data,rt.deterministic_zip(root))
 def test_extra_snapshot_file_rejected(self):
  root=self.base/'snap';rt.write(root/'a.json',{'a':1});rt.seal(root);(root/'extra').write_bytes(b'x')
  with self.assertRaisesRegex(ValueError,'COVERAGE'):rt.check_snapshot(root)
 def test_length_does_not_invent_body(self):
  self.assertIsNone(empty_length()['zh_parts']['body']);self.assertFalse(text_metrics(['正文abc123'])['is_original_length'])
 def test_numbered_headings_keep_hierarchy_not_sentences(self):
  rows,_=detect_structure(['一、模型假设\n这是一段短句。\n1.1 变量定义\n1 Introduction\n1.1 The Model\n1.2 Cost = 4'], 'a'*64);self.assertEqual([r['level'] for r in rows],[1,2,1,2]);self.assertNotIn('这是一段短句。',[r['section_title'] for r in rows])
 def test_dynamic_question_grouping_and_budget(self):
  root=self.base/'rel';fixture(root);r=cli(root,'outline','cumcm','--subproblems','4','--budget','12000');self.assertEqual(r.returncode,0,r.stderr);d=json.loads(r.stdout);idx=[s['subproblem_index'] for s in d['dynamic_outline'] if s['subproblem_index']];self.assertEqual(idx,sorted(idx));self.assertEqual(sum(x['amount'] for x in d['length_budget']['section_budgets']),12000);self.assertFalse(d['length_budget']['verified'])
 def test_zero_questions_rejected(self):
  root=self.base/'rel';fixture(root);self.assertNotEqual(cli(root,'outline','cumcm','--subproblems','0').returncode,0)
 def test_duplicate_task_questions_rejected(self):
  root=self.base/'rel';fixture(root);task=self.base/'task.json';rt.write(task,dict(task_id='t1',competition_id='cumcm',subproblems=[dict(subproblem_id='q1'),dict(subproblem_id='q1')]));r=cli(root,'outline','cumcm','--task-json',str(task));self.assertNotEqual(r.returncode,0);self.assertIn('DUPLICATE_SUBPROBLEM',r.stderr)
 def test_unknown_task_has_no_invented_figure_and_hash_bound(self):
  root=self.base/'rel';fixture(root);task=self.base/'task.json';rt.write(task,dict(task_id='t1',competition_id='cumcm',is_fixture=False,subproblems=[dict(subproblem_id='q1',problem_types=['unknown'])]));r=cli(root,'outline','cumcm','--task-json',str(task));self.assertEqual(r.returncode,0,r.stderr);d=json.loads(r.stdout);self.assertEqual(d['figure_requirements'][0]['kind'],'unspecified');self.assertEqual(d['task_context']['extensions']['task_input_sha256'],rt.checked(task)['sha256']);self.assertFalse(d['task_context']['is_fixture'])
 def test_content_version_can_advance_schema_stays_v1(self):
  from normalize_cumcm_sample import validate_contract
  b=rt.load(ROOT/'normalized/stage05_codex_v3/bundle.json');self.assertEqual(validate_contract(b),[]);bad=copy.deepcopy(b);bad['figure_styles'][0]['version']=0;self.assertTrue(validate_contract(bad));bad=copy.deepcopy(b);bad['figure_styles'][0]['schema_version']=2;self.assertTrue(validate_contract(bad))
 def test_extraction_cache_includes_extension(self):
  from batch_content_v2 import extract
  source=self.base/'sample.txt';source.write_bytes(b'ABC');record=dict(sha256=hashlib.sha256(b'ABC').hexdigest(),size=3,extension='.txt');a,_,_=extract(record,source,self.base/'cache');record['extension']='.bin';b,_,_=extract(record,source,self.base/'cache');self.assertEqual(a['status'],'extracted');self.assertEqual(b['status'],'source_only')
 def test_changed_paper_advances_asset_and_profile_versions(self):
  # A minimal, independently generated PDF is placed at one stable legacy test identity.
  legacy=rt.load(ROOT/'out/pack/cases_seed_v0_sources.json')['items'][0];rel=legacy['source']['rel'];data=self.base/'data';source=fio.relative(data,rel);source.parent.mkdir(parents=True)
  stream=b'BT /F1 12 Tf 72 700 Td (1 Introduction) Tj 0 -20 Td (1.1 Model) Tj ET';objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream'];pdf=b'%PDF-1.4\n';offsets=[0]
  for i,obj in enumerate(objects,1):offsets.append(len(pdf));pdf+=str(i).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
  pos=len(pdf);pdf+=b'xref\n0 6\n0000000000 65535 f \n'+b''.join(('%010d 00000 n \n'%o).encode() for o in offsets[1:])+b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(pos).encode()+b'\n%%EOF\n';source.write_bytes(pdf);s4=self.base/'s4';rt.write(s4/'bundle.json',dict(problems=[],cases=[],assets=[]));rt.seal(s4);inv=self.base/'inv';inv.mkdir()
  def run(name,previous=None):
   row=dict(relative_path=rel,competition_ids=['cumcm'],sha256=hashlib.sha256(source.read_bytes()).hexdigest(),size=source.stat().st_size,extension='.pdf',status='ok',source_root='fixture',resource_type_hint='paper');(inv/'source_files.jsonl').write_bytes((json.dumps(row)+'\n').encode());out=self.base/name;cmd=[PY,'scripts/batch_content_v2.py','--data-root',str(data),'--inventory',str(inv),'--out',str(out),'--stage04',str(s4),'--cache',str(self.base/'cache')]
   if previous:cmd+=['--previous',str(previous)]
   r=subprocess.run(cmd,cwd=str(ROOT),capture_output=True,text=True,encoding='utf8');self.assertEqual(r.returncode,0,r.stdout+r.stderr);return out,rt.load(out/'bundle.json')
  first,b1=run('first');source.write_bytes(source.read_bytes()+b'% changed bytes\n');second,b2=run('second',first);self.assertEqual(b1['cases'][0]['case_id'],b2['cases'][0]['case_id']);self.assertEqual(b2['cases'][0]['profile_version'],2);self.assertTrue(all(a['version']==2 for a in b2['assets']));third,b3=run('third',second);self.assertEqual(b2['cases'],b3['cases']);self.assertTrue(all(a['version']==2 for a in b3['assets']));rt.check_snapshot(first)
 def test_batch_incremental_change_rename_remove(self):
  data=self.base/'data';data.mkdir();(data/'a.txt').write_bytes(b'one');(data/'b.txt').write_bytes(b'two');s4=self.base/'s4';rt.write(s4/'bundle.json',dict(problems=[],cases=[],assets=[]));rt.seal(s4);inv=self.base/'inv';inv.mkdir()
  def scan():
   rows=[]
   for p in sorted(data.glob('*.txt')):rows.append(dict(relative_path=p.name,competition_ids=['cumcm'],sha256=hashlib.sha256(p.read_bytes()).hexdigest(),size=p.stat().st_size,extension='.txt',status='ok',source_root='fixture',resource_type_hint='unknown'))
   (inv/'source_files.jsonl').write_bytes(b''.join((json.dumps(r)+'\n').encode() for r in rows))
  def run(out,previous=None,maxdocs=None):
   cmd=[PY,'scripts/batch_content_v2.py','--data-root',str(data),'--inventory',str(inv),'--out',str(out),'--stage04',str(s4),'--cache',str(self.base/'cache')]
   if previous:cmd+=['--previous',str(previous),'--resume']
   if maxdocs:cmd+=['--max-documents',str(maxdocs)]
   r=subprocess.run(cmd,cwd=str(ROOT),capture_output=True,text=True,encoding='utf8');self.assertEqual(r.returncode,0,r.stdout+r.stderr)
  scan();old=self.base/'old';run(old);proof=rt.checked(old/'integrity.json')['sha256'];(data/'renamed.txt').write_bytes((data/'a.txt').read_bytes());(data/'a.txt').unlink();(data/'b.txt').unlink();(data/'c.txt').write_bytes(b'new');scan();new=self.base/'new';run(new,old);changes=rt.load(new/'source_changes.json');self.assertEqual(changes['counts'],{'added':1,'renamed':1,'removed':1});self.assertEqual(proof,rt.checked(old/'integrity.json')['sha256']);rt.check_snapshot(old)
  oldid=rt.load(old/'source_identity_ledger.json')['cumcm|a.txt'];self.assertEqual(rt.load(new/'source_identity_ledger.json')['cumcm|renamed.txt'],oldid);(data/'renamed.txt').write_bytes(b'changed');scan();third=self.base/'third';run(third,new);self.assertEqual(rt.load(third/'source_changes.json')['counts'],{'unchanged':1,'changed':1})
if __name__=='__main__':unittest.main()
