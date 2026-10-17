"""Deterministic notification rollup and independently recalculated audit."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import opensms_task_d_notification_summary as summary
import opensms_chat_summary as chat
import opensms_finalize_patrol_report as report
import opensms_parallel_audit as audit


class NotificationSummaryTests(unittest.TestCase):
    def branch(self, phase, cards, date):
        path = f"/tmp/offline-run/{phase}.json"
        notification = {"task": "D", "phase": phase, "runId": "offline-run", "forceGate": True,
                        "automationGate": True, "serverDate": "2026-09-07", "expireDate": date,
                        "cardNos": cards, "candidateCount": len(cards)}
        batches = []
        for start in range(0, len(cards), 20):
            values = cards[start:start + 20]
            data = {"inputCount": len(values), "matchedCardCount": len(values),
                    "updatedChatCount": len(values) * 200, "notFound": []}
            batches.append({**data, "cardNos": values, "batchNumber": len(batches) + 1,
                            "expireDate": date, "businessReceiptPassed": True, "transactionalVerificationPassed": True,
                            "serverResponse": {"code": 200, "data": copy.deepcopy(data)}})
        receipt = {"task": "D", "phase": phase, "manifestPath": path, "candidateCount": len(cards),
                   "serverDate": "2026-09-07", "expireDate": date, "status": "passed" if cards else "no_candidate",
                   "verificationMode": "synchronous_transactional_business_receipt", "businessReceiptPassed": True,
                   "transactionalVerificationPassed": True, "batches": batches}
        if cards:
            receipt.update(inputCount=len(cards), matchedCardCount=len(cards), updatedChatCount=len(cards) * 200)
        return {"notificationPath": path, "notification": notification, "receipt": receipt}

    def fixture(self):
        return [self.branch("swap_zero", ["2360000001", "2360000002"], "2026-09-07"),
                self.branch("retention_verified", ["2360000002", "2360000003"], "2026-09-14")]

    def test_overlap_counts_unique_phones_not_sessions_and_later_d_branch_wins(self):
        result = summary.build_summary("offline-run", self.fixture())
        self.assertEqual((result["confirmedSubmissionCount"], result["uniqueNotifiedPhoneCount"], result["overlapPhoneCount"]), (4, 3, 1))
        self.assertEqual(result["lastTaskDNotificationByDate"], [
            {"expireDate": "2026-09-07", "phoneCount": 1}, {"expireDate": "2026-09-14", "phoneCount": 2}])
        self.assertEqual(sum(x["updatedChatCount"] for x in result["byPhase"]), 800)
        self.assertFalse(result["independentExpiryReadbackAvailable"])

    def test_empty_branches_are_confirmed_zero_without_fabricated_top_totals(self):
        branches = [self.branch("swap_zero", [], "2026-09-07"), self.branch("retention_verified", [], "2026-09-14")]
        self.assertEqual(summary.build_summary("offline-run", branches)["uniqueNotifiedPhoneCount"], 0)

    def test_zero_and_small_update_counts_still_count_exact_matched_phones(self):
        for updates in (0, 1):
            branches = self.fixture()
            for branch in branches:
                branch["receipt"]["updatedChatCount"] = updates
                for batch in branch["receipt"]["batches"]:
                    batch["updatedChatCount"] = batch["serverResponse"]["data"]["updatedChatCount"] = updates
            result = summary.build_summary("offline-run", branches)
            self.assertEqual(result["confirmedSubmissionCount"], 4)
            self.assertEqual(result["uniqueNotifiedPhoneCount"], 3)

    def test_false_success_and_identity_mismatch_rejected(self):
        mutations = [
            lambda b: b[0]["receipt"].update(status="failed"),
            lambda b: b[0]["receipt"].update(validateOnly=True),
            lambda b: b[0]["receipt"].update(manifestPath="/tmp/unrelated.json"),
            lambda b: b[0]["receipt"]["batches"][0].update(cardNos=["2360000001", "2360000009"]),
            lambda b: b[0]["receipt"]["batches"][0].update(cardNos=["2360000001", "2360000001"]),
            lambda b: b[0]["receipt"]["batches"][0]["serverResponse"]["data"].update(matchedCardCount=1),
            lambda b: b[0]["receipt"]["batches"][0]["serverResponse"]["data"].update(updatedChatCount=1),
            lambda b: b[0]["receipt"].update(updatedChatCount=123),
            lambda b: b[0]["notification"].update(runId="old-run"),
            lambda b: b[1]["notification"].update(expireDate="2026-09-15"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                branches = self.fixture()
                mutate(branches)
                with self.assertRaises(ValueError):
                    summary.build_summary("offline-run", branches)

    def test_summary_and_md_are_generated_from_json_without_receipt_reads(self):
        rollup = summary.build_summary("offline-run", self.fixture())
        result = {"status": "success", "runId": "offline-run", "taskD": {"status": "passed", "notificationSummary": rollup,
                  "retentionMatchedCardCount": 2, "filteredUnmatchedCount": 1, "neverProcessSourceExcludedCount": 0}}
        text = chat.render_summary(result)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("## ")]), 7)
        self.assertIn("去重 3 个号码", text)
        self.assertIn("未命中跳过 1（不计成功", text)
        self.assertIn("不是到期日独立查询", text)
        details = "\n".join(report.task_d_notification_lines({"runId": "offline-run", "taskDExecuted": True, "taskDNotificationSummary": rollup}))
        self.assertIn("| 2026-09-07 | 1 |", details)
        self.assertEqual(report.task_d_notification_lines({"taskDExecuted": False, "taskDNotificationSummary": rollup}), [])
        for status in ("success", "failed"):
            text = chat.render_summary({**result, "status": status, "runId": "different-run"})
            self.assertNotIn("去重 3 个号码", text)
        self.assertEqual(summary.summary_lines(rollup, "different-run"), [])

    def test_incomplete_or_inconsistent_statistics_never_break_failure_reporting(self):
        for value in ({"status": "passed"}, {**summary.build_summary("offline-run", self.fixture()), "uniqueNotifiedPhoneCount": 999}):
            self.assertEqual(summary.summary_lines(value), [])
            text = chat.render_summary({"status": "failed", "error": "later stage failed",
                                        "taskD": {"status": "passed", "notificationSummary": value}})
            self.assertIn("later stage failed", text)
            self.assertNotIn("去重 999", text)

    def test_file_hashes_and_independent_audit_detect_tampered_aggregate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            branches = self.fixture()
            # Exercise zero-update receipts through the independent hash-bound audit.
            for branch in branches:
                branch["receipt"]["updatedChatCount"] = 0
                for batch in branch["receipt"]["batches"]:
                    batch["updatedChatCount"] = batch["serverResponse"]["data"]["updatedChatCount"] = 0
            def save(name, value):
                path = root / ("offline-run_" + name)
                path.write_text(json.dumps(value), encoding="utf-8")
                return path
            source = save("source.fixture", {"synthetic": "not an actual workbook"})
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            branches[0]["notification"].update(sourcePath=str(source), sourceSha256=source_sha)
            notices, receipts = [], []
            for branch in branches:
                phase = branch["notification"]["phase"]
                notices.append(save(phase + ".json", branch["notification"]))
                branch["receipt"]["manifestPath"] = str(notices[-1])
                receipts.append(save(phase + "_receipt.json", branch["receipt"]))
            manifest = save("manifest.json", {"task": "D", "runId": "offline-run",
                "sourcePath": str(source), "sourceSha256": source_sha,
                "swapNotificationPath": str(notices[0]), "retentionNotificationPath": str(notices[1])})
            result = summary.build_from_files("offline-run", manifest, *receipts)
            self.assertEqual(len(result["evidence"]), 6)
            def config_for(value):
                path = save("rollup.json", value)
                return {"runId": "offline-run", "taskDManifest": str(manifest),
                        "taskDSwapExpiryReceipt": str(receipts[0]), "taskDRetentionExpiryReceipt": str(receipts[1]),
                        "taskDNotificationSummary": {**value, "receipt": str(path),
                                                    "receiptSha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
            class Resolver:
                def resolve(self, value, **kwargs):
                    return Path(value) if value else None
            config = config_for(result)
            self.assertEqual(audit.validate_task_d_notification_rollup(config, Resolver())["uniqueNotifiedPhoneCount"], 3)
            for key, value in (("uniqueNotifiedPhoneCount", 4), ("overlapPhoneCount", 0)):
                forged = {**result, key: value}
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "独立重算"):
                    audit.validate_task_d_notification_rollup(config_for(forged), Resolver())
            source.write_text("modified source", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                summary.build_from_files("offline-run", manifest, *receipts)
            with self.assertRaisesRegex(ValueError, "源工作簿"):
                audit.validate_task_d_notification_rollup(config_for(result), Resolver())
            data = json.loads(manifest.read_text())
            del data["runId"]
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "准备清单身份"):
                summary.build_from_files("offline-run", manifest, *receipts)

    def test_same_run_private_preparation_attempt_reuses_exact_transactional_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / 'offline-run'
            receipts = run / 'receipts'
            attempt = receipts / 'task_d_preparation_attempts' / '12345'
            attempt.mkdir(parents=True)
            source = run / 'offline-run_source.fixture'
            source.write_text('source')
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            branches = self.fixture()
            notices, transactions = [], []
            for branch in branches:
                phase = branch['notification']['phase']
                notice = attempt / f'offline-run_{phase}.json'
                transaction = receipts / f'offline-run_{phase}_receipt.json'
                if phase == 'swap_zero':
                    branch['notification'].update(sourcePath=str(source), sourceSha256=source_sha)
                branch['receipt']['manifestPath'] = str(notice)
                notice.write_text(json.dumps(branch['notification']))
                transaction.write_text(json.dumps(branch['receipt']))
                notices.append(notice)
                transactions.append(transaction)
            manifest = attempt / 'offline-run_manifest.json'
            manifest.write_text(json.dumps({'task': 'D', 'runId': 'offline-run',
                'sourcePath': str(source), 'sourceSha256': source_sha,
                'swapNotificationPath': str(notices[0]), 'retentionNotificationPath': str(notices[1])}))
            before = [p.read_bytes() for p in transactions]
            result = summary.build_from_files('offline-run', manifest, *transactions)
            self.assertEqual(result['confirmedSubmissionCount'], 4)
            self.assertEqual([p.read_bytes() for p in transactions], before)
            rollup = receipts / 'offline-run_rollup.json'
            rollup.write_text(json.dumps(result))
            config = {'runId': 'offline-run', 'taskDManifest': str(manifest),
                      'taskDSwapExpiryReceipt': str(transactions[0]),
                      'taskDRetentionExpiryReceipt': str(transactions[1]),
                      'taskDNotificationSummary': {**result, 'receipt': str(rollup),
                          'receiptSha256': hashlib.sha256(rollup.read_bytes()).hexdigest()}}
            class Resolver:
                def resolve(self, value, **kwargs):
                    return Path(value) if value else None
            self.assertEqual(audit.validate_task_d_notification_rollup(config, Resolver())['uniqueNotifiedPhoneCount'], 3)
            other = Path(temp) / 'other-run' / transactions[0].name
            other.parent.mkdir()
            other.write_bytes(before[0])
            with self.assertRaisesRegex(ValueError, '不属于本轮'):
                summary.build_from_files('offline-run', manifest, other, transactions[1])
            config['taskDSwapExpiryReceipt'] = str(other)
            with self.assertRaisesRegex(ValueError, '不是本轮目录'):
                audit.validate_task_d_notification_rollup(config, Resolver())

    def test_exact_same_run_retention_continuation_layout_and_independent_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / 'offline-run'
            root = run / 'receipts'
            root.mkdir(parents=True)
            source = run / 'offline-run_source.fixture'
            source.write_text('unchanged source')
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            notices, transactions = [], []
            for branch in self.fixture():
                phase = branch['notification']['phase']
                notice = root / f'offline-run_{phase}.json'
                if phase == 'swap_zero':
                    branch['notification'].update(sourcePath=str(source), sourceSha256=source_sha)
                branch['receipt']['manifestPath'] = str(notice)
                notice.write_text(json.dumps(branch['notification']))
                transaction = root / f'offline-run_{phase}_receipt.json'
                transaction.write_text(json.dumps(branch['receipt']))
                notices.append(notice)
                transactions.append(transaction)
            manifest = root / 'offline-run_manifest.json'
            manifest.write_text(json.dumps({'task': 'D', 'runId': 'offline-run',
                'sourcePath': str(source), 'sourceSha256': source_sha,
                'swapNotificationPath': str(notices[0]), 'retentionNotificationPath': str(notices[1])}))
            original = transactions[1].read_bytes()
            class Resolver:
                def resolve(self, value, **kwargs):
                    return Path(value) if value else None
            for directory, allowed in (
                (root / 'task_d_continuation_attempts' / '12345', True),
                (root / 'task_d_continuation_attempts' / 'not-numeric', False),
                (root / 'arbitrary' / '12345', False),
                (Path(temp) / 'other-run' / 'receipts' / 'task_d_continuation_attempts' / '12345', False),
            ):
                with self.subTest(directory=directory):
                    directory.mkdir(parents=True)
                    continued = directory / transactions[1].name
                    continued.write_bytes(original)
                    if allowed:
                        result = summary.build_from_files('offline-run', manifest, transactions[0], continued)
                        rollup = root / 'offline-run_rollup.json'
                        rollup.write_text(json.dumps(result))
                        config = {'runId': 'offline-run', 'taskDManifest': str(manifest),
                            'taskDSwapExpiryReceipt': str(transactions[0]), 'taskDRetentionExpiryReceipt': str(continued),
                            'taskDNotificationSummary': {**result, 'receipt': str(rollup),
                                'receiptSha256': hashlib.sha256(rollup.read_bytes()).hexdigest()}}
                        self.assertEqual(audit.validate_task_d_notification_rollup(config, Resolver())['uniqueNotifiedPhoneCount'], 3)
                    else:
                        with self.assertRaisesRegex(ValueError, '不属于本轮'):
                            summary.build_from_files('offline-run', manifest, transactions[0], continued)
                        with self.assertRaisesRegex(ValueError, '不是本轮目录'):
                            audit.validate_task_d_notification_rollup({**config, 'taskDRetentionExpiryReceipt': str(continued)}, Resolver())
            self.assertEqual(transactions[1].read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
