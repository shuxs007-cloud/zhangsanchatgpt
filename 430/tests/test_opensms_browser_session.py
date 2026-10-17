import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("browser_session", ROOT / "scripts/opensms_browser_session.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BrowserSessionTests(unittest.TestCase):
    def fixture(self, directory):
        folder = Path(directory)
        settings, status, lock = [folder / name for name in ("settings.json", "status.json", "lock.json")]
        settings.write_text(json.dumps({"browser":"cloakbrowser", "requestCompletionGapMs":1000,
                                        "requestConcurrency":1}))
        status.write_text(json.dumps({"browser":"cloakbrowser", "status":"awaiting-manual-login",
            "pid":os.getpid(), "port":12345, "sessionId":"session", "webSocketPath":"/devtools/browser/id"}))
        lock.write_text(json.dumps({"pid":os.getpid(), "token":"private-token", "browserSessionId":"session"}))
        return dict(settings_path=settings, status_path=status, lock_path=lock)

    def test_exact_new_browser_and_verified_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.fixture(directory)
            value = module.resolve_configured_browser(**args)
            self.assertEqual(value["cdp"], "http://127.0.0.1:12345")
            self.assertEqual(value["browser"], "cloakbrowser")

    def ready(self, args):
        state = json.loads(args["status_path"].read_text())
        state.update(status="ready", headless=True, authentication="verified-readonly-api")
        args["status_path"].write_text(json.dumps(state))

    def test_headless_ready_session_needs_no_new_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.fixture(directory)
            self.ready(args)
            value = module.ensure_configured_browser(**args, starter=lambda: self.fail("must not launch"))
            self.assertTrue(value["headless"])
            self.assertEqual(value["status"], "ready")

    def test_manual_or_missing_session_uses_headless_starter_once(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                args = self.fixture(directory)
                if missing:
                    args["status_path"].unlink()
                calls = []
                def start():
                    calls.append(1)
                    if missing:
                        self.fixture(directory)
                    self.ready(args)
                self.assertTrue(module.ensure_configured_browser(**args, starter=start)["headless"])
                self.assertEqual(calls, [1])

    def test_no_auto_login_retry_or_fallback_on_failed_start(self):
        for error in ("OPENSMS_MANUAL_LOGIN_REQUIRED", "OPENSMS_BROWSER_HTTP_403", "OPENSMS_AUTH_KEYCHAIN_UNAVAILABLE"):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                args = self.fixture(directory)
                calls=[]
                def start():
                    calls.append(1)
                    raise RuntimeError(error)
                with self.assertRaisesRegex(RuntimeError, error):
                    module.ensure_configured_browser(**args, starter=start)
                self.assertEqual(calls, [1])

    def test_verified_headed_browser_is_reused_without_start(self):
        with tempfile.TemporaryDirectory() as directory:
            args=self.fixture(directory);self.ready(args)
            state=json.loads(args["status_path"].read_text());state["headless"]=False
            args["status_path"].write_text(json.dumps(state))
            value=module.ensure_configured_browser(**args,starter=lambda:self.fail("live browser replaced"))
            self.assertFalse(value["headless"])
            self.assertEqual(value["pid"],os.getpid())

    def test_unconfirmed_ready_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            args=self.fixture(directory);self.ready(args)
            state=json.loads(args["status_path"].read_text());state["authentication"]="unknown"
            args["status_path"].write_text(json.dumps(state))
            with self.assertRaisesRegex(RuntimeError,"LOGIN_REQUIRED"):
                module.resolve_configured_browser(**args)

    def test_pending_login_never_restarts_business_and_requires_exact_expired_headless_session(self):
        with tempfile.TemporaryDirectory() as directory:
            args=self.fixture(directory)
            bound={"pid":os.getpid(),"sessionId":"session"}
            with patch.object(module,"STATUS",args["status_path"]),patch.dict(os.environ,{"OPENSMS_FIXED_RULES_OFFLINE":"0"}),patch.object(module.subprocess,"run") as run:
                for changes in ({"authentication":"unconfirmed-technical-error"},
                                {"authentication":"expired","headless":False},
                                {"authentication":"expired","headless":True,"manualLoginRequested":False}):
                    state={**bound,"manualLoginRequested":True,**changes}
                    args["status_path"].write_text(json.dumps(state))
                    self.assertIsNone(module.show_pending_manual_login(bound))
                run.assert_not_called()
                state={**bound,"authentication":"expired","headless":True,"manualLoginRequested":True}
                args["status_path"].write_text(json.dumps(state))
                run.return_value.stdout='{"status":"login-required"}'
                self.assertFalse(module.show_pending_manual_login(bound)["businessRestarted"])
                self.assertEqual(run.call_args.args[0][-1],"login")
                self.assertIsNone(module.show_pending_manual_login({**bound,"sessionId":"other"}))
                self.assertEqual(run.call_count,1)

    def test_invalid_fixed_settings_never_start_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            args=self.fixture(directory)
            args["settings_path"].write_text(json.dumps({"browser":"other"}))
            with self.assertRaisesRegex(RuntimeError,"SETTINGS_INVALID"):
                module.ensure_configured_browser(**args,starter=lambda:self.fail("must not launch"))

    def test_missing_browser_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.fixture(directory)
            args["status_path"].unlink()
            with self.assertRaisesRegex(RuntimeError, "LOGIN_REQUIRED"):
                module.resolve_configured_browser(**args)

    def test_closed_changed_or_dead_session_rejected(self):
        for field, value in (("pid", "12345"), ("port", 0), ("status", "closed"),
                             ("sessionId", "different"), ("webSocketPath", "/wrong")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                args = self.fixture(directory)
                state = json.loads(args["status_path"].read_text())
                state[field] = value
                args["status_path"].write_text(json.dumps(state))
                with self.assertRaisesRegex(RuntimeError, "LOGIN_REQUIRED"):
                    module.resolve_configured_browser(**args)
        with tempfile.TemporaryDirectory() as directory:
            args = self.fixture(directory)
            with self.assertRaisesRegex(RuntimeError, "LOGIN_REQUIRED"):
                module.resolve_configured_browser(**args, process_check=lambda pid: (_ for _ in ()).throw(ProcessLookupError()))

    def test_new_browser_transport_has_no_fallback_or_owner_secrets_in_state(self):
        runner = (ROOT / "scripts/opensms_patrol_runner.py").read_text()
        self.assertIn('if key != "owner"', runner)
        self.assertIn('child_env["OPENSMS_EXACT_CDP_ONLY"] = "1"', runner)
        self.assertIn('child_env["OPENSMS_FORCE_RAW_CDP"] = "1"', runner)
        self.assertIn('report_lease = SiteExecutionLease(file=REPORT_LOCK', runner)
        self.assertIn('OPENSMS_SITE_JOB_OWNER_GONE', (ROOT / "opensms_site_execution.mjs").read_text())


if __name__ == "__main__":
    unittest.main()
