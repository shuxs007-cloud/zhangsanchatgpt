"""Offline shell dispatch tests; all successful dispatches use local stubs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TaskWrapperTests(unittest.TestCase):
    def invoke(self, script, args, *, env=None):
        return subprocess.run(["bash", str(script), *args], text=True,
                              capture_output=True, env=env, timeout=10)

    def test_a_dispatches_only_a_and_clears_inherited_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory) / "scripts"
            scripts.mkdir()
            wrapper = scripts / "run_opensms_task_a.sh"
            wrapper.write_bytes((ROOT / "scripts" / wrapper.name).read_bytes())
            stub = scripts / "run_opensms_patrol.sh"
            stub.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\"\nenv | sort\n")
            stub.chmod(0o755)
            gates = [f"OPENSMS_FORCE_TASK_{letter}" for letter in "BCDEF"]
            gates += [f"OPENSMS_{action}_TASK_{letter}"
                      for action in ("ENABLE", "EXECUTE") for letter in "DEF"]
            gates += ["OPENSMS_TASK_F_POLICY_CATALOG_SYNC"]
            result = self.invoke(wrapper, ["--json"], env={**os.environ, **dict.fromkeys(gates, "1")})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[:3], ["run", "--task-a-only", "--json"])
            for key in gates:
                self.assertNotIn(key + "=", result.stdout)

    def test_a_rejects_all_extra_modes_before_dispatch(self):
        for args in (["--force-task-f"], ["--json", "--force-task-b"], ["run"],
                     ["--resume-state", "/no-file"], ["--task-f-only"]):
            with self.subTest(args=args):
                result = self.invoke(ROOT / "scripts/run_opensms_task_a.sh", args)
                self.assertEqual(result.returncode, 2)

    def test_a_help_does_not_dispatch(self):
        result = self.invoke(ROOT / "scripts/run_opensms_task_a.sh", ["--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("B/C/D/E/F全部跳过", result.stdout)

    def test_f_help_and_invalid_authorization_do_not_dispatch(self):
        script = ROOT / "scripts/run_opensms_task_f.sh"
        result = self.invoke(script, ["--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("status --state", result.stdout)
        for args in (["sync-export"], ["sync-export", "--authorization-source", "timer"],
                     ["status"], ["status", "--state", "relative.json", "--json"],
                     ["status", "--state", "/unused", "--force-task-f"]):
            with self.subTest(args=args):
                self.assertEqual(self.invoke(script, args).returncode, 2)

    def test_f_status_dispatch_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory) / "scripts"
            scripts.mkdir()
            wrapper = scripts / "run_opensms_task_f.sh"
            wrapper.write_bytes((ROOT / "scripts" / wrapper.name).read_bytes())
            (scripts / "opensms_task_f_progress_status.py").write_text(
                "import json,sys\nprint(json.dumps({'argv':sys.argv[1:],'readOnly':True}))\n")
            state = str(Path(directory) / "state.json")
            result = self.invoke(wrapper, ["status", "--state", state, "--json"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {
                "argv": ["--state", state, "--json"], "readOnly": True})


if __name__ == "__main__":
    unittest.main()
