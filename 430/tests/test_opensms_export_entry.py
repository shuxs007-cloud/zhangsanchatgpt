"""Test the export boundary using a fake runner; no network or business writes."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ExportEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        scripts = Path(temporary.name) / 'scripts'
        scripts.mkdir()
        self.entry = scripts / 'run_opensms_export.sh'
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'scripts' / self.entry.name, self.entry)
        fake = scripts / 'run_opensms_patrol.sh'
        fake.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\nenv | sort\n')
        fake.chmod(0o700)

    def call(self, args, env=None):
        return subprocess.run(['/bin/bash', str(self.entry), *args], env=env or os.environ,
                              text=True, capture_output=True)

    def test_default_and_json_use_normal_conditional_f_without_forced_tasks(self):
        for args in ([], ['--json']):
            result = self.call(args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[:2], ['run', '--json'])
            self.assertNotIn('--task-a-only', result.stdout)
            self.assertNotIn('--force-task-', result.stdout)

    def test_inherited_authorization_and_diagnostic_overrides_are_removed(self):
        names = [f'OPENSMS_{gate}_TASK_{task}' for task in 'BCDEF'
                 for gate in ('FORCE', 'ENABLE', 'EXECUTE')]
        names += ['OPENSMS_TASK_F_DATABASE', 'OPENSMS_TASK_F_REPAIR_MODE',
                  'OPENSMS_TASK_F_POLICY_CATALOG_SYNC', 'OPENSMS_TASK_F_BATCH_SIZE']
        env = {**os.environ, **dict.fromkeys(names, 'stale'), 'OPENSMS_DEVICE_DAILY_WORKERS': '4'}
        result = self.call(['--json'], env)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in names:
            self.assertNotIn(name + '=', result.stdout)
        self.assertIn('OPENSMS_DEVICE_DAILY_WORKERS=4', result.stdout)

    def test_conflicting_arguments_never_start_runner(self):
        for args in (['--force-task-f'], ['--task-a-only'], ['--json', '--force-task-b']):
            result = self.call(args)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')

    def test_runtime_scripts_do_not_retain_previous_domain_matchers(self):
        root = Path(__file__).resolve().parents[1]
        legacy_host = 'adminsms' + '-jiq'
        files = list(root.glob('opensms_*.mjs'))
        files += [file for file in (root / 'scripts').iterdir()
                  if file.is_file() and file.suffix in {'.mjs', '.js', '.py', '.sh'}]
        self.assertGreater(len(files), 20)
        for file in files:
            with self.subTest(file=str(file.relative_to(root))):
                self.assertNotIn(legacy_host, file.read_text(encoding='utf-8'))

    def test_core_origin_contract_uses_current_domain(self):
        root = Path(__file__).resolve().parents[1]
        origin = 'https://adminsms.smsoug.com'
        for name in (
            'opensms_protocol_client.mjs', 'opensms_payload_codec.mjs',
            'opensms_payload_readiness.mjs', 'opensms_navigation_guard.mjs',
            'scripts/opensms_login_recovery.mjs', 'scripts/opensms_login_recovery_cli.mjs',
            'scripts/opensms_export_fast_raw.mjs', 'scripts/opensms_read_server_dates_fast_raw.mjs',
            'scripts/opensms_balance_only.mjs', 'scripts/opensms_reply_rate_today.mjs',
            'scripts/opensms_api_target_guard_cli.mjs',
        ):
            with self.subTest(file=name):
                self.assertIn(origin, (root / name).read_text(encoding='utf-8'))
        policy = (root / 'scripts/OPENSMS_10DAY_PATROL_POLICY.md').read_text(encoding='utf-8')
        self.assertIn(origin + '/index', policy)
        self.assertIn(origin + '/export4day', policy)


if __name__ == '__main__':
    unittest.main()
