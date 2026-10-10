"""阶段03 国赛小样本标准化的独立性回归：副本一致、幂等、冲突、缺失、画像占位。"""
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "normalize_under_test", str(Path(os.path.abspath(__file__)).parents[1] / "scripts" / "normalize_cumcm_sample.py"))
norm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(norm)


class CumcmSampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cumcm_sample_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_hash_verified_matches_python_sha(self):
        f = self.tmp / "a.bin"
        f.write_bytes(b"hello sample")
        info = norm.hash_verified(f)
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["sha256"], hashlib.sha256(b"hello sample").hexdigest())
        self.assertEqual(info["size"], len(b"hello sample"))

    def test_hash_verified_missing_is_not_ok(self):
        info = norm.hash_verified(self.tmp / "does_not_exist.bin")
        self.assertEqual(info["status"], "missing")
        self.assertIsNone(info["sha256"])

    def test_copy_verified_idempotent(self):
        src = self.tmp / "src.bin"
        src.write_bytes(b"data")
        dst = self.tmp / "dst.bin"
        r1 = norm.copy_verified(src, dst)
        self.assertEqual(r1["action"], "copied")
        self.assertEqual(r1["copy_sha256"], hashlib.sha256(b"data").hexdigest())
        self.assertFalse(r1["conflict"])
        r2 = norm.copy_verified(src, dst)
        self.assertEqual(r2["action"], "reused")
        self.assertEqual(r2["copy_sha256"], r1["copy_sha256"])

    def test_copy_conflict_not_overwritten(self):
        src = self.tmp / "src.bin"
        src.write_bytes(b"v1")
        dst = self.tmp / "dst.bin"
        norm.copy_verified(src, dst)
        src.write_bytes(b"v2-different-content")
        r = norm.copy_verified(src, dst)
        self.assertTrue(r["conflict"])
        # 已有不一致副本不被静默覆盖：dst 仍是 v1
        self.assertEqual(dst.read_bytes(), b"v1")

    def test_build_profiles_unknown_placeholder(self):
        p = {"problem_id": "cumcm-2018-B", "competition_id": "cumcm", "year": 2018,
             "problem_code": "B", "source": {"rel": "problems/x/doc.doc"}, "attachments": []}
        prof = norm.build_problem_profile(p, "a" * 64, "asset-1")
        self.assertEqual(prof["problem_types"], ["unknown"])
        self.assertEqual(prof["subproblems"], [])
        self.assertEqual(prof["label_status"], "unknown")
        self.assertIsNone(prof["confidence"])
        cp = norm.build_case_profile(
            {"case_id": "c1", "problem_id": "cumcm-2018-B", "competition_id": "cumcm"}, "b" * 64, "asset-p")
        self.assertFalse(any(cp["license"].values()))
        self.assertEqual(cp["structure"], [])
        self.assertEqual(cp["writing"], [])
        self.assertEqual(cp["figures"], [])
        self.assertIsNone(cp["length"]["zh_chars"])

    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires private seed and original corpus')
    def test_run_normalize_deterministic_bundle_and_seed_match(self):
        out1 = self.tmp / "o1"
        out2 = self.tmp / "o2"
        rep1, b1 = norm.run_normalize(Path("D:/Erdos_data"), out1)
        self.assertTrue(rep1["qc"]["passed"], rep1["qc"])
        self.assertEqual(len(b1["problems"]), 3)
        self.assertEqual(len(b1["cases"]), 7)
        self.assertEqual(len(b1["assets"]), 18)
        # 每个源/附件/论文的流式重算 hash 与旧 seed 一致
        self.assertTrue(all(f.get("seed_match") is True for f in rep1["files"]))
        rep2, b2 = norm.run_normalize(Path("D:/Erdos_data"), out2)
        self.assertEqual(
            json.dumps(b1, ensure_ascii=False, sort_keys=True),
            json.dumps(b2, ensure_ascii=False, sort_keys=True))
        # 相同输入重复运行，业务内容文件（bundle）字节一致（幂等）
        self.assertEqual((out1 / "bundle.json").read_bytes(), (out2 / "bundle.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
