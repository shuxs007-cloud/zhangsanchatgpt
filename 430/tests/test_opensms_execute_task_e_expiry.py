import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = Path("/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
SCRIPT = ROOT / "scripts" / "opensms_execute_task_e_expiry.mjs"


class TaskEExecutorTests(unittest.TestCase):
    def build_manifest(self, root: Path) -> tuple[Path, Path, Path]:
        source = root / "source.xlsx"
        source.write_bytes(b"task-e-empty-source")
        ledger = root / "state" / "ledger.json"
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "task": "E",
            "forceGate": True,
            "automationGate": True,
            "sourcePath": str(source),
            "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "ledgerPath": str(ledger),
            "ledgerSha256Before": None,
            "ledgerDedupeKey": "phone",
            "eligibleExpiryStatuses": ["6天", "快过期", "已过期"],
            "candidateCount": 0,
            "dateGroupCount": 0,
            "groups": [],
        }, ensure_ascii=False), encoding="utf-8")
        return manifest, source, ledger

    def run_executor(self, manifest: Path, output: Path, ledger: Path, env: dict[str, str]):
        return subprocess.run(
            [str(NODE), str(SCRIPT), str(manifest), "test", str(output), str(ledger)],
            env={**os.environ, **env},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_off_rejects_before_any_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest, _, ledger = self.build_manifest(root)
            output = root / "receipts"
            completed = self.run_executor(manifest, output, ledger, {})
            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(output.exists())
            self.assertFalse(ledger.exists())

    def test_empty_manifest_is_receipted_without_browser_or_ledger_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest, _, ledger = self.build_manifest(root)
            output = root / "receipts"
            completed = self.run_executor(manifest, output, ledger, {
                "OPENSMS_ENABLE_TASK_E": "1",
                "OPENSMS_FORCE_TASK_E": "1",
                "OPENSMS_EXECUTE_TASK_E": "1",
                "BITBROWSER_CDP": "http://127.0.0.1:1",
            })
            self.assertEqual(0, completed.returncode, completed.stderr)
            receipt = json.loads((output / "opensms_task_e_test_receipt.json").read_text(encoding="utf-8"))
            self.assertEqual("no_candidate", receipt["status"])
            self.assertEqual(20, receipt["markerBatchSize"])
            self.assertEqual(0, receipt["submittedCount"])
            self.assertEqual(0, receipt["notWrittenCount"])
            self.assertEqual([], receipt["notWrittenCards"])
            self.assertTrue(receipt["transactionalVerificationPassed"])
            self.assertTrue(receipt["ledgerVerificationPassed"])
            self.assertFalse(ledger.exists())

    def test_blank_carrier_candidate_rejected_before_browser_and_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest, _, ledger = self.build_manifest(root)
            payload = json.loads(manifest.read_text())
            payload.update({"candidateCount": 1, "dateGroupCount": 1, "groups": [{
                "expireDate": "2026-09-02", "count": 1, "cardNos": ["4160000001"],
                "rows": [{"电话": "4160000001", "运营商": "  ",
                          "过期状态": "已过期", "截止日": "2026-09-02"}],
            }]})
            manifest.write_text(json.dumps(payload))
            output = root / "receipts"
            completed = self.run_executor(manifest, output, ledger, {
                "OPENSMS_ENABLE_TASK_E": "1", "OPENSMS_FORCE_TASK_E": "1",
                "OPENSMS_EXECUTE_TASK_E": "1", "BITBROWSER_CDP": "http://127.0.0.1:1",
            })
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("候选运营商为空", completed.stderr)
            self.assertFalse(output.exists())
            self.assertFalse(ledger.exists())


if __name__ == "__main__":
    unittest.main()
