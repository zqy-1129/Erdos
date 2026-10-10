"""阶段02 契约校验回归：schema 自校验、真实注册/清单兼容、正负例、演进与不兼容检测。"""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "content_contracts", str(Path(os.path.abspath(__file__)).parents[1] / "scripts" / "validate_content_contracts.py"))
validate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validate)


class ContentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        jsonschema, D7, R, F = validate.load_jsonschema()
        cls.jsonschema = jsonschema
        cls.D7 = D7
        validate._v = (D7, R, F)
        cls.store, cls.by_name = validate.load_store(validate.SCHEMAS_DIR)
        cls.taxonomy = json.loads(validate.DEFAULT_TAXONOMY.read_text(encoding="utf-8"))

    # ---- 依赖 ---- #
    def test_dependency_available(self):
        self.assertTrue(self.jsonschema is not None)

    # ---- schema 自校验 ---- #
    def test_schemas_meta_check_passes(self):
        self.assertEqual(validate.meta_check(validate.SCHEMAS_DIR, self.D7), [])

    # ---- 真实注册表 / 分类规范兼容 ---- #
    def test_real_registry_valid(self):
        issues, n = validate.check_registry(validate.DEFAULT_REGISTRY, self.store, self.by_name)
        self.assertEqual(issues, [])
        self.assertEqual(n, 19)

    def test_real_taxonomy_valid(self):
        self.assertEqual(validate.check_taxonomy(validate.DEFAULT_TAXONOMY, self.store, self.by_name), [])

    # ---- 真实逐文件清单逐行通过 ---- #
    @unittest.skipUnless(os.environ.get('ERDOS_PRIVATE_CORPUS_TESTS')=='1', 'requires private inventory snapshot')
    def test_real_source_files_valid(self):
        issues, total, ok = validate.check_sources(validate.DEFAULT_SOURCES, self.store, self.by_name)
        self.assertEqual(issues, [])
        self.assertEqual(total, ok)
        self.assertGreater(total, 0)

    # ---- 正例全部通过 ---- #
    def test_all_positives_pass(self):
        pos, _, _ = validate.check_examples(validate.DEFAULT_EXAMPLES, self.store, self.by_name, self.taxonomy)
        for fname, issues in pos:
            self.assertEqual(issues, [], "正例 {} 未通过: {}".format(fname, issues))

    # ---- 每个负例被预期规则拒绝 ---- #
    def test_each_negative_rejected_by_expected_rule(self):
        _, neg, _ = validate.check_examples(validate.DEFAULT_EXAMPLES, self.store, self.by_name, self.taxonomy)
        self.assertGreater(len(neg), 0)
        for fname, expected, issues in neg:
            self.assertTrue(issues, "负例 {} 未被拒绝".format(fname))
            self.assertIn("{}".format(expected), "\n".join(issues),
                          "负例 {} 拒绝原因不含预期 '{}': {}".format(fname, expected, issues[:2]))

    # ---- 同题多标签 + null 未知合法 ---- #
    def test_multi_label_and_null_unknown_valid(self):
        validator = validate.make_validator(self.by_name["problem_profile"], self.store,
                                            validate._v[0], validate._v[1], validate._v[2])
        doc = {
            "problem_id": "cumcm-2024-A",
            "competition_id": "cumcm",
            "problem_types": ["optimization", "evaluation"],
            "subproblems": [
                {"subproblem_id": "cumcm-2024-A:q1", "problem_types": ["unknown"], "confidence": None, "label_status": "unknown"},
                {"subproblem_id": "cumcm-2024-A:q2", "problem_types": ["prediction"], "confidence": 0.5, "label_status": "pending_review"}
            ],
            "schema_version": 1,
            "profile_version": 1,
            "confidence": None,
            "label_status": "unknown",
            "provenance": {"kind":"unknown","evidence":None,"note":None},
            "source_sha256": None,
            "source_asset_ref": None,
            "review_status": "pending_review"
        }
        for subproblem in doc['subproblems']:
            subproblem['provenance'] = {"kind":"unknown","evidence":None,"note":None}
        self.assertEqual(validate.schema_error_strings(validator, doc), [])

    # ---- 演进：新增可选字段兼容 ---- #
    def test_evolution_add_optional_compatible(self):
        base = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}, "additionalProperties": False}
        evolved = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "additionalProperties": False}
        old_doc = {"a": "x"}
        self.assertEqual(list(self.D7(base).iter_errors(old_doc)), [])
        # 新增可选字段后，旧文档仍应通过（向前兼容）
        self.assertEqual(list(self.D7(evolved).iter_errors(old_doc)), [])

    # ---- 演进：新增必填字段为不兼容变更（旧文档被检出失效） ---- #
    def test_evolution_add_required_is_incompatible(self):
        base = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}, "additionalProperties": False}
        incompatible = {"type": "object", "required": ["a", "b"], "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "additionalProperties": False}
        old_doc = {"a": "x"}
        self.assertEqual(list(self.D7(base).iter_errors(old_doc)), [])
        errs = list(self.D7(incompatible).iter_errors(old_doc))
        self.assertTrue(errs, "新增必填字段应导致旧文档失效")


if __name__ == "__main__":
    unittest.main()
