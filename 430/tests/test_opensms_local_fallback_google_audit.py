"""Offline regressions for the dispatcher's forced-local receipt contract."""
from copy import deepcopy
import unittest

import test_opensms_no_google_audit_report as fixtures


AUDIT = fixtures.AUDIT


class LocalFallbackGoogleAuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NoGoogleAuditReportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.dispatch.update({
            "processing_mode": "local_fallback",
            "fallback_reason": "已指定强制本地处理",
            "api_attempts": [],
            "local_builder": "/offline/pipeline/build_card_slot_report.sh",
            "output_path": str(self.fixture.result),
            "receipt_path": str(self.fixture.card_path),
            "local_result": {
                "result": "成功",
                "checks": {"Google已禁用且专属内容不存在": True, "系统机房及开卡日期": True},
                "failed_checks": [],
                "output_path": str(self.fixture.result),
                "receipt_path": str(self.fixture.card_path),
            },
        })

    def test_forced_local_dispatch_is_verified_skipped(self):
        result = self.fixture.review()
        self.assertEqual(AUDIT.SKIP, result.status, result.message)
        self.assertTrue(result.details["skip_contract_verified"])
        self.assertFalse(result.details["google_artifacts_read"])
        self.assertEqual("local_fallback", result.details["processing_mode"])

    def test_remote_failed_and_missing_processing_modes_are_rejected(self):
        for mode in ("remote_api", "remote_api_failed_no_local_fallback", "local_failed", "", None):
            with self.subTest(mode=mode):
                self.fixture.dispatch["processing_mode"] = mode
                self.assertEqual(AUDIT.FAIL, self.fixture.review().status)

    def test_remote_execution_is_rejected_even_with_local_success_metadata(self):
        original = deepcopy(self.fixture.dispatch)
        for mode in ("local", "local_fallback"):
            for evidence in ({"api_attempts": [{"available": True}]},
                             {"job": {"status": "succeeded"}},
                             {"api_base_url": "https://offline.invalid"}):
                with self.subTest(mode=mode, evidence=evidence):
                    self.fixture.dispatch = {**deepcopy(original), "processing_mode": mode, **evidence}
                    self.assertEqual(AUDIT.FAIL, self.fixture.review().status)

    def test_missing_dispatch_execution_metadata_is_rejected(self):
        original = deepcopy(self.fixture.dispatch)
        for key in ("result", "api_attempts", "local_builder", "local_result", "output_path", "receipt_path"):
            with self.subTest(key=key):
                self.fixture.dispatch = deepcopy(original)
                self.fixture.dispatch.pop(key)
                self.assertEqual(AUDIT.FAIL, self.fixture.review().status)

    def test_missing_or_failed_local_result_is_rejected(self):
        original = deepcopy(self.fixture.dispatch["local_result"])
        for mutation in ({"result": "失败"}, {"checks": {}}, {"checks": {"系统机房及开卡日期": False}},
                         {"checks": {"系统机房及开卡日期": 1}}, {"failed_checks": ["系统机房及开卡日期"]},
                         {"output_path": "/offline/other.xlsx"}, {"receipt_path": "/offline/other.json"}):
            with self.subTest(mutation=mutation):
                self.fixture.dispatch["local_result"] = {**deepcopy(original), **mutation}
                self.assertEqual(AUDIT.FAIL, self.fixture.review().status)
        for key in original:
            with self.subTest(missing=key):
                self.fixture.dispatch["local_result"] = deepcopy(original)
                self.fixture.dispatch["local_result"].pop(key)
                self.assertEqual(AUDIT.FAIL, self.fixture.review().status)

    def test_malformed_execution_metadata_fails_closed(self):
        original = deepcopy(self.fixture.dispatch)
        for mutation in ({"local_result": "success"}, {"local_builder": None},
                         {"local_builder": "https://offline.invalid/builder"},
                         {"api_attempts": None}):
            with self.subTest(mutation=mutation):
                self.fixture.dispatch = {**deepcopy(original), **mutation}
                self.assertEqual(AUDIT.FAIL, self.fixture.review().status)

    def test_local_fallback_still_requires_google_skip_and_current_run_proof(self):
        for location, key, value in (
            (self.fixture.dispatch["inputs"], "google_dependency", {}),
            (self.fixture.dispatch["inputs"], "association", {"path": "/forbidden/old.xlsx"}),
            (self.fixture.card["process"], "google_dependency", {}),
            (self.fixture.card["verification"], "google_dependency_review", {}),
            (self.fixture.card["verification"]["system_kooda_source_review"], "run_id", "prior-run"),
        ):
            with self.subTest(key=key, value=value):
                original = location[key]
                try:
                    location[key] = value
                    self.assertEqual(AUDIT.FAIL, self.fixture.review().status)
                finally:
                    location[key] = original


if __name__ == "__main__":
    unittest.main()
