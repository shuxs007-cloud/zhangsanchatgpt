import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("site_execution", ROOT / "scripts/opensms_site_execution.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SiteExecutionTests(unittest.TestCase):
    def test_two_jobs_cannot_acquire_same_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "browser.lock"
            first = module.SiteExecutionLease(file=file).acquire()
            try:
                self.assertEqual(json.loads(file.read_text())["pid"], os.getpid())
                with self.assertRaisesRegex(RuntimeError, "OPENSMS_SITE_LOCK_TIMEOUT"):
                    module.SiteExecutionLease(file=file, timeout_seconds=0).acquire()
                self.assertTrue(file.exists())
            finally:
                first.release()
            second = module.SiteExecutionLease(file=file).acquire()
            self.assertTrue(second.environment["OPENSMS_SITE_LOCK_TOKEN"])
            second.release()
            self.assertFalse(file.exists())

    def test_dead_owner_is_never_stolen(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "browser.lock"
            file.write_text(json.dumps({"pid":12345,"token":"dead"}))
            with patch.object(module.os, "kill", side_effect=ProcessLookupError):
                with self.assertRaisesRegex(RuntimeError, "ABANDONED"):
                    module.SiteExecutionLease(file=file).acquire()
            self.assertEqual(json.loads(file.read_text())["token"], "dead")

    def test_valid_browser_lease_is_borrowed_not_released_by_export(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "browser.lock"
            owner = {"pid":os.getpid(), "token":"private", "browserSessionId":"one"}
            file.write_text(json.dumps(owner))
            lease = module.SiteExecutionLease(file=file, inherited_owner=owner).acquire()
            self.assertTrue(lease.inherited)
            lease.release()
            self.assertEqual(json.loads(file.read_text()), owner)

    def test_changed_browser_session_cannot_be_borrowed(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "browser.lock"
            owner = {"pid":os.getpid(), "token":"private", "browserSessionId":"one"}
            file.write_text(json.dumps({**owner, "browserSessionId":"two"}))
            with self.assertRaisesRegex(RuntimeError, "OWNER_CHANGED"):
                module.SiteExecutionLease(file=file, inherited_owner=owner).acquire()

    def test_damaged_lock_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "browser.lock"
            file.write_text("partial")
            with self.assertRaisesRegex(RuntimeError, "TIMEOUT"):
                module.SiteExecutionLease(file=file, timeout_seconds=0).acquire()
            self.assertEqual(file.read_text(), "partial")

    def test_replaced_owner_is_not_released(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "browser.lock"
            lease = module.SiteExecutionLease(file=file).acquire()
            file.write_text(json.dumps({"pid":os.getpid(),"token":"other"}))
            with self.assertRaisesRegex(RuntimeError, "OWNER_CHANGED"):
                lease.release()
            self.assertTrue(file.exists())

    def test_binding_node_children_inherit_python_lease_without_deadlock(self):
        node = ROOT / "scripts/opensms_patrol_runner.py"
        runtime = Path("/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / f"{module.BROWSER_ID}.lock"
            lease = module.SiteExecutionLease(file=file).acquire()
            try:
                program = """import {acquireSiteLock} from '/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs';
                  const lease=await acquireSiteLock({browserId:process.argv[1],directory:process.argv[2],timeoutMs:0});
                  console.log(JSON.stringify({inherited:lease.inherited}));lease.release();"""
                result = subprocess.run([str(runtime),"--input-type=module","-e",program,module.BROWSER_ID,directory],
                    env={**os.environ,**lease.environment},capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertTrue(json.loads(result.stdout)["inherited"])
                self.assertTrue(file.exists())
            finally:
                lease.release()

    def test_production_entrypoints_use_one_job_and_request_lock(self):
        protocol = (ROOT / "opensms_protocol_client.mjs").read_text()
        for name in ("createRawBackgroundPage", "connectRawOpenSmsPage", "connectOpenSmsBrowser"):
            body = protocol.split(f"export async function {name}",1)[1].split("\n}",1)[0]
            self.assertIn("await ensureOpenSmsSiteOwnership();", body)
        pacing = (ROOT / "opensms_request_pacing.mjs").read_text()
        wrapper = pacing.split("export const withOpenSmsRequestPacing =",1)[1]
        self.assertLess(wrapper.index("await ensureOpenSmsSiteOwnership();"), wrapper.index("return productionPacer("))
        self.assertEqual(wrapper.count("await ensureOpenSmsSiteOwnership();"),2)
        runner = (ROOT / "scripts/opensms_patrol_runner.py").read_text()
        main = runner.split("def main()",1)[1]
        self.assertLess(main.index("SiteExecutionLease("), main.index("runner = PatrolRunner("))
        self.assertIn("OPENSMS_PACING_REQUEST_UNCONFIRMED", main)


if __name__ == "__main__":
    unittest.main()
