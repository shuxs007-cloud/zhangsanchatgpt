"""Offline version-two contracts and version-one compatibility."""
import json
from pathlib import Path
import tempfile
import unittest

from opensms_task_d_policy import removed_population_fields
from opensms_parallel_audit import check_task_d_retention_counts
from opensms_task_d_report_details import build_details, render_details
from opensms_chat_summary import render_summary
from opensms_finalize_patrol_report import task_d_retention_lines


def config():
    return {"taskD" + key[0].upper() + key[1:]: value
            for key, value in removed_population_fields().items()} | {
                "taskDRetentionCarrierTransferEnabled": False}


class RemovedB14Tests(unittest.TestCase):
    def test_audit_requires_disabled_population_without_old_receipts(self):
        current = config()
        self.assertEqual(check_task_d_retention_counts(current, None).status, "passed")
        for key, value in (("taskDRetentionPopulationEnabled", True),
                           ("taskDRetentionPopulationReadExecuted", True),
                           ("taskDRetentionSummary", {"totalInRetentionCount": 0}),
                           ("taskDRetentionSummaryReceipt", "old.json"),
                           ("taskDRetentionCarrierTransferEnabled", True),
                           ("groups", [{"route": "task_d_retain_chatr_to_b14_c"}])):
            with self.subTest(key=key):
                self.assertEqual(check_task_d_retention_counts(current | {key: value}, None).status, "failed")

    def test_chat_and_full_report_omit_population_including_later_failure(self):
        task = removed_population_fields() | {"status": "passed", "executed": True,
            "retentionCarrierTransferEnabled": False,
            "notificationSummary": {"status": "passed"}}
        for status in ("success", "failed"):
            text = render_summary({"status": status, "taskD": task})
            self.assertEqual(sum(line.startswith("## ") for line in text.splitlines()), 7)
            self.assertNotIn("B14", text)
            self.assertNotIn("保留区统计未确认", text)
            self.assertNotIn("执行后保留区", text)
        self.assertEqual(task_d_retention_lines(config()), [])

    def test_detail_checks_only_two_branches_not_deleted_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            def save(name, data):
                path = Path(tmp) / f"{name}_offline.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                return str(path)
            paths = {}
            for phase, field in (("swap_zero", "swapNotificationPath"),
                                 ("retention_verified", "retentionNotificationPath")):
                paths[field] = save(phase, {"task": "D", "phase": phase,
                    "expireDate": "2026-10-07", "candidateCount": 0, "cardNos": []})
            manifest = save("manifest", {"task": "D", "runId": "offline", **paths})
            value = build_details(config() | {"taskDCurrentUserAuthorized": True,
                "runId": "offline", "taskDManifest": manifest})
            self.assertEqual(value["status"], "passed")
            self.assertEqual(value["warnings"], [])
            self.assertTrue(value["retentionPopulationRemoved"])
            self.assertNotIn("保留区号码明细", "\n".join(render_details(value)))


if __name__ == "__main__":
    unittest.main()
