"""阶段06 回归与负例测试：候选包构建、本地消费者、语义门禁。

- 消费者测试全部走实际 CLI（subprocess），不偷读原始数据仓。
- 正例检索用独立构造的合法 fixture release（许可 internal_analysis=true），不篡改真实论文权限。
- 语义负例基于已通过校验的真实 cumcm 候选包/任务上下文，逐项变异后断言对应错误码。
- 全部相对路径统一走 stage03_fileio 防穿越，测试 zip-slip/.. 拒绝。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CONTENT_DIR = Path(os.path.abspath(__file__)).parent.parent
sys.path.insert(0, str(CONTENT_DIR / "scripts"))
import stage03_fileio as fio  # noqa: E402

PY = sys.executable
PY_S = [PY, "-S"]


def run(cmd, check=False):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=str(CONTENT_DIR), env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return r


def run_json(cmd):
    r = run(cmd)
    try:
        return r.returncode, json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.returncode, None


RELEASE = "out/releases/content-candidate-codex-v3"


def build_fixture_release(root: Path, refs):
    from continuous_fixture import build
    build(root, refs)


def licensed_ref(case_id, tags, send_third=False):
    return {
        "case_id": case_id, "problem_id": "cumcm-2023-A", "profile_version": 2,
        "source_sha256": "0" * 64, "tags": tags, "resolution_status": "resolved",
        "license": {"internal_analysis": True, "distribute_original": False,
                    "distribute_profile": False, "send_to_third_party_model": send_third},
    }


def make_context_bundle(task_context):
    b = json.loads((CONTENT_DIR / RELEASE / "competitions/cumcm/bundle.json").read_text(encoding="utf8"))
    b["task_contexts"] = [task_context]
    return b


@unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires separately installed private seed snapshots; see README')
class TestCatalogAndSelect(unittest.TestCase):
    def test_list_19(self):
        code, data = run_json([PY, "scripts/local_content_consumer.py", "list"])
        self.assertEqual(code, 0)
        self.assertEqual(data["count"], 19)

    def test_select_format(self):
        code, data = run_json([PY, "scripts/local_content_consumer.py", "select", "cumcm", "--format", "latex"])
        self.assertEqual(code, 0)
        self.assertEqual(data["format"], "latex")

    def test_select_bad_format(self):
        r = run([PY, "scripts/local_content_consumer.py", "select", "cumcm", "--format", "pdf"])
        self.assertEqual(r.returncode, 2)


@unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires separately installed private seed snapshots; see README')
class TestSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="erdos-fixture-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_reference_unlicensed(self):
        code, data = run_json([PY, "scripts/local_content_consumer.py", "search", "cumcm",
                               "--tags", "optimization,mechanism"])
        self.assertEqual(code, 1)
        self.assertEqual(data["result"], "no_reference")

    def test_positive_fixture(self):
        root = Path(self.tmp) / "rel"
        build_fixture_release(root, [licensed_ref("cumcm-2023-A-092", ["optimization", "mechanism"])])
        code, data = run_json([PY, "scripts/local_content_consumer.py", "--release", str(root),
                               "search", "cumcm", "--tags", "optimization"])
        self.assertEqual(code, 0)
        self.assertEqual(data["result"], "ok")
        self.assertEqual(data["matched_references"][0]["case_id"], "cumcm-2023-A-092")

    def test_or_vs_and(self):
        root = Path(self.tmp) / "rel"
        build_fixture_release(root, [
            licensed_ref("cumcm-2023-A-092", ["optimization", "mechanism"]),
            licensed_ref("cumcm-2023-A-165", ["prediction"]),
        ])
        # OR：optimization 命中 092；prediction 命中 165
        _, or_data = run_json([PY, "scripts/local_content_consumer.py", "--release", str(root),
                               "search", "cumcm", "--tags", "optimization,prediction"])
        self.assertEqual(len(or_data["matched_references"]), 2)
        # AND：同时含 optimization 与 prediction 的没有
        _, and_data = run_json([PY, "scripts/local_content_consumer.py", "--release", str(root),
                                "search", "cumcm", "--tags", "optimization,prediction", "--tags-all"])
        self.assertEqual(and_data["result"], "no_reference")

    def test_unknown_tag(self):
        root = Path(self.tmp) / "rel"
        build_fixture_release(root, [licensed_ref("cumcm-2023-A-092", ["optimization"])])
        code, data = run_json([PY, "scripts/local_content_consumer.py", "--release", str(root),
                               "search", "cumcm", "--tags", "nonexistent_tag_xyz"])
        self.assertEqual(data["result"], "no_reference")


@unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires separately installed private seed snapshots; see README')
class TestGet(unittest.TestCase):
    def _ruleset_id(self):
        assets = json.loads((CONTENT_DIR / RELEASE / "competitions" / "cumcm" / "assets.json").read_text(encoding="utf-8"))
        return [a["asset_id"] for a in assets if a["role"] == "ruleset"][0]

    def test_get_verifies(self):
        aid = self._ruleset_id()
        code, data = run_json([PY, "scripts/local_content_consumer.py", "get", aid])
        self.assertEqual(code, 0)
        self.assertTrue(data["verified"])

    def test_get_wrong_version(self):
        aid = self._ruleset_id()
        r = run([PY, "scripts/local_content_consumer.py", "get", aid, "--version", "999"])
        self.assertEqual(r.returncode, 2)

    def test_get_unknown_asset(self):
        r = run([PY, "scripts/local_content_consumer.py", "get", "../escape"])
        self.assertEqual(r.returncode, 2)


@unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires separately installed private seed snapshots; see README')
class TestOutline(unittest.TestCase):
    def test_dynamic_outline_and_valid_context(self):
        r = run([PY, "scripts/local_content_consumer.py", "outline", "cumcm", "--subproblems", "4", "--format", "latex"])
        self.assertEqual(r.returncode, 0)
        d = json.loads(r.stdout)
        model = [s for s in d["dynamic_outline"] if s["section_type"] == "model" and s["subproblem_index"]]
        solve = [s for s in d["dynamic_outline"] if s["section_type"] == "solve" and s["subproblem_index"]]
        self.assertEqual(len(model), 4)
        self.assertEqual(len(solve), 4)
        bundle = make_context_bundle(d["task_context"])
        tmp = Path(tempfile.mkdtemp()) / "bundle.json"
        tmp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        chk = run(PY_S + ["scripts/validate_content_contracts.py", "--check-bundle", str(tmp)])
        self.assertEqual(chk.returncode, 0, chk.stdout)


@unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires separately installed private seed snapshots; see README')
class TestSemantics(unittest.TestCase):
    """基于真实候选包逐项变异断言语义门禁。"""

    def _cumcm_bundle(self):
        p = CONTENT_DIR / RELEASE / "competitions" / "cumcm" / "bundle.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def _check(self, bundle):
        tmp = Path(tempfile.mkdtemp()) / "bundle.json"
        tmp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        return run(PY_S + ["scripts/validate_content_contracts.py", "--check-bundle", str(tmp)])

    def test_wrong_parent(self):
        b = self._cumcm_bundle()
        next(c for c in b["cases"] if c["resolution_status"] == "resolved")["problem_id"] = "cumcm-9999-X"
        r = self._check(b)
        self.assertEqual(r.returncode, 1)
        self.assertIn("PARENT_MISSING", r.stdout)

    def test_release_removal_inconsistency(self):
        b = self._cumcm_bundle()
        rel = b["releases"][0]
        rel["items"][0]["change"] = "remove"  # 未加入 removed
        r = self._check(b)
        self.assertEqual(r.returncode, 1)
        self.assertIn("REMOVAL_STATE", r.stdout)

    def test_unverified_rules_compliance(self):
        r = run([PY, "scripts/local_content_consumer.py", "outline", "cumcm", "--subproblems", "1", "--format", "latex"])
        self.assertEqual(r.returncode, 0)
        tc = json.loads(r.stdout)["task_context"]
        tc["rules_compliance"] = "verified"  # 但 ruleset_status=unknown
        bundle = make_context_bundle(tc)
        tmp = Path(tempfile.mkdtemp()) / "bundle.json"
        tmp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        chk = run(PY_S + ["scripts/validate_content_contracts.py", "--check-bundle", str(tmp)])
        self.assertEqual(chk.returncode, 1)
        self.assertIn("RULES_UNVERIFIED", chk.stdout)

    def test_third_party_send_rejection(self):
        # 未知授权原论文不能外发第三方模型（license.send_to_third_party_model=false）
        r = run([PY, "scripts/local_content_consumer.py", "outline", "cumcm", "--subproblems", "1", "--format", "latex"])
        tc = json.loads(r.stdout)["task_context"]
        tc["model_execution"] = "third_party"
        # 真实 cumcm 案例 license 全 false，加入候选即应拒绝
        s4 = json.loads((CONTENT_DIR / "normalized/stage04/cumcm_codex_v3/bundle.json").read_text(encoding="utf-8"))
        case = s4["cases"][0]
        prof_asset = [a for a in s4["assets"] if a["role"] == "case_profile" and a["owner_id"] == case["case_id"]][0]
        tc["candidate_profiles"] = [{
            "case_id": case["case_id"], "profile_version": case["profile_version"],
            "source_sha256": case["source_sha256"],
            "profile_asset_ref": {"asset_id": prof_asset["asset_id"], "version": prof_asset["version"],
                                  "sha256": prof_asset["sha256"]},
        }]
        bundle = make_context_bundle(tc)
        tmp = Path(tempfile.mkdtemp()) / "bundle.json"
        tmp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        chk = run(PY_S + ["scripts/validate_content_contracts.py", "--check-bundle", str(tmp)])
        self.assertEqual(chk.returncode, 1)
        self.assertIn("LICENSE_SCOPE", chk.stdout)


@unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires separately installed private seed snapshots; see README')
class TestPathSafety(unittest.TestCase):
    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with self.assertRaises(ValueError):
                fio.relative(root, "../escape")
            with self.assertRaises(ValueError):
                fio.relative(root, "a/../../b")

    def test_zip_slip_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with self.assertRaises(ValueError):
                fio.relative(root, "sub/..\\..\\win")  # 反斜杠 / '.. ' 成分


if __name__ == "__main__":
    unittest.main(verbosity=2)
