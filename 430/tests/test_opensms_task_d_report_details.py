"""Offline per-phone report contracts; no browser or business task execution."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import opensms_task_d_report_details as details


class NotificationDetailsTests(unittest.TestCase):
    def fixture(self):
        phones = ["4160000001", "4160000002", "4160000003"]
        notification = {"task": "D", "phase": "swap_zero", "expireDate": "2026-09-08", "candidateCount": 3, "cardNos": phones}
        raw = {"code": 200, "data": {"inputCount": 2, "matchedCardCount": 2, "updatedChatCount": 6, "notFound": []}}
        batch = {"batchNumber": 1, "cardNos": phones[:2], "expireDate": notification["expireDate"],
                 "serverResponse": raw, **raw["data"], "businessReceiptPassed": True, "transactionalVerificationPassed": True}
        receipt = {"task": "D", "phase": "swap_zero", "expireDate": notification["expireDate"], "candidateCount": 3,
                   "status": "failed", "batches": [batch]}
        return notification, receipt

    def test_confirmed_partial_success_is_kept_and_unsubmitted_is_not_failure(self):
        n, r = self.fixture()
        rows = details.notification_rows(n, r)
        self.assertEqual(["success", "success", "unprocessed"], [row["status"] for row in rows])
        self.assertEqual(["4160000001", "4160000002", "4160000003"], [row["phone"] for row in rows])

    def test_timeout_pending_is_uncertain_not_failed_or_success(self):
        n, r = self.fixture()
        r["pendingBatch"] = {"batchNumber": 2, "cardNos": [n["cardNos"][-1]], "expireDate": n["expireDate"]}
        self.assertEqual(["success", "success", "uncertain"], [row["status"] for row in details.notification_rows(n, r)])

    def test_zero_update_raw_receipt_confirms_no_change_without_replay(self):
        n, r = self.fixture()
        batch = r["batches"].pop()
        batch["serverResponse"]["data"]["updatedChatCount"] = 0
        r["pendingBatch"] = {"batchNumber": 1, "cardNos": batch["cardNos"],
                             "expireDate": n["expireDate"], "response": batch["serverResponse"]}
        rows = details.notification_rows(n, r)
        self.assertEqual(["success", "success", "unprocessed"], [row["status"] for row in rows])
        self.assertIn("backend_matched_no_change", rows[0]["reason"])

    def test_raw_transaction_not_found_is_explicit_failure_only_for_that_phone(self):
        n, r = self.fixture()
        r["batches"] = []
        r["pendingBatch"] = {"batchNumber": 1, "cardNos": n["cardNos"][:2], "expireDate": n["expireDate"],
                             "response": {"code": 200, "data": {"inputCount": 2, "matchedCardCount": 1,
                                 "updatedChatCount": 6, "notFound": ["4160000002"]}}}
        self.assertEqual(["uncertain", "failed", "unprocessed"], [row["status"] for row in details.notification_rows(n, r)])
        r["pendingBatch"]["response"]["data"]["inputCount"] = 7
        self.assertEqual(["uncertain", "uncertain", "unprocessed"], [row["status"] for row in details.notification_rows(n, r)])

    def test_http_200_or_candidate_count_alone_cannot_prove_success(self):
        n, r = self.fixture()
        r["batches"][0]["serverResponse"] = {"code": 200}
        self.assertNotIn("success", [row["status"] for row in details.notification_rows(n, r)])
        self.assertTrue(all(row["status"] == "unprocessed" for row in details.notification_rows(n)))

    def test_foreign_duplicate_or_wrong_date_batches_rejected(self):
        n, r = self.fixture()
        for field, value in (("cardNos", ["4160000099"]), ("cardNos", ["4160000001", "4160000001"]),
                             ("expireDate", "2026-09-09"), ("batchNumber", 3)):
            changed = deepcopy(r)
            changed["batches"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                details.notification_rows(n, changed)

    def test_validate_only_never_claims_notifications(self):
        n, r = self.fixture()
        r["validateOnly"] = True
        with self.assertRaises(ValueError):
            details.notification_rows(n, r)

    def test_unauthorized_or_a_only_does_not_read_any_files(self):
        for config in ({}, {"taskDExecuted": True, "taskAOnly": True},
                       {"taskDExecuted": True, "taskDStatus": "skipped_not_authorized"}):
            with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read")):
                self.assertEqual("skipped", details.build_details(config)["status"])

    def test_render_lists_every_status_and_retention_phones(self):
        rows = [{"phone": str(4160000000 + i), "phase": "swap_zero", "expireDate": "2026-09-08",
                 "status": state, "reason": "fixture"} for i, state in enumerate(details.STATUS)]
        result = {"status": "passed", "rows": rows, "warnings": [], "retentionRows": [
            {"phone": "4160000099", "carrier": "B14-T快下保留卡", "new": True, "sourceCarrier": "B1", "asOf": "offline"}]}
        text = "\n".join(details.render_details(result))
        for state in details.STATUS.values():
            self.assertIn(state + "：1 号次", text)
        self.assertIn("| 4160000099 | B14-T快下保留卡 | 是 | B1 |", text)
        self.assertIn("不是提交失败", text)

    def test_cross_run_manifest_rejected_without_using_its_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task_d_old_run_manifest.json"
            path.write_text(json.dumps({"task": "D", "runId": "old_run"}), encoding="utf-8")
            result = details.build_details({"taskDCurrentUserAuthorized": True, "runId": "new_run", "taskDManifest": str(path)})
            self.assertEqual("incomplete", result["status"])
            self.assertEqual([], result["rows"])

    def test_public_labels_are_exact(self):
        self.assertEqual("换号区下卡通知", details.TASK_D_LABEL)
        self.assertEqual("过期卡发送下卡通知", details.TASK_E_LABEL)

    def test_unconfigured_detail_requires_raw_bound_proof_and_is_not_success(self):
        from test_opensms_task_d_filtered_audit import retention_fixture, AUDIT
        receipt = retention_fixture()
        row = receipt['filteredQueryProof']['queries'][0]['pages'][0]['rows'][1]
        row.update(carrireIdentif=None, displayStatus='未配置运营商', showAble=True,
                   showInTable=True, userAble=False, slotIdentif='210_18.01')
        fields = dict(unconfiguredCarrierSkippedCount=1, unconfiguredCarrierSkippedCards=[row['cardNo']])
        receipt.update(**fields, unconfiguredCarrierSkippedRows=[dict(cardNo=row['cardNo'],
            backendId=row['id'], slot=row['slotIdentif'], opr=row['opr'], reason='carrier_unconfigured')])
        with tempfile.TemporaryDirectory() as directory:
            def save(name, value):
                path = Path(directory) / (name + '_offline.json')
                path.write_text(json.dumps(value), encoding='utf-8')
                return str(path)
            source = save('source', receipt)
            notification = dict(task='D', phase='retention_verified', expireDate='2026-09-14',
                candidateCount=251, cardNos=sorted(AUDIT.validate_task_d_filtered_partition(receipt, 'offline')['actionable']),
                retentionReceiptSha256=AUDIT.sha256_file(Path(source)))
            manifest = dict(task='D', runId='offline', **fields, retentionReceiptPath=source,
                retentionNotificationPath=save('retention', notification),
                swapNotificationPath=save('swap', dict(task='D', phase='swap_zero', expireDate='2026-09-07', candidateCount=0, cardNos=[])))
            config = dict(taskDCurrentUserAuthorized=True, runId='offline', taskDManifest=save('manifest', manifest))
            result = details.build_details(config)
            self.assertEqual(1, result['counts']['unconfigured'])
            self.assertEqual(0, result['successUniquePhoneCount'])
            self.assertEqual([row['cardNo']], [r['phone'] for r in result['rows'] if r['status'] == 'unconfigured'])
            notification['retentionReceiptSha256'] = 'wrong'
            save('retention', notification)
            rejected = details.build_details(config)
            self.assertNotIn('unconfigured', rejected['counts'])
            self.assertTrue(any('哈希' in warning for warning in rejected['warnings']))


if __name__ == "__main__":
    unittest.main()
