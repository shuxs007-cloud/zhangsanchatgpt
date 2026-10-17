"""Offline provenance/scope regressions for restored Chatr Google dates."""
from copy import deepcopy
import contextlib
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

import test_opensms_no_google_audit_report as fixtures


AUDIT, REPORT = fixtures.AUDIT, fixtures.REPORT


class ChatrGoogleAuditReportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NoGoogleAuditReportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.config = self.fixture.root, self.fixture.config
        self.source = self.root / "fresh_google_chatr.xlsx"
        self.download_path = self.root / "fresh_google_chatr_download.json"
        self.snapshot_path = self.root / "chatr_google_dates.json"
        workbook = Workbook()
        workbook.remove(workbook.active)
        for sheet in ("二號機房", "三號機房", "四號機房"):
            ws = workbook.create_sheet(sheet)
            ws.append(["卡槽", "電信商/價格", "激活日期", "預計過期日"])
        workbook["二號機房"].append(["204_62.01", "Chatr 20", "2026-09-01", "2026-09-30"])
        workbook["二號機房"].append(["204_63", "Kooda 20", "2026-08-01", "2026-08-30"])
        workbook.save(self.source)
        workbook.close()
        source_hash = AUDIT.sha256_file(self.source)
        self.download = {"runId": "offline-current", "startedAt": "2026-09-07T00:01:00+08:00",
                         "downloadedAt": "2026-09-07T00:02:00+08:00",
                         "sourceUrl": AUDIT.CHATR_GOOGLE_SOURCE_URL,
                         "outputPath": str(self.source), "sha256": source_hash, "size": self.source.stat().st_size}
        row = {"normalizedSlot": "204_62", "rawSlot": "204_62.01", "carrier": "Chatr 20",
               "sourceSheet": "二號機房", "sourceRow": 2,
               "activationDate": "2026-09-01", "cutoffDate": "2026-09-30",
               "rawActivationDate": "2026-09-01", "rawCutoffDate": "2026-09-30"}
        self.snapshot = {"schemaVersion": 1, "status": "passed", "runId": "offline-current",
                         "createdAt": "2026-09-07T00:03:00+08:00", "source": "google_chatr_dates",
                         "sourcePath": str(self.source), "sourceSha256": source_hash,
                         "readOnly": True, "snapshotConsistent": True, "sourceHashVerified": True,
                         "sourceSheets": ["二號機房", "三號機房", "四號機房"],
                         "sourceColumns": {sheet: {name: index for index, name in enumerate(
                             ("卡槽", "電信商/價格", "激活日期", "預計過期日"), start=1)}
                             for sheet in ("二號機房", "三號機房", "四號機房")},
                         "recordCount": 1, "records": [{**row, "provenance": [row]}],
                         "totalSourceRows": 2, "chatrSourceRows": 1, "ignoredNonChatrRows": 1,
                         "excludedInvalidSlotRows": 0}
        self.config.update({"googleChatrDateFallbackEnabled": True,
                            "googleChatrDateFallbackScope": AUDIT.CHATR_GOOGLE_DATE_SCOPE,
                            "chatrDocumentDateSource": AUDIT.CHATR_GOOGLE_DATE_SOURCE,
                            "runStartedAt": "2026-09-07T00:00:00+08:00",
                            "chatrGoogleSource": str(self.source), "chatrGoogleSourceSha256": source_hash,
                            "chatrGoogleDownloadReceipt": str(self.download_path),
                            "chatrGoogleDates": str(self.snapshot_path)})
        self.config["commandTimings"].extend({"command": name, "exitCode": 0, "durationSeconds": 1}
                                             for name in sorted(AUDIT.CHATR_GOOGLE_COMMANDS))
        self.proof = {"enabled": True, "status": "passed", "run_id": "offline-current",
                      "scope": AUDIT.CHATR_GOOGLE_DATE_SCOPE, "source": AUDIT.CHATR_GOOGLE_DATE_SOURCE,
                      "snapshot_path": str(self.snapshot_path), "source_path": str(self.source),
                      "source_sha256": source_hash, "record_count": 1,
                      "field_counts": {"激活日期": 1, "截止日": 0}, "matched_rows": 1, "filled_rows": 1,
                      "chatr_rows": 1, "unmatched_rows": 0, "missing_activation_rows": 0, "missing_cutoff_rows": 0}
        resolution = self.fixture.card["process"]["date_resolution"]
        resolution["source"] = AUDIT.CHATR_GOOGLE_RESOLUTION_SOURCE
        resolution["field_counts"] = {
            "激活日期": {"system_export": 0, "local_cache": 0, "google": 0, "google_chatr": 1, "missing": 0},
            "截止日": {"system_export": 1, "local_cache": 0, "google": 0, "google_chatr": 0, "missing": 0}}
        self.bind_receipts()

    def bind_receipts(self):
        fixtures.write_json(self.download_path, self.download)
        fixtures.write_json(self.snapshot_path, self.snapshot)
        self.config["chatrGoogleDownloadReceiptSha256"] = AUDIT.sha256_file(self.download_path)
        snapshot_hash = AUDIT.sha256_file(self.snapshot_path)
        self.config["chatrGoogleDatesSha256"] = self.proof["snapshot_sha256"] = snapshot_hash
        process, verification = self.fixture.card["process"], self.fixture.card["verification"]
        process["google_chatr_date_fallback"] = deepcopy(self.proof)
        process["date_resolution"]["chatr_google_dates"] = deepcopy(self.proof)
        verification["google_chatr_date_fallback_review"] = {**deepcopy(self.proof), "matches": True}
        verification["date_resolution_review"]["chatr_google_dates"] = deepcopy(self.proof)
        verification["date_resolution_review"]["field_counts"] = deepcopy(process["date_resolution"]["field_counts"])
        self.fixture.dispatch["inputs"]["chatr_google_dates"] = {"path": str(self.snapshot_path), "sha256": snapshot_hash}
        self.fixture.save_receipts()

    def review(self):
        self.fixture.save_receipts()
        return AUDIT.check_google_dependency(self.config, self.fixture.resolver, self.fixture.inputs)

    def test_current_chatr_scope_passes_without_restoring_full_association(self):
        result = self.review()
        self.assertEqual(AUDIT.SKIP, result.status, result.message)
        self.assertTrue(result.details["chatr_date_fallback_enabled"])
        proof = AUDIT.check_chatr_google_provenance(self.config, self.fixture.resolver, self.fixture.inputs)
        self.assertEqual(AUDIT.PASS, proof.status, proof.message)
        self.assertEqual(3, len(proof.details["allowed_artifact_paths"]))
        self.assertEqual(AUDIT.PASS, AUDIT.check_card_receipt(self.fixture.card_path, False).status)
        self.assertEqual(AUDIT.PASS, AUDIT.check_date_resolution(self.config, self.fixture.resolver, self.fixture.inputs).status)

    def test_explicit_boolean_scope_is_required_before_opening_google_files(self):
        for value in (False, None, "true", 1):
            with self.subTest(value=value), patch.dict(self.config, {"googleChatrDateFallbackEnabled": value}), \
                 patch.object(AUDIT, "sha256_file", side_effect=AssertionError("forbidden Google read")):
                self.assertEqual(AUDIT.FAIL, self.review().status)

    def test_missing_or_failed_download_command_is_rejected(self):
        self.config["commandTimings"][-1]["exitCode"] = 1
        self.assertEqual(AUDIT.FAIL, self.review().status)

    def test_old_run_and_old_download_timestamp_are_rejected(self):
        for mutation in ({"runId": "previous"}, {"sourceUrl": "https://docs.google.com/spreadsheets/d/other/export?format=xlsx"},
                         {"downloadedAt": "2026-09-06T00:02:00+08:00"},
                         {"startedAt": "2026-09-06T00:01:00+08:00"}):
            with self.subTest(mutation=mutation):
                original = deepcopy(self.download)
                self.download.update(mutation)
                self.bind_receipts()
                self.assertEqual(AUDIT.FAIL, self.review().status)
                self.download = original

    def test_tampered_source_snapshot_or_download_hash_rejected(self):
        for key in ("chatrGoogleSourceSha256", "chatrGoogleDatesSha256", "chatrGoogleDownloadReceiptSha256"):
            with self.subTest(key=key), patch.dict(self.config, {key: "0" * 64}):
                self.assertEqual(AUDIT.FAIL, self.review().status)

    def test_rehashed_snapshot_kooda_and_forged_dates_rejected_against_raw_google(self):
        for key, value in (("carrier", "Kooda"), ("activationDate", "2026-08-01"), ("sourceRow", 3)):
            with self.subTest(key=key):
                original = deepcopy(self.snapshot)
                self.snapshot["records"][0][key] = value
                self.snapshot["records"][0]["provenance"][0][key] = value
                self.bind_receipts()
                self.assertEqual(AUDIT.FAIL, self.review().status)
                self.snapshot = original

    def test_omitted_chatr_rows_and_forged_source_counts_rejected(self):
        self.snapshot["chatrSourceRows"] = 2
        self.bind_receipts()
        self.assertEqual(AUDIT.FAIL, self.review().status)
        self.snapshot["chatrSourceRows"] = 1
        self.snapshot["recordCount"] = 0
        self.snapshot["records"] = []
        self.proof["record_count"] = 0
        self.bind_receipts()
        self.assertEqual(AUDIT.FAIL, self.review().status)

    def test_scope_broadened_to_kooda_or_open_card_date_rejected(self):
        counts = self.fixture.card["process"]["date_resolution"]["field_counts"]
        for field, category in (("激活日期", "google"), ("开卡日期", "google_chatr")):
            with self.subTest(field=field):
                original = deepcopy(counts)
                counts.setdefault(field, {})[category] = 1
                self.assertEqual(AUDIT.FAIL, self.review().status)
                counts.clear()
                counts.update(original)

    def test_forged_main_chatr_counts_rejected(self):
        for key, value in (("chatr_rows", 2), ("unmatched_rows", 1), ("filled_rows", 2),
                           ("missing_activation_rows", 2), ("missing_cutoff_rows", -1)):
            with self.subTest(key=key):
                original = deepcopy(self.proof)
                self.proof[key] = value
                self.bind_receipts()
                self.assertEqual(AUDIT.FAIL, self.review().status)
                self.proof = original

    def test_mismatched_independent_proof_or_dispatch_snapshot_rejected(self):
        self.fixture.card["verification"]["google_chatr_date_fallback_review"]["run_id"] = "old"
        self.assertEqual(AUDIT.FAIL, self.review().status)
        self.bind_receipts()
        self.fixture.dispatch["inputs"]["chatr_google_dates"]["sha256"] = "0" * 64
        self.assertEqual(AUDIT.FAIL, self.review().status)

    def test_old_google_artifact_still_rejected_with_current_chatr_enabled(self):
        (self.root / "old_google.xlsx").write_bytes(b"old")
        result = self.review()
        self.assertEqual(AUDIT.FAIL, result.status)
        self.assertEqual(["old_google.xlsx"], result.details["unexpected_artifacts"])

    def test_delivery_admits_only_verified_current_three_artifacts(self):
        proof = AUDIT.check_chatr_google_provenance(self.config, self.fixture.resolver, self.fixture.inputs)
        (self.root / "FILES.tsv").write_text("path\n")
        (self.root / "SHA256SUMS.txt").write_text("\n".join(
            AUDIT.sha256_file(Path(path)) + "  " + Path(path).name for path in proof.details["allowed_artifact_paths"]))
        result = AUDIT.check_delivery(self.root, google_disabled=True,
                                      allowed_google_paths=proof.details["allowed_artifact_paths"])
        self.assertEqual(AUDIT.PASS, result.status, result.message)
        self.assertEqual(AUDIT.FAIL, AUDIT.check_delivery(self.root, google_disabled=True).status)

    def test_parallel_consumers_share_only_this_invocation_source_scan(self):
        tasks = AUDIT.build_tasks(self.config, self.fixture.resolver, self.fixture.inputs, 4)
        by_name = {task.name: task for task in tasks}
        original = AUDIT.check_chatr_google_provenance
        with patch.object(AUDIT, "check_chatr_google_provenance", wraps=original) as checker:
            for name in ("Chatr Google日期补缺来源", "Google依赖关闭", "最终交付校验"):
                by_name[name].fn()
            self.assertEqual(1, checker.call_count)
            new_tasks = AUDIT.build_tasks(self.config, self.fixture.resolver, self.fixture.inputs, 4)
            next(task for task in new_tasks if task.name == "Chatr Google日期补缺来源").fn()
            self.assertEqual(2, checker.call_count)

    def test_report_explains_date_only_scope_and_real_source_counts(self):
        self.config.update({"taskAOnly": True, "taskSelection": "A", "taskFStageStatus": "skipped",
                            "taskFSkippedReason": "task_a_only", "offlineRecoveryEnabled": False,
                            "runTime": "offline", "taskId": "offline", "taskDuration": "1s", "exportWorkflow": "fixture"})
        fixtures.write_json(self.fixture.config_path, self.config)
        output = self.root / "report.md"
        with patch.object(sys, "argv", ["report", str(self.fixture.config_path), str(output)]), contextlib.redirect_stdout(io.StringIO()):
            REPORT.main()
        rendered = output.read_text()
        for text in ("Google全面关联：已关闭", "Chatr来源 1 行", "排除非Chatr 1 行", "1 个唯一卡槽",
                     "激活日期 1，截止日 0", "Kooda空日期仅从生成前冻结F库补", "开卡日期留空", "system_source_only",
                     "本轮主表Chatr 1 行", "未匹配 0 行", "仍缺激活日期 0 行", "仍缺截止日 0 行"):
            self.assertIn(text, rendered)
        self.assertNotIn("未下载、未读取Google关联表", rendered)
        self.assertNotIn("Chatr空激活日期允许保留", rendered)
        self.assertEqual(3, sum(line.startswith("[打开") for line in rendered.splitlines()))


