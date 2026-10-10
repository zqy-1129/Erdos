"""独立回归：失败清单、稳定哈希与所有来源检查的链接边界。"""
import hashlib
import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'inventory_under_test', str(Path(os.path.abspath(__file__)).parents[1] / 'scripts' / 'inventory_competitions.py'))
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


class InventoryRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='inventory_regression_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'data'
        self.file = self.root / 'problems' / '01_fa' / 'sample.bin'
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b'original')
        self.registry = inventory._mini_registry()

    def test_discovery_stat_failure_retains_unknown_entry(self):
        original = inventory._entry_stat
        def denied(entry):
            if entry.name == 'sample.bin':
                raise PermissionError('fixture denied')
            return original(entry)
        with patch.object(inventory, '_entry_stat', denied):
            result = inventory.run_scan(self.root, self.registry)
        record = next(r for r in result['records'] if r['relative_path'].endswith('sample.bin'))
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['entry_kind'], 'unknown')
        self.assertIsNone(record['sha256'])
        self.assertEqual(result['stats']['failed_files'], 1)

    def test_unreadable_nested_directory_is_reported(self):
        blocked = self.file.parent / 'blocked'
        blocked.mkdir()
        real = inventory._scandir_entries
        def denied(path):
            if Path(path) == blocked:
                raise PermissionError('fixture denied')
            return real(path)
        with patch.object(inventory, '_scandir_entries', denied):
            result = inventory.run_scan(self.root, self.registry)
        self.assertTrue(any(i['code'] == 'dir_enumerate_failed' and i['severity'] == 'error'
                            for i in result['issues']))

    def test_discovery_then_change_uses_new_stable_metadata(self):
        original = inventory.discover_files
        content = b'changed-content-with-different-size'
        def discover_then_change(*args):
            found = original(*args)
            self.file.write_bytes(content)
            return found
        with patch.object(inventory, 'discover_files', discover_then_change):
            result = inventory.run_scan(self.root, self.registry)
        record = result['records'][0]
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['size'], len(content))
        self.assertEqual(record['sha256'], hashlib.sha256(content).hexdigest())

    def test_change_during_read_is_not_cached(self):
        original = inventory._stream_sha256
        def mutate_after_hash(path):
            digest = original(path)
            with Path(path).open('ab') as handle:
                handle.write(b'new bytes')
            return digest
        with patch.object(inventory, '_stream_sha256', mutate_after_hash):
            result = inventory.run_scan(self.root, self.registry)
        self.assertEqual(result['records'][0]['status'], 'unstable')
        self.assertFalse(result['cache_updated'])

    def test_cached_metadata_change_falls_back_to_read(self):
        cache = Path(self.temp.name) / 'cache.json'
        inventory.run_scan(self.root, self.registry, cache_path=cache)
        original = inventory._safe_stat
        first = [True]
        content = b'changed after cache comparison'
        def change_after_first_stat(path):
            st = original(path)
            if first[0]:
                first[0] = False
                self.file.write_bytes(content)
            return st
        with patch.object(inventory, '_safe_stat', change_after_first_stat):
            result = inventory.run_scan(self.root, self.registry, 'cached', cache_path=cache)
        record = result['records'][0]
        self.assertEqual(record['hash_source'], 'full')
        self.assertEqual(record['size'], len(content))
        self.assertEqual(record['sha256'], hashlib.sha256(content).hexdigest())

    def _assert_source_link_is_never_enumerated(self, linked):
        original = os.lstat
        def fake_link(path, *args, **kwargs):
            if Path(path) == linked:
                return inventory._FakeStat(stat.S_IFDIR, file_attributes=inventory.FILE_ATTRIBUTE_REPARSE_POINT)
            return original(path, *args, **kwargs)
        with patch.object(inventory.os, 'lstat', fake_link), patch.object(inventory, 'dir_nonempty') as enumerate_dir:
            issues = inventory.empty_and_missing(self.root, self.registry)
        enumerate_dir.assert_not_called()
        self.assertTrue(any(i['code'] == 'reparse_skipped' for i in issues))

    def test_top_source_link_is_skipped_by_empty_check(self):
        self._assert_source_link_is_never_enumerated(self.root / 'problems')

    def test_nested_source_link_is_skipped_by_empty_check(self):
        self._assert_source_link_is_never_enumerated(self.file.parent)

    def test_registry_root_escape_is_rejected(self):
        state, _ = inventory.source_path_state(self.root, self.root / '..' / 'outside')
        self.assertEqual(state, 'error')

    def test_portable_symlink_stat_is_recognized(self):
        self.assertTrue(inventory._is_link_stat(inventory._FakeStat(stat.S_IFLNK)))


if __name__ == '__main__':
    unittest.main()
