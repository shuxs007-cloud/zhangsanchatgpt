import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import opensms_build_mac_migration as migration


class MacMigrationTests(unittest.TestCase):
    def test_no_credentials_locks_or_symlink_traversal(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for name in ('good.py', 'auth.json', 'cookies.json', '.env', 'writer.lock', 'x.pyc'):
                (root / name).write_text('sample')
            (root / 'busy.lock').mkdir()
            (root / 'busy.lock/owner.json').write_text('{}')
            (root / 'profile').mkdir()
            (root / 'profile/secret').write_text('sample')
            (root / 'shortcut').symlink_to(root / 'good.py')
            self.assertEqual([p.name for p in migration.regular_files(root)], ['good.py'])

    def test_restore_script_compiles(self):
        compile(migration.RESTORE_PY, 'restore_mac.py', 'exec')

    def test_restore_verifies_and_rejects_existing_files(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / 'bundle'
            root.mkdir()
            helper = root / 'restore_mac.py'
            helper.write_text(migration.RESTORE_PY)
            sample = root / 'sample.txt'
            sample.write_text('完整性校验')
            manifest = [{'path': 'sample.txt', 'sha256': hashlib.sha256(sample.read_bytes()).hexdigest()}]
            (root / 'VERIFY_MANIFEST.json').write_text(json.dumps(manifest))
            result = subprocess.run([sys.executable, str(helper), '--verify-only'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['verified'])
            sample.write_text('changed')
            failed = subprocess.run([sys.executable, str(helper), '--verify-only'], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn('Checksum mismatch', failed.stderr)

    def test_restore_refuses_directory_escape(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / 'bundle'
            root.mkdir()
            (root / 'restore_mac.py').write_text(migration.RESTORE_PY)
            (root / 'VERIFY_MANIFEST.json').write_text(json.dumps([{'path': '../outside', 'sha256': 'x'}]))
            result = subprocess.run([sys.executable, str(root / 'restore_mac.py'), '--verify-only'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unsafe', result.stderr)


if __name__ == '__main__':
    unittest.main()
