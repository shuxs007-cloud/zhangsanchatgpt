"""Synthetic verifier tests. Never imports/runs the production patrol runner."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('fixed_rule_verifier', ROOT / 'scripts/opensms_verify_fixed_rules.py')
VERIFY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VERIFY
SPEC.loader.exec_module(VERIFY)


class ManifestTests(unittest.TestCase):
    def test_full_contains_quick_once_and_no_auto_discovery_or_business_commands(self):
        quick = VERIFY.QUICK
        full = quick + VERIFY.FULL_EXTRA
        files = [file for group in full for file in group.files]
        self.assertEqual(len(files), len(set(files)))
        self.assertTrue(set(g.name for g in quick).issubset(g.name for g in full))
        for group in full:
            command = VERIFY.command_for(group)
            self.assertNotIn('discover', command)
            self.assertNotIn('run', command)
            self.assertNotIn('--force-task-d', command)
        self.assertIn('tests/test_opensms_task_d_notification_summary.py', files)

    def test_export_status_fix_is_in_regular_offline_acceptance(self):
        files = [file for group in VERIFY.QUICK for file in group.files]
        for required in ('tests/test_opensms_bounded_status_fetch.mjs',
                         'tests/test_opensms_request_pacing.mjs',
                         'tests/test_opensms_read_retry.mjs',
                         'tests/test_opensms_export_performance.mjs',
                         'tests/test_opensms_export_failure_diagnostics.py'):
            self.assertEqual(files.count(required), 1, required)

    def test_environment_removes_inherited_authority_credentials_and_injection(self):
        with patch.dict(os.environ, {'OPENSMS_FORCE_TASK_D': '1', 'OPENSMS_ENABLE_TASK_F': '1',
                'OPENSMS_RUN_ID': 'live', 'BITBROWSER_CDP': 'http://live',
                'OPENSMS_RAW_API_TARGET_ID': 'live-page', 'NODE_OPTIONS': '--import injected.mjs',
                'PYTHONPATH': '/foreign', 'HTTPS_PROXY': 'https://secret', 'API_KEY': 'secret'}):
            env = VERIFY.isolated_environment('/private/test')
        for key in ('OPENSMS_FORCE_TASK_D', 'OPENSMS_ENABLE_TASK_F', 'OPENSMS_RUN_ID',
                    'NODE_OPTIONS', 'HTTPS_PROXY', 'API_KEY'):
            self.assertNotIn(key, env)
        self.assertEqual(env['BITBROWSER_CDP'], 'http://127.0.0.1:1')
        self.assertEqual(env['OPENSMS_EXACT_CDP_ONLY'], '1')
        self.assertEqual(env['OPENSMS_TASK_F_INDEPENDENT_LOGIN_ONLY'], '1')
        self.assertEqual(env['OPENSMS_RAW_API_TARGET_ID'], 'offline-fixed-rules')
        self.assertEqual(env['TMPDIR'], '/private/test')

    def test_missing_manifest_file_and_unsafe_limits_fail_closed(self):
        with patch.object(VERIFY, 'QUICK', (VERIFY.Group('missing', 'python', ('tests/absent.py',)),)):
            with self.assertRaises(FileNotFoundError):
                VERIFY.selected_groups('quick')
        for workers, timeout in ((0, 1), (5, 1), (1, 0), (1, float('nan')), (1, float('inf'))):
            with self.subTest(workers=workers, timeout=timeout), self.assertRaises(ValueError):
                VERIFY.run_groups((), ROOT, workers=workers, timeout=timeout)


@unittest.skipUnless(sys.platform == 'darwin' and VERIFY.SANDBOX.is_file(), 'requires macOS OS sandbox')
class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='fixed-verifier-selftest-')
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name).resolve()

    def run_commands(self, commands, workers=2, timeout=10, **kwargs):
        groups = tuple(VERIFY.Group(name, 'python', ()) for name in commands)
        return VERIFY.run_groups(groups, self.output, workers=workers, timeout=timeout,
            command_builder=lambda group: [sys.executable, '-B', '-c', commands[group.name]], **kwargs)

    def test_independent_groups_overlap_and_logs_temporary_paths_are_disjoint(self):
        code = "import os,time; print(os.environ['TMPDIR']); time.sleep(0.25); print('Ran 2 tests in 0.25s')"
        result = self.run_commands({'one': code, 'two': code})
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['peakConcurrentGroups'], 2)
        self.assertEqual(result['testCount'], 4)
        self.assertLess(result['elapsedSeconds'], sum(r['elapsedSeconds'] for r in result['results']))
        paths = [Path(r['log']).read_text().splitlines()[0] for r in result['results']]
        self.assertEqual(len(set(paths)), 2)

    def test_os_blocks_network_and_writes_outside_own_group(self):
        denied = str(self.output / 'forbidden-write')
        code = (
            'import errno,socket; from pathlib import Path\n'
            'try:\n socket.socket().connect(("127.0.0.1",1))\n'
            'except OSError as e:\n assert e.errno in (errno.EPERM,errno.EACCES), repr(e)\n'
            'else:\n raise AssertionError("network allowed")\n'
            f'try:\n Path({denied!r}).write_text("unsafe")\n'
            'except PermissionError:\n pass\n'
            'else:\n raise AssertionError("shared write allowed")\n'
        )
        result = self.run_commands({'isolation': code})
        self.assertEqual(result['status'], 'passed', result)
        self.assertFalse(Path(denied).exists())

    def test_failure_stops_dispatch_but_drains_already_running_group(self):
        result = self.run_commands({'fail': 'raise SystemExit(3)',
            'drain': 'import time; time.sleep(0.2); print("drained")',
            'never': 'raise AssertionError("must not launch")'})
        self.assertEqual([r['status'] for r in result['results']], ['failed', 'passed', 'not_started'])
        self.assertIn('drained', Path(result['results'][1]['log']).read_text())

    def test_launch_error_is_structured_and_drains_existing_process(self):
        groups = (VERIFY.Group('drain', 'python', ()), VERIFY.Group('bad', 'python', ()))
        def command(group):
            if group.name == 'bad':
                raise RuntimeError('synthetic launch error')
            return [sys.executable, '-c', 'import time; time.sleep(0.1)']
        result = VERIFY.run_groups(groups, self.output, workers=2, timeout=5, command_builder=command)
        self.assertEqual([r['status'] for r in result['results']], ['passed', 'launch_failed'])
        self.assertIn('synthetic launch error', result['results'][1]['error'])

    def test_timeout_cleans_child_process_group_and_keeps_log(self):
        child = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'
        code = ('import signal,subprocess,sys,time; '
                'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                f'p=subprocess.Popen([sys.executable,"-c",{child!r}]); '
                'print(p.pid,flush=True); time.sleep(30)')
        # Leave startup headroom when full document regression is using the CPU.
        result = self.run_commands({'timeout': code, 'never': 'pass'}, workers=1, timeout=2)
        first = result['results'][0]
        self.assertEqual(first['status'], 'timeout', result)
        self.assertTrue(first['timedOut'])
        self.assertTrue(first['cleanup']['parentReaped'])
        self.assertEqual(result['results'][1]['status'], 'not_started')
        pid = int(Path(first['log']).read_text().strip())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail(f'test child still running: {pid}')

    def test_scheduler_exception_cleans_all_active_and_preserves_result(self):
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError('synthetic scheduler error')
            return False
        result = self.run_commands({'active': 'import time; time.sleep(30)', 'never': 'pass'},
                                   workers=1, cancelled=cancelled)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('synthetic scheduler error', result['schedulerError'])
        self.assertEqual([r['status'] for r in result['results']], ['cancelled', 'not_started'])


if __name__ == '__main__':
    unittest.main()
