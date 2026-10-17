"""Offline raw-query partition, exact candidates, and skip-count reporting."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("task_d_filtered_audit_test", SCRIPTS / "opensms_parallel_audit.py")
AUDIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


def retention_fixture():
    phones = [f"416{index:07d}" for index in range(398)]
    rows = [{"id": f"id-{index}", "cardNo": phone, "opr": "Chatr" if index < 71 else "Koodo",
             "carrireIdentif": "protect-c" if index == 0 else "protect-t" if index == 71 else "normal"}
            for index, phone in enumerate(phones[:254])]
    queries = []
    for filter_value, group in (("chatr", "chatr"), ("koo", "kooda"), ("telus1", "kooda"), ("kini", "kooda")):
        hits = [deepcopy(row) for row in rows if filter_value in row["opr"].lower()]
        queries.append({"method": "POST", "endpoint": "/phone/config/list", "filter": filter_value,
                        "oprClass": group, "cardNos": phones.copy(), "pages": [
                            {"pageNum": 1, "pageSize": 500, "code": 200, "total": len(hits), "rows": hits}]})
    return {"runId": "offline", "task": "D", "sourceProvenance": "phone_card_retain_all_pages",
            "endpoint": "/phone/cardRetain/list", "pageSize": 200, "pageCount": 2,
            "pages": [{"pageNum": 1, "rowCount": 200}, {"pageNum": 2, "rowCount": 198}],
            "totalRecords": 398, "fetchedRecords": 398, "uniqueCardCount": 398,
            "records": [{"cardNo": phone} for phone in phones], "cardNos": phones,
            "filteredQueryProof": {"version": 1, "queries": queries},
            "filteredUnmatchedCards": phones[254:], "filteredUnmatchedCount": 144,
            "carrierConfigs": [{"id": 1, "identif": "normal", "name": "A21"},
                               {"id": 2, "identif": "protect-c", "name": "快过期续费C"},
                               {"id": 3, "identif": "protect-t", "name": "快过期续费T"}],
            "neverProcessSourceExcludedCount": 2, "neverProcessSourceExcludedRows": [
                {"cardNo": phones[index], "sourceCarrierName": name, "sourceCarrierIdentif": carrier,
                 "backendId": f"id-{index}"}
                for index, name, carrier in ((0, "快过期续费C", "protect-c"), (71, "快过期续费T", "protect-t"))]}


class FilteredPartitionTests(unittest.TestCase):
    def test_future_sixth_partition_is_independently_rebuilt_and_legacy_remains_strict(self):
        receipt = retention_fixture()
        row = receipt['filteredQueryProof']['queries'][0]['pages'][0]['rows'][1]
        row['carrireIdentif'] = None
        row['slotIdentif'] = '210_18.01'
        policy = 'skip_row_data_anomalies_v1'
        receipt['filteredQueryProof']['dataAnomalyPolicy'] = policy
        receipt.update(dataAnomalyPolicy=policy, dataAnomalySkippedCount=1,
            dataAnomalySkippedCards=[row['cardNo']], dataAnomalySkippedRows=[dict(
                cardNo=row['cardNo'], backendId=row['id'], slot=row['slotIdentif'],
                opr=row['opr'], sourceCarrierIdentif='', reason='carrier_identity_missing')])
        result = AUDIT.validate_task_d_filtered_partition(receipt, 'offline')
        self.assertEqual({row['cardNo']}, result['dataAnomalies'])
        self.assertEqual(251, len(result['actionable']))
        self.assertEqual(result['source'], result['actionable'] | result['protected'] | result['unmatched'] |
                         result['unconfigured'] | result['retired'] | result['dataAnomalies'])
        for mutation in ('count', 'reason', 'id', 'slot', 'policy', 'conflict', 'duplicate_config'):
            changed = deepcopy(receipt)
            if mutation == 'count': changed['dataAnomalySkippedCount'] = 0
            elif mutation == 'reason': changed['dataAnomalySkippedRows'][0]['reason'] = 'unmatched'
            elif mutation == 'id': changed['dataAnomalySkippedRows'][0]['backendId'] = 'wrong'
            elif mutation == 'slot': changed['dataAnomalySkippedRows'][0]['slot'] = 'wrong'
            elif mutation == 'policy': changed['filteredQueryProof'].pop('dataAnomalyPolicy')
            elif mutation == 'conflict': changed['filteredQueryProof']['queries'][0]['pages'][0]['rows'][1].update(carrireIdentif='one', carrierIdentif='two')
            elif mutation == 'duplicate_config': changed['carrierConfigs'].append(dict(identif='normal', name='duplicate'))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                AUDIT.validate_task_d_filtered_partition(changed, 'offline')

    def test_retired_b14_is_fifth_exact_partition_with_independent_identity_checks(self):
        receipt = retention_fixture()
        declared = []
        for query_index, source, name in ((0, '2093378431675928577', 'B14-C快下保留卡'),
                                          (1, '2088852494502465536', 'B14-T快下保留卡')):
            row = receipt['filteredQueryProof']['queries'][query_index]['pages'][0]['rows'][1]
            row.update(carrireIdentif=source, slotIdentif='210_18.01')
            declared.append(dict(cardNo=row['cardNo'], backendId=row['id'], slot=row['slotIdentif'],
                opr=row['opr'], sourceCarrierIdentif=source, retiredCarrierName=name, reason='carrier_retired_b14'))
        receipt.update(retiredCarrierSkippedRows=declared, retiredCarrierSkippedCount=2,
                       retiredCarrierSkippedCards=[row['cardNo'] for row in declared])
        result = AUDIT.validate_task_d_filtered_partition(receipt, 'offline')
        self.assertEqual(2, len(result['retired']))
        self.assertEqual(250, len(result['actionable']))
        self.assertEqual(result['source'], result['actionable'] | result['protected'] |
                         result['unmatched'] | result['unconfigured'] | result['retired'])
        for mutation in ('missing_rows', 'count', 'cards', 'slot', 'source', 'name', 'alias', 'opr', 'reappeared'):
            changed = deepcopy(receipt)
            row = changed['filteredQueryProof']['queries'][0]['pages'][0]['rows'][1]
            if mutation == 'missing_rows': changed.pop('retiredCarrierSkippedRows')
            elif mutation == 'count': changed['retiredCarrierSkippedCount'] = 0
            elif mutation == 'cards': changed['retiredCarrierSkippedCards'] = []
            elif mutation == 'slot': changed['retiredCarrierSkippedRows'][0]['slot'] = 'wrong'
            elif mutation == 'source': row['carrireIdentif'] = 'unknown-non-b14'
            elif mutation == 'name': row['carrierName'] = 'A21'
            elif mutation == 'alias': row['carrierIdentif'] = 'normal'
            elif mutation == 'opr': row['opr'] = 'Koodo'
            elif mutation == 'reappeared': changed['carrierConfigs'].append(dict(identif=row['carrireIdentif'], name='B14-C快下保留卡'))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                AUDIT.validate_task_d_filtered_partition(changed, 'offline')

    def test_explicit_unconfigured_is_fourth_partition_and_cannot_hide_invalid_identity(self):
        receipt = retention_fixture()
        row = receipt['filteredQueryProof']['queries'][0]['pages'][0]['rows'][1]
        row.update(carrireIdentif=None, displayStatus='未配置运营商', showAble=True,
                   showInTable=True, userAble=False, slotIdentif='210_18.01')
        receipt.update(unconfiguredCarrierSkippedCount=1, unconfiguredCarrierSkippedCards=[row['cardNo']],
                       unconfiguredCarrierSkippedRows=[dict(cardNo=row['cardNo'], backendId=row['id'],
                           slot=row['slotIdentif'], opr=row['opr'], reason='carrier_unconfigured')])
        result = AUDIT.validate_task_d_filtered_partition(receipt, 'offline')
        self.assertEqual({row['cardNo']}, result['unconfigured'])
        self.assertEqual(251, len(result['actionable']))
        self.assertEqual(result['source'], result['actionable'] | result['protected'] | result['unmatched'] | result['unconfigured'])
        for patch_fields in ({'id': None}, {'userAble': 'false'}, {'name': '快过期续费C'},
                             {'carrireIdentif': {}}, {'carrireIdentif': 0}, {'carrireIdentif': 'normal'}):
            changed = deepcopy(receipt)
            changed['filteredQueryProof']['queries'][0]['pages'][0]['rows'][1].update(patch_fields)
            with self.subTest(patch_fields), self.assertRaises(ValueError):
                AUDIT.validate_task_d_filtered_partition(changed, 'offline')
        for field, value in (('unconfiguredCarrierSkippedCount', 0), ('unconfiguredCarrierSkippedCards', []),
                             ('unconfiguredCarrierSkippedRows', [])):
            with self.subTest(field), self.assertRaises(ValueError):
                AUDIT.validate_task_d_filtered_partition({**receipt, field: value}, 'offline')

    def test_398_sources_254_filtered_hits_144_skipped_two_protected(self):
        result = AUDIT.validate_task_d_filtered_partition(retention_fixture(), "offline")
        self.assertEqual((398, 254, 144, 2, 252), tuple(len(result[key]) for key in
                         ("source", "matched", "unmatched", "protected", "actionable")))
        self.assertEqual((70, 182), (len(result["byOpr"]["chatr"]), len(result["byOpr"]["kooda"])))

    def test_same_backend_identity_across_t_aliases_is_deduplicated(self):
        receipt = retention_fixture()
        query = receipt["filteredQueryProof"]["queries"][1]
        row = query["pages"][0]["rows"][1]
        row["opr"] = "Koodo Telus1"
        receipt["filteredQueryProof"]["queries"][2]["pages"][0].update(total=1, rows=[deepcopy(row)])
        self.assertEqual(254, len(AUDIT.validate_task_d_filtered_partition(receipt, "offline")["matched"]))

    def test_empty_source_requires_no_business_candidates(self):
        receipt = retention_fixture()
        receipt.update(cardNos=[], records=[], totalRecords=0, fetchedRecords=0, uniqueCardCount=0,
                       pageCount=1, pages=[{"pageNum": 1, "rowCount": 0}],
                       filteredQueryProof={"version": 1, "queries": []}, filteredUnmatchedCards=[],
                       filteredUnmatchedCount=0, neverProcessSourceExcludedRows=[], neverProcessSourceExcludedCount=0)
        self.assertFalse(AUDIT.validate_task_d_filtered_partition(receipt, "offline")["actionable"])

    def test_malformed_or_incomplete_proofs_fail_closed(self):
        for mutation in ("old_run", "missing_filter", "batch_gap", "batch_overlap", "batch_501", "missing_page",
                         "source_short", "query_total", "query_code", "duplicate", "outside", "wrong_filter",
                         "ambiguous_opr", "cross_identity", "bool_id", "object_id", "float_id", "unsafe_id", "object_carrier_id",
                         "false_unmatched", "missing_unmatched", "false_protection", "protected_id",
                         "unresolved_carrier", "deleted_carrier"):
            with self.subTest(mutation=mutation):
                receipt = retention_fixture()
                queries = receipt["filteredQueryProof"]["queries"]
                page = queries[0]["pages"][0]
                row = page["rows"][0]
                if mutation == "old_run": receipt["runId"] = "old"
                elif mutation == "missing_filter": queries.pop()
                elif mutation == "batch_gap": queries[2]["cardNos"].pop()
                elif mutation == "batch_overlap": queries.append(deepcopy(queries[2]))
                elif mutation == "batch_501": queries[0]["cardNos"].extend(f"647{x:07d}" for x in range(103))
                elif mutation == "missing_page": receipt["pages"].pop()
                elif mutation == "source_short": receipt["records"].pop()
                elif mutation == "query_total": page["total"] += 1
                elif mutation == "query_code": page["code"] = 500
                elif mutation == "duplicate": page["rows"][1] = deepcopy(row)
                elif mutation == "outside": row["cardNo"] = "9999999999"
                elif mutation == "wrong_filter": queries[1]["filter"] = "Kooda"
                elif mutation == "ambiguous_opr": row["opr"] = "Chatr Koodo"
                elif mutation == "cross_identity":
                    other = queries[1]["pages"][0]["rows"][1]
                    other["opr"] = "Koodo Telus1"
                    queries[2]["pages"][0].update(total=1, rows=[{**other, "id": "other-id"}])
                elif mutation == "bool_id": row["id"] = True
                elif mutation == "object_id": row["id"] = {"id": 1}
                elif mutation == "float_id": row["id"] = 1.5
                elif mutation == "unsafe_id": row["id"] = 9007199254740992
                elif mutation == "object_carrier_id": row["carrireIdentif"] = {"id": 1}
                elif mutation == "false_unmatched": receipt["filteredUnmatchedCards"][0] = row["cardNo"]
                elif mutation == "missing_unmatched": receipt["filteredUnmatchedCards"].pop()
                elif mutation == "false_protection": receipt["neverProcessSourceExcludedRows"][0]["cardNo"] = receipt["cardNos"][1]
                elif mutation == "protected_id": receipt["neverProcessSourceExcludedRows"][0]["backendId"] = "invented"
                elif mutation == "unresolved_carrier": receipt["carrierConfigs"].pop()
                elif mutation == "deleted_carrier": receipt["carrierConfigs"][0]["delData"] = True
                with self.assertRaises(ValueError):
                    AUDIT.validate_task_d_filtered_partition(receipt, "offline")


class FullTaskDAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opensms-d-filtered-audit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.receipt = retention_fixture()
        self.partition = AUDIT.validate_task_d_filtered_partition(self.receipt, "offline")
        receipt_path = self.save("retention.json", self.receipt)
        self.config = {"runId": "offline", "serverDate": "2026-09-07", "taskDExecuted": True,
                       "taskDCurrentUserAuthorized": True, "taskDRetentionReceipt": receipt_path, "groups": []}
        self.manifest = {"task": "D", "forceGate": True, "automationGate": True,
                         "readOnlyPreparationConcurrent": True, "serverDate": "2026-09-07",
                         "swapExpireDate": "2026-09-07", "retentionExpireDate": "2026-09-14",
                         "retentionUniqueCardCount": 398, "retentionMatchedCardCount": 254,
                         "retentionActionableCardCount": 252, "filteredUnmatchedCount": 144,
                         "filteredUnmatchedCards": sorted(self.partition["unmatched"]),
                         "neverProcessSourceExcludedCount": 2,
                         "neverProcessSourceExcludedCards": sorted(self.partition["protected"]),
                         "chatrCandidateCount": 70, "koodaCandidateCount": 182}
        self.config.update({"taskD" + key[0].upper() + key[1:]: self.manifest[key]
                            for key in ("retentionUniqueCardCount", "retentionMatchedCardCount", "retentionActionableCardCount",
                                        "filteredUnmatchedCount", "filteredUnmatchedCards", "neverProcessSourceExcludedCount",
                                        "neverProcessSourceExcludedCards", "chatrCandidateCount", "koodaCandidateCount")})
        for group, suffix, target in (("chatr", "chatr_to_b14_c", "B14-C快下保留卡"),
                                      ("kooda", "kooda_to_b14_t", "B14-T快下保留卡")):
            rows = [{"cardNo": phone, **self.partition["matched"][phone], "expectedTargetCarrierName": target}
                    for phone in sorted(self.partition["byOpr"][group])]
            self.config["groups"].append({"title": "任务D " + group, "candidate": self.save(group + ".json", {
                "task": "D", "forceGate": True, "automationGate": True, "route": "task_d_retain_" + suffix,
                "sourceProvenance": "phone_card_retain_all_pages", "targetCarrierName": target,
                "rows": rows, "candidates": rows, "count": len(rows), "candidateCount": len(rows)})})
        self.manifest["swapNotificationPath"] = self.save("swap.json", {"phase": "swap_zero", "expireDate": "2026-09-07", "count": 0, "rows": []})
        self.manifest["retentionNotificationPath"] = self.save("notification.json", {
            "task": "D", "forceGate": True, "automationGate": True, "phase": "retention_verified",
            "expireDate": "2026-09-14", "cardNos": sorted(self.partition["actionable"]), "count": 252,
            "retentionReceiptPath": receipt_path, "retentionReceiptSha256": AUDIT.sha256_file(Path(receipt_path))})
        for key, count in (("taskDSwapExpiryReceipt", 0), ("taskDRetentionExpiryReceipt", 252)):
            self.config[key] = self.save(key + ".json", {"status": "passed", "businessReceiptPassed": True,
                "transactionalVerificationPassed": True, "independentReadbackAvailable": False,
                "verificationMode": "synchronous_transactional_business_receipt", "batches": [
                    {"inputCount": count, "matchedCardCount": count, "notFound": []}]})

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def audit(self):
        self.config["taskDManifest"] = self.save("manifest.json", self.manifest)
        path = Path(self.save("config.json", self.config))
        return AUDIT.check_task_d(self.config, AUDIT.PathResolver(path, self.root))

    def test_valid_partition_does_not_block_144_skipped_cards(self):
        result = self.audit()
        self.assertEqual(AUDIT.PASS, result.status, result.message)
        self.assertEqual(144, result.details["filtered_unmatched_count"])
        self.assertEqual(252, result.details["carrier_candidate_count"])

    def notification_only(self):
        self.config.update(taskDRetentionCarrierTransferEnabled=False, taskDCarrierCandidateCount=0,
                           taskDCarrierActualWrite=0, groups=[], taskDChatrCandidateCount=0, taskDKoodaCandidateCount=0)
        self.manifest.update(retentionCarrierTransferEnabled=False, retentionCarrierTransferStatus="removed_by_user",
                             carrierCandidateCount=0, chatrCandidateCount=0, koodaCandidateCount=0)
        path = Path(self.manifest["retentionNotificationPath"])
        notification = json.loads(path.read_text())
        notification.update(retentionCarrierTransferEnabled=False, retentionCarrierTransferStatus="removed_by_user")
        self.save(path.name, notification)

    def test_removed_transfer_audits_notifications_without_carrier_groups(self):
        self.notification_only()
        result = self.audit()
        self.assertEqual(AUDIT.PASS, result.status, result.message)
        self.assertEqual(0, result.details["carrier_candidate_count"])
        self.assertEqual(252, result.details["retention_actionable_cards"])

    def test_unconfigured_partition_is_verified_across_manifest_notification_and_report(self):
        self.notification_only()
        row = self.receipt['filteredQueryProof']['queries'][0]['pages'][0]['rows'][1]
        row.update(carrireIdentif=None, displayStatus='未配置运营商', showAble=True,
                   showInTable=True, userAble=False, slotIdentif='210_18.01')
        fields = dict(unconfiguredCarrierSkippedCount=1, unconfiguredCarrierSkippedCards=[row['cardNo']])
        self.receipt.update(**fields, unconfiguredCarrierSkippedRows=[dict(cardNo=row['cardNo'],
            backendId=row['id'], slot=row['slotIdentif'], opr=row['opr'], reason='carrier_unconfigured')])
        self.save('retention.json', self.receipt)
        self.manifest.update(**fields, retentionActionableCardCount=251)
        self.config.update(taskDUnconfiguredCarrierSkippedCount=1,
                           taskDUnconfiguredCarrierSkippedCards=[row['cardNo']], taskDRetentionActionableCardCount=251)
        path = Path(self.manifest['retentionNotificationPath'])
        notification = json.loads(path.read_text())
        notification['cardNos'].remove(row['cardNo'])
        notification.update(**fields, count=251, retentionReceiptSha256=AUDIT.sha256_file(Path(self.config['taskDRetentionReceipt'])))
        self.save(path.name, notification)
        self.save('taskDRetentionExpiryReceipt.json', dict(status='passed', businessReceiptPassed=True,
            transactionalVerificationPassed=True, independentReadbackAvailable=False,
            verificationMode='synchronous_transactional_business_receipt',
            batches=[dict(inputCount=251, matchedCardCount=251, notFound=[])]))
        result = self.audit()
        self.assertEqual(AUDIT.PASS, result.status, result.message)
        for target, key, value in ((self.manifest, 'unconfiguredCarrierSkippedCount', 0),
                                  (self.config, 'taskDUnconfiguredCarrierSkippedCards', [])):
            original = target[key]
            target[key] = value
            self.assertEqual(AUDIT.FAIL, self.audit().status)
            target[key] = original
        notification['cardNos'].append(row['cardNo'])
        notification['count'] = 252
        self.save(path.name, notification)
        self.assertEqual(AUDIT.FAIL, self.audit().status)

    def test_removed_transfer_rejects_stale_candidate_paths_and_false_counts(self):
        self.notification_only()
        self.manifest["chatrCandidatePath"] = "old.json"
        self.assertEqual(AUDIT.FAIL, self.audit().status)
        self.manifest.pop("chatrCandidatePath")
        self.config["taskDCarrierActualWrite"] = 1
        self.assertEqual(AUDIT.FAIL, self.audit().status)

    def test_removed_transfer_rejects_old_b14_notification_dependency(self):
        self.notification_only()
        path = Path(self.manifest["retentionNotificationPath"])
        notification = json.loads(path.read_text())
        notification["requiredCarrierTargets"] = {"chatr": "B14-C快下保留卡"}
        self.save(path.name, notification)
        self.assertEqual(AUDIT.FAIL, self.audit().status)

    def test_manifest_cannot_count_skipped_as_actionable_or_success(self):
        self.manifest["retentionActionableCardCount"] = 396
        self.assertEqual(AUDIT.FAIL, self.audit().status)

    def test_report_counts_must_match_independent_recalculation(self):
        self.config["taskDRetentionMatchedCardCount"] = 398
        self.assertEqual(AUDIT.FAIL, self.audit().status)

    def test_candidate_wrong_identity_or_opr_group_fails(self):
        path = Path(self.config["groups"][0]["candidate"])
        candidate = json.loads(path.read_text())
        candidate["rows"][0]["backendId"] = "invented"
        candidate["candidates"] = candidate["rows"]
        self.save(path.name, candidate)
        self.assertEqual(AUDIT.FAIL, self.audit().status)

    def test_skipped_card_cannot_enter_expiry_notification(self):
        path = Path(self.manifest["retentionNotificationPath"])
        notification = json.loads(path.read_text())
        notification["cardNos"][0] = self.receipt["filteredUnmatchedCards"][0]
        self.save(path.name, notification)
        self.assertEqual(AUDIT.FAIL, self.audit().status)


if __name__ == "__main__":
    unittest.main()
