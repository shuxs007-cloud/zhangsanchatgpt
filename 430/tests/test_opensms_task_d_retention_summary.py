from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("retention_summary_test", SCRIPTS / "opensms_task_d_retention_summary.py")
SUMMARY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUMMARY)
AUDIT_SPEC = importlib.util.spec_from_file_location("retention_audit_test", SCRIPTS / "opensms_parallel_audit.py")
AUDIT = importlib.util.module_from_spec(AUDIT_SPEC)
sys.modules[AUDIT_SPEC.name] = AUDIT
AUDIT_SPEC.loader.exec_module(AUDIT)

RUN_ID = "20260907T010000+0800"
C, T = "B14-C快下保留卡", "B14-T快下保留卡"
ROUTES = list(SUMMARY.ROUTES)
TIME = lambda sec: f"2026-09-07T01:00:{sec:02d}+08:00"


class RetentionSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / RUN_ID
        self.root.mkdir()
        self.bundle_path = self.root / "groups.json"
        self.population_path = self.root / "population.json"
        self.output = self.root / "summary.json"

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def group(self, route_index=0, writes=None, already=None):
        writes, already = writes or [], already or []
        route, name = ROUTES[route_index], (C, T)[route_index]
        tid = ("c-id", "t-id")[route_index]
        candidates = [{"电话": phone} for phone, *_ in writes + already]
        original = {"createdAt": TIME(1), "task": "D", "forceGate": True, "automationGate": True, "phase": "retention_carrier", "route": route, "targetCarrierName": name, "sourceProvenance": "phone_card_retain_all_pages", "count": len(candidates), "rows": candidates}
        cp = self.save(f"{route}_candidate.json", original)
        result = {"route": route, "target": name, "candidate": cp, "candidateCount": len(candidates), "backendValid": len(writes), "actualWrite": len(writes), "result": "passed" if candidates else "no_candidate"}
        if not candidates:
            return result
        valid = [{"电话": phone, "后台ID": bid, "精确原运营商": old_name, "原运营商ID": old_id, "目标运营商": name, "目标运营商ID": tid} for phone, bid, old_name, old_id in writes]
        vp = self.save(f"{route}_validated.json", {**original, "sourceCandidate": cp, "validatedAt": TIME(2), "originalCount": len(candidates), "count": len(writes), "rows": valid})
        before = {"candidateCount": len(candidates), "eligibleCount": len(writes), "alreadyTargetCount": len(already), "nonSourceCount": 0, "missingCount": 0, "duplicateBackendCount": 0, "eligibleRows": [{"cardNo": phone, "id": bid, "carrierName": old_name, "carrireIdentif": old_id} for phone, bid, old_name, old_id in writes], "alreadyTarget": [{"phone": phone, "row": {"cardNo": phone, "id": bid, "carrierName": name, "carrireIdentif": tid}} for phone, bid, *_ in already], "nonSource": []}
        source_map = {old_id: old_name for _, _, old_name, old_id in writes}
        write = {"ts": TIME(3), "candidatePath": cp, "validatedCandidatePath": vp, "target": {"name": name, "identif": tid}, "sourceCarriers": [{"name": v, "identif": k} for k, v in source_map.items()], "sourceCarrierNames": list(source_map.values()), "before": before, "actualWriteCount": len(writes), "status": "written" if writes else "no_eligible_records", "noWrite": not writes, "updateResponses": [{"code": 200}] if writes else [], "after": {} if writes else None}
        wp = self.save(f"{route}_write.json", write)
        verify = {"checkedAt": TIME(4), "source": vp, "target": {"name": name, "identif": tid if writes else None}, "total": len(writes), "targetCount": len(writes), "missingCount": 0, "duplicateBackendCount": 0, "stillNotTargetCount": 0, "passed": True if writes else None, "status": "checked" if writes else "no_candidate", "verificationStatus": "performed" if writes else "not_required", "independentReadbackPerformed": bool(writes), "mode": "protocol-api-readonly-verify"}
        result.update(validated=vp, write=wp, verify=self.save(f"{route}_verify.json", verify))
        return result

    def fixture(self, c_writes=None, t_writes=None, c_already=None, population=None):
        groups = [self.group(0, c_writes, c_already), self.group(1, t_writes)]
        bundle = {"runId": RUN_ID, "businessCompletedAt": TIME(5), "groups": groups}
        self.save(self.bundle_path.name, bundle)
        pop = population if population is not None else [[(phone, bid) for phone, bid, *_ in (c_writes or []) + (c_already or [])], [(phone, bid) for phone, bid, *_ in t_writes or []]]
        all_groups = []
        for index, items in enumerate(pop):
            rows = [{"phone10": SUMMARY.phone10(phone), "backendId": bid, "carrierIdentif": ("c-id", "t-id")[index]} for phone, bid in items]
            unique = len({row["phone10"] for row in rows})
            page_count = max(1, (len(rows) + 499) // 500)
            all_groups.append({"carrierName": (C, T)[index], "carrierIdentif": ("c-id", "t-id")[index], "rawRowCount": len(rows), "uniquePhoneCount": unique, "duplicatePhoneRowCount": len(rows) - unique, "firstPageTotal": len(rows), "pageCount": page_count, "pageSize": 500, "paginationTotalStable": True, "backendIdsUnique": True, "exactCarrierValidated": True, "rows": rows, "pageProofs": [{"pageNum": num, "pageSize": 500, "reportedTotal": len(rows), "rowCount": min(500, max(0, len(rows) - (num-1)*500)), "expectedRowCount": min(500, max(0, len(rows) - (num-1)*500)), "verifiedAt": TIME(7)} for num in range(1, page_count+1)]})
        total_raw = sum(g["rawRowCount"] for g in all_groups)
        total = sum(g["uniquePhoneCount"] for g in all_groups)
        self.save(self.population_path.name, {"schemaVersion": 1, "task": "D", "status": "passed", "phase": "post_task_d_retention_population", "runId": RUN_ID, "createdAt": TIME(6), "completedAt": TIME(8), "scope": "exact_b14_c_t_all_pages", "countingUnit": "unique_phone10", "sourceProvenance": "phone_config_exact_carriers_all_pages", "endpoint": "/phone/config/list", "pageSize": 500, "readOnly": True, "silentBrowserMode": True, "browserForegroundActivationAttempted": False, "authorization": {key: True for key in ("forceGate", "enableGate", "executeGate", "runIdMatched")}, "browserSession": {"profile": "430", "dedicatedTargetId": "test-target"}, "readOnlyConcurrency": {"submitted": 3, "completed": 3, "failed": 0, "cancelled": 0}, "totalUniquePhoneCount": total, "totalRawRowCount": total_raw, "totalDuplicatePhoneRowCount": total_raw-total, "crossGroupPhoneOverlapCount": 0, "backendIdsUnique": True, "groups": all_groups})
        return bundle

    def run_summary(self, save=False):
        return SUMMARY.build_summary(self.population_path, self.bundle_path, RUN_ID, self.output if save else None)

    def mutate(self, path, fn):
        value = SUMMARY.load(path)
        fn(value)
        Path(path).write_text(json.dumps(value), encoding="utf-8")

    def test_new_transfer_is_exact_write_from_outside_and_verified(self):
        self.fixture(c_writes=[("12345678901", "b1", "A1", "a1")], population=[[("2345678901", "b1"), ("2345678902", "b2")], []])
        result = self.run_summary()
        self.assertEqual((1, 2), (result["newTransferredCount"], result["totalInRetentionCount"]))
        self.assertEqual("2345678901", result["newTransfers"][0]["phone10"])

    def test_already_target_no_write_not_new(self):
        self.fixture(c_already=[("2345678901", "b1", C, "c-id")])
        result = self.run_summary()
        self.assertEqual((0, 1), (result["newTransferredCount"], result["totalInRetentionCount"]))

    def test_inter_retention_transfer_is_not_new(self):
        self.fixture(c_writes=[("2345678901", "b1", T, "t-id")])
        result = self.run_summary()
        self.assertEqual(0, result["newTransferredCount"])
        self.assertEqual(1, len(result["withinRetentionTransfers"]))

    def test_no_candidates_can_have_nonzero_population(self):
        self.fixture(population=[[("2345678901", "b1")], [("2345678902", "b2")]])
        result = self.run_summary()
        self.assertEqual((0, 2), (result["newTransferredCount"], result["totalInRetentionCount"]))

    def test_removed_transfer_has_zero_write_groups_but_independent_population(self):
        self.fixture(population=[[("2345678901", "b1")], [("2345678902", "b2")]])
        self.mutate(self.bundle_path, lambda bundle: bundle.update(groups=[],
                    retentionCarrierTransferEnabled=False, retentionCarrierTransferStatus="removed_by_user"))
        result = self.run_summary()
        self.assertEqual((0, 2), (result["newTransferredCount"], result["totalInRetentionCount"]))
        self.assertEqual(result["proofGroups"], [])
        self.assertIs(result["retentionCarrierTransferEnabled"], False)

    def test_removed_transfer_cannot_include_legacy_write_groups(self):
        self.fixture()
        self.mutate(self.bundle_path, lambda bundle: bundle.update(
                    retentionCarrierTransferEnabled=False, retentionCarrierTransferStatus="removed_by_user"))
        with self.assertRaisesRegex(ValueError, "工作已删除"):
            self.run_summary()

    def test_audit_accepts_removed_transfer_and_rejects_undeclared_legacy_groups(self):
        self.fixture(population=[[("2345678901", "b1")], []])
        self.mutate(self.bundle_path, lambda bundle: bundle.update(groups=[],
                    retentionCarrierTransferEnabled=False, retentionCarrierTransferStatus="removed_by_user"))
        config = self.audit_config()
        config["taskDRetentionCarrierTransferEnabled"] = False
        resolver = AUDIT.PathResolver(self.root / "config.json", None)
        result = AUDIT.check_task_d_retention_counts(config, resolver)
        self.assertEqual(AUDIT.PASS, result.status, result.message)
        config["groups"] = [{"title": "任务D 旧转入"}]
        self.assertEqual(AUDIT.FAIL, AUDIT.check_task_d_retention_counts(config, resolver).status)

    def test_duplicate_same_carrier_phone_counts_once(self):
        self.fixture(population=[[("2345678901", "b1"), ("2345678901", "b2")], []])
        self.assertEqual(1, self.run_summary()["totalInRetentionCount"])

    def test_cross_carrier_overlap_fails(self):
        self.fixture(population=[[("2345678901", "b1")], [("2345678901", "b2")]])
        with self.assertRaises(SUMMARY.RetentionProofError): self.run_summary()

    def test_duplicate_backend_id_fails(self):
        self.fixture(population=[[("2345678901", "b1"), ("2345678902", "b1")], []])
        with self.assertRaises(SUMMARY.RetentionProofError): self.run_summary()

    def test_source_log_count_cannot_be_population(self):
        self.fixture(population=[[("2345678901", "b1")], []])
        self.mutate(self.population_path, lambda d: d.update(totalUniquePhoneCount=900))
        with self.assertRaises(SUMMARY.RetentionProofError): self.run_summary()

    def test_population_before_business_completion_fails(self):
        self.fixture()
        self.mutate(self.population_path, lambda d: d.update(createdAt=TIME(4)))
        with self.assertRaises(SUMMARY.RetentionProofError): self.run_summary()

    def test_incomplete_pagination_fails(self):
        self.fixture()
        self.mutate(self.population_path, lambda d: d["groups"][0].update(pageProofs=[]))
        with self.assertRaises(SUMMARY.RetentionProofError): self.run_summary()

    def test_missing_verify_fails(self):
        bundle = self.fixture(c_writes=[("2345678901", "b1", "A1", "a1")])
        Path(bundle["groups"][0]["verify"]).unlink()
        with self.assertRaises(FileNotFoundError): self.run_summary()

    def test_failed_verify_fails(self):
        bundle = self.fixture(c_writes=[("2345678901", "b1", "A1", "a1")])
        self.mutate(bundle["groups"][0]["verify"], lambda d: d.update(passed=False))
        with self.assertRaises(SUMMARY.RetentionProofError): self.run_summary()

    def test_live_after_target_drift_fails(self):
        self.fixture(c_writes=[("2345678901", "b1", "A1", "a1")], population=[[], [("2345678901", "b1")]])
        with self.assertRaises(SUMMARY.RetentionProofError): self.run_summary()

    def test_exact_source_proof_missing_fails(self):
        bundle = self.fixture(c_writes=[("2345678901", "b1", "A1", "a1")])
        self.mutate(bundle["groups"][0]["validated"], lambda d: d["rows"][0].pop("精确原运营商"))
        with self.assertRaises(SUMMARY.RetentionProofError): self.run_summary()

    def test_wrong_run_and_wrong_route_fail(self):
        self.fixture()
        self.mutate(self.bundle_path, lambda d: d.update(runId="old-run"))
        with self.assertRaises(SUMMARY.RetentionProofError): self.run_summary()

    def test_exclusive_output_is_never_overwritten(self):
        self.fixture()
        self.run_summary(save=True)
        with self.assertRaises(FileExistsError): self.run_summary(save=True)

    def audit_config(self):
        stored = self.run_summary(save=True)
        groups = [{**group, "title": "任务D" + str(index)} for index, group in enumerate(SUMMARY.load(self.bundle_path)["groups"])]
        return {"runId": RUN_ID, "taskDRetentionSummaryContractVersion": 1, "taskDExecuted": True, "taskDCurrentUserAuthorized": True, "taskDRetentionSummary": stored, "taskDRetentionPopulationReceipt": str(self.population_path), "taskDRetentionSummaryReceipt": str(self.output), "taskDRetentionGroupsReceipt": str(self.bundle_path), "groups": groups, "taskDSwapExpiryReceipt": self.save("swap_expiry.json", {"task": "D", "phase": "swap_zero", "status": "passed", "businessReceiptPassed": True, "createdAt": TIME(1)}), "taskDRetentionExpiryReceipt": self.save("retention_expiry.json", {"task": "D", "phase": "retention_verified", "status": "passed", "businessReceiptPassed": True, "createdAt": TIME(4)})}

    def test_audit_recomputes_new_and_total(self):
        self.fixture(c_writes=[("2345678901", "b1", "A1", "a1")])
        config = self.audit_config()
        result = AUDIT.check_task_d_retention_counts(config, AUDIT.PathResolver(self.root / "config.json", None))
        self.assertEqual("passed", result.status, result)

    def test_audit_rejects_changed_saved_count(self):
        self.fixture()
        config = self.audit_config()
        self.mutate(self.output, lambda d: d.update(newTransferredCount=50))
        result = AUDIT.check_task_d_retention_counts(config, AUDIT.PathResolver(self.root / "config.json", None))
        self.assertEqual("failed", result.status)

    def test_audit_rejects_mutated_source_sha_even_same_count(self):
        self.fixture()
        config = self.audit_config()
        cp = SUMMARY.load(self.bundle_path)["groups"][0]["candidate"]
        self.mutate(cp, lambda d: d.update(untrusted_extra=True))
        result = AUDIT.check_task_d_retention_counts(config, AUDIT.PathResolver(self.root / "config.json", None))
        self.assertEqual("failed", result.status)

    def test_audit_unauthorized_must_not_read_new_proofs(self):
        config = {"taskDRetentionSummaryContractVersion": 1, "taskDExecuted": False, "taskDRetentionSummaryReceipt": "/not/a/real/file"}
        result = AUDIT.check_task_d_retention_counts(config, AUDIT.PathResolver(self.root / "config.json", None))
        self.assertEqual("failed", result.status)

    def test_audit_rejects_swapped_actual_task_d_groups(self):
        self.fixture()
        config = self.audit_config()
        config["groups"][0]["candidate"] = self.save("other_candidate.json", {"count": 1, "route": ROUTES[0], "targetCarrierName": C})
        result = AUDIT.check_task_d_retention_counts(config, AUDIT.PathResolver(self.root / "config.json", None))
        self.assertEqual("failed", result.status)

    def test_audit_rejects_expiry_after_reported_completion(self):
        self.fixture()
        config = self.audit_config()
        self.mutate(config["taskDRetentionExpiryReceipt"], lambda data: data.update(createdAt=TIME(9)))
        result = AUDIT.check_task_d_retention_counts(config, AUDIT.PathResolver(self.root / "config.json", None))
        self.assertEqual("failed", result.status)

    def test_rejects_non_phone_characters(self):
        with self.assertRaises(SUMMARY.RetentionProofError): SUMMARY.phone10("a2345678901")

    def test_old_report_remains_readable(self):
        result = AUDIT.check_task_d_retention_counts({}, AUDIT.PathResolver(self.root / "config.json", None))
        self.assertEqual("passed", result.status)


if __name__ == "__main__":
    unittest.main()