class RealChatrDocumentAuditTests(unittest.TestCase):
    def check_actual_receipts(self, *, distinct_paths=False, iccid_conflict=False):
        import test_opensms_chatr_document_fallback as document
        fixture = document.ChatrDocumentFallbackTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if iccid_conflict:
            fixture.set_conflicting_source_iccid()
        generated = fixture.fixture.build()
        root = fixture.fixture.root
        snapshot = json.loads(fixture.snapshot.read_text())
        created = AUDIT.parse_iso(snapshot["createdAt"])
        source_hash = AUDIT.sha256_file(fixture.source)
        snapshot_hash = AUDIT.sha256_file(fixture.snapshot)
        source_archive, snapshot_archive, downloaded_source = fixture.source, fixture.snapshot, fixture.source
        if distinct_paths:
            delivery = root / "delivery"
            delivery.mkdir()
            original_download = root / "downloads"
            original_download.mkdir()
            downloaded_source = original_download / "opensms_google_card_slots_original.xlsx"
            downloaded_source.write_bytes(fixture.source.read_bytes())
            source_archive = delivery / "OpenSMS_Chatr日期Google源_renamed.xlsx"
            source_archive.write_bytes(fixture.source.read_bytes())
            snapshot_archive = delivery / "opensms_chatr_google_dates_renamed.json"
            snapshot_archive.write_bytes(fixture.snapshot.read_bytes())
        download = {"runId": snapshot["runId"], "startedAt": (created - timedelta(seconds=2)).isoformat(),
                    "downloadedAt": (created - timedelta(seconds=1)).isoformat(),
                    "sourceUrl": AUDIT.CHATR_GOOGLE_SOURCE_URL,
                    "outputPath": str(downloaded_source), "sha256": source_hash, "size": fixture.source.stat().st_size}
        download_path, card_path, dispatch_path = root / "google_download.json", root / "card.json", root / "dispatch.json"
        fixtures.write_json(download_path, download)
        fixtures.write_json(card_path, generated)
        fixtures.write_json(dispatch_path, {"inputs": {"chatr_google_dates": {
            "path": str(fixture.snapshot), "sha256": snapshot_hash}}})
        config = {"runId": snapshot["runId"], "googleAssociationEnabled": False,
                  "googleDisabledReason": "user_disabled", "googleChatrDateFallbackEnabled": True,
                  "googleChatrDateFallbackScope": AUDIT.CHATR_GOOGLE_DATE_SCOPE,
                  "chatrDocumentDateSource": AUDIT.CHATR_GOOGLE_DATE_SOURCE,
                  "chatrGoogleSource": str(source_archive), "chatrGoogleSourceSha256": source_hash,
                  "chatrGoogleDownloadReceipt": str(download_path),
                  "chatrGoogleDownloadReceiptSha256": AUDIT.sha256_file(download_path),
                  "chatrGoogleDates": str(snapshot_archive), "chatrGoogleDatesSha256": snapshot_hash,
                  "reportDateCachePath": str(fixture.fixture.cache),
                  "reportDateCacheSha256": AUDIT.sha256_file(fixture.fixture.cache),
                  "commandTimings": [{"command": name, "exitCode": 0} for name in AUDIT.CHATR_GOOGLE_COMMANDS]}
        if distinct_paths:
            config.update(processingChatrGoogleSource=str(fixture.source),
                          processingChatrGoogleDates=str(fixture.snapshot))
        resolver = AUDIT.PathResolver(root / "config.json", root)
        inputs = {"card_receipt": card_path, "source": fixture.fixture.raw, "association": None,
                  "dispatch_receipt": dispatch_path}
        checks = [AUDIT.check_card_receipt(card_path, False), AUDIT.check_date_resolution(config, resolver, inputs),
                  AUDIT.check_chatr_google_provenance(config, resolver, inputs)]
        self.assertTrue(all(check.status == AUDIT.PASS for check in checks),
                        [(check.name, check.message) for check in checks])
        return fixture, config, resolver, inputs, downloaded_source

    def test_actual_generator_and_independent_verifier_receipt_passes_audit(self):
        self.check_actual_receipts()

    def test_actual_iccid_conflict_passes_audit_and_md_lists_system_winner(self):
        fixture, config, resolver, inputs, _ = self.check_actual_receipts(iccid_conflict=True)
        receipt = json.loads(inputs["card_receipt"].read_text())
        lines = REPORT.chatr_google_date_report_lines(config, receipt)
        rendered = "\n".join(lines)
        for expected in ("ICCID冲突：1行", "ICCID冲突_系统为准", "8912230200081836799F",
                         "8912230200081836702", "creation_date_fallback", "2026-10-05T00:00:00"):
            self.assertIn(expected, rendered)

    def test_actual_different_download_working_and_renamed_archive_paths_pass(self):
        fixture, config, resolver, inputs, original = self.check_actual_receipts(distinct_paths=True)
        for path in (fixture.source, fixture.snapshot, original):
            with self.subTest(path=path):
                prior = path.read_bytes()
                path.write_bytes(prior + b"changed")
                self.assertEqual(AUDIT.FAIL, AUDIT.check_chatr_google_provenance(config, resolver, inputs).status)
                path.write_bytes(prior)
        config["processingChatrGoogleSource"] = str(original)
        self.assertEqual(AUDIT.FAIL, AUDIT.check_chatr_google_provenance(config, resolver, inputs).status)


