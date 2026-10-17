import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch
import unittest
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location('benchmark', ROOT / 'scripts/opensms_benchmark_local.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LocalBenchmarkTests(unittest.TestCase):
    def test_external_outputs_link_and_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / '430'
            external = Path(temp) / 'external'
            root.mkdir()
            external.mkdir()
            (root / 'outputs').symlink_to(external, target_is_directory=True)
            self.assertEqual(MODULE.validate_output_directory(root / 'outputs' / 'new', root), external.resolve() / 'new')
            (external / 'escape').symlink_to(root, target_is_directory=True)
            for output in (root / 'outputs', root / 'other', root / 'outputs' / 'escape' / 'new'):
                with self.assertRaises(ValueError):
                    MODULE.validate_output_directory(output, root)

    def test_recommendation_does_not_add_threads_for_noise(self):
        result = MODULE.recommend_workers({'1': 2.0, '2': 1.2, '4': 1.01, '8': 1.0})
        self.assertEqual(result['workers'], 4)
        self.assertEqual(result['fastestMeasuredWorkers'], 8)
        self.assertFalse(result['autoApplied'])
        self.assertEqual(MODULE.recommend_workers({'1': 1.1, '8': 1.0})['workers'], 8)
        self.assertIsNone(MODULE.recommend_workers({}))
        for value in (0, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                MODULE.recommend_workers({'4': value})

    def test_local_benchmark_preserves_business_details(self):
        before = {'checks': [{'name': '规则检查', 'status': 'passed', 'message': 'ok',
                             'details': {'count': 1}}]}
        after = json.loads(json.dumps(before))
        after['checks'][0]['details']['count'] = 2
        self.assertNotEqual(MODULE.audit_semantics(before), MODULE.audit_semantics(after))
        source = (ROOT / 'scripts/opensms_benchmark_local.py').read_text()
        self.assertIn("scripts = {} if args.skip_port", source)
        self.assertIn("'portBenchmarkExecuted': bool(port_results)", source)

    def test_audit_benchmark_compares_all_business_details_not_scheduler_count(self):
        spec = importlib.util.spec_from_file_location('audit_bench', ROOT / 'scripts/opensms_benchmark_audit.py')
        bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bench)
        before = {'checks': [{'name': '最终交付校验', 'status': 'passed', 'message': 'ok',
                             'details': {'hash_workers': 1, 'checksum_failures': {}}}]}
        after = json.loads(json.dumps(before))
        after['checks'][0]['details']['hash_workers'] = 8
        self.assertEqual(bench.audit_semantics(before), bench.audit_semantics(after))
        after['checks'][0]['details']['checksum_failures'] = {'a.xlsx': 'changed'}
        self.assertNotEqual(bench.audit_semantics(before), bench.audit_semantics(after))

    def test_bounded_workers(self):
        with patch.object(MODULE.os, 'cpu_count', return_value=10):
            self.assertEqual(MODULE.validate_workers('1,2,4,8,10,4'), [1, 2, 4, 8, 10])
            for value in ('0', '-1', '11', '1000000', ''):
                with self.assertRaises(ValueError):
                    MODULE.validate_workers(value)

    def test_os_profile_denies_network_and_other_writes(self):
        output = Path('/tmp/offline-only')
        policy = MODULE.sandbox_profile(output)
        self.assertIn('(deny network*)', policy)
        self.assertIn('(deny file-write*)', policy)
        self.assertIn(json.dumps(str(output.resolve())), policy)
        self.assertNotIn('opensms_artifacts/state', policy)

    def test_benchmark_has_no_live_entry_or_production_config_write(self):
        text = (ROOT / 'scripts/opensms_benchmark_local.py').read_text()
        self.assertNotIn('run_opensms_export.sh', text)
        self.assertNotIn('run_opensms_patrol.sh', text)
        self.assertNotIn('OPENSMS_FORCE_TASK_', text)
        self.assertIn("'configurationChanged': False", text)

    def test_audit_auto_is_measured_bounded_default(self):
        spec = importlib.util.spec_from_file_location('bounded_audit_test', ROOT / 'scripts/opensms_parallel_audit.py')
        audit = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = audit
        spec.loader.exec_module(audit)
        with patch.object(audit.os, 'cpu_count', return_value=10):
            self.assertEqual(audit.resolve_worker_count('auto'), 4)
            self.assertEqual(audit.resolve_worker_count('max'), 10)
            self.assertEqual(audit.resolve_worker_count('8'), 8)
            for value in ('0.5', '11', '10000', '-1'):
                with self.assertRaises(ValueError):
                    audit.resolve_worker_count(value)
        with patch.object(audit.os, 'cpu_count', return_value=1):
            self.assertEqual(audit.resolve_worker_count('auto'), 1)


if __name__ == '__main__':
    unittest.main()