class ChatrArchiveLogTests(unittest.TestCase):
    def test_only_hash_bound_exact_same_run_chatr_command_archives_are_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            run_id = '20261010T221537+0800'
            base = Path(temp).resolve()
            delivery = base / run_id
            root = delivery / 'receipts'
            directory = root / 'task_d_continuation_attempts' / '12345'
            directory.mkdir(parents=True)
            log_root = base / 'logs'
            logs = log_root / run_id
            logs.mkdir(parents=True)
            source = logs / 'google_chatr_download.log'
            source.write_text('original authorized Chatr command log')
            archive = directory / '013_previous_google_chatr_download.log'
            archive.write_bytes(source.read_bytes())
            item = {'source': str(source), 'archive': str(archive), 'sha256': AUDIT.sha256_file(source)}
            attempt = {'authorizationSource': 'current-user', 'attemptDirectory': str(directory), 'archive': [item]}
            state = {'runId': run_id, 'delivery': str(delivery), 'taskDContinuationAttempts': [attempt]}
            state_path = root / f'opensms_patrol_runner_state_{run_id}.json'
            state_path.write_text(json.dumps(state))
            config = {'runId': run_id, 'googleAssociationEnabled': False,
                'googleChatrDateFallbackEnabled': True, 'googleChatrDateFallbackScope': AUDIT.CHATR_GOOGLE_DATE_SCOPE}
            resolver = AUDIT.PathResolver(root / 'config.json', delivery)
            def check():
                return AUDIT.verified_chatr_continuation_logs(config, resolver, log_root=log_root)
            self.assertEqual(check(), [str(archive)])
            archive.write_text('changed archival log')
            self.assertEqual(check(), [])
            archive.write_bytes(source.read_bytes())
            attempt['authorizationSource'] = 'timer'
            state_path.write_text(json.dumps(state))
            self.assertEqual(check(), [])
            attempt['authorizationSource'] = 'current-user'
            arbitrary = directory / '013_previous_google_association.log'
            arbitrary.write_bytes(source.read_bytes())
            item['archive'] = str(arbitrary)
            state_path.write_text(json.dumps(state))
            self.assertEqual(check(), [])
            item['archive'] = str(archive)
            state_path.write_text(json.dumps(state))
            config['googleChatrDateFallbackEnabled'] = False
            self.assertEqual(check(), [])


if __name__ == "__main__":
    unittest.main()
