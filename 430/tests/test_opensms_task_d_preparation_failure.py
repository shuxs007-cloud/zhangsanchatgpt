"""Task D preparation failures remain precise, local and bound to this run."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_opensms_acquire_reliability as fixtures

RUNNER = fixtures.RUNNER


class TaskDPreparationFailureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AcquisitionReliabilityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.failure = {
            'version': 1, 'runId': self.runner.run_id, 'task': 'D', 'stage': 'task_d_prepare',
            'code': 'OPENSMS_TASK_D_ROW_INVALID', 'category': 'carrier_unconfigured',
            'cardNo': '2000000001', 'backendId': 'record-1', 'slot': '210_18.01',
            'businessWrites': 0, 'notificationSubmitted': False,
        }

    def invoke(self, failure, label='task_d_prepare'):
        stderr = json.dumps({'taskDPreparationFailure': failure}) + '\noriginal stack'
        with patch.object(RUNNER.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['offline'], 1, '', stderr)):
            return self.runner.command(label, ['offline'])

    def test_data_failure_reaches_full_compact_and_md_without_masking_incomplete_run(self):
        with self.assertRaisesRegex(RUNNER.PatrolError, '后台明确未配置运营商.*不能自动跳过') as captured:
            self.invoke(self.failure)
        self.assertEqual(self.runner.state['taskDPreparationFailure']['category'], 'carrier_unconfigured')
        result = self.runner.write_failure_report(captured.exception)
        compact = RUNNER.compact_launcher_result(result)
        self.assertEqual(compact['taskDPreparationFailure'], result['taskDPreparationFailure'])
        self.assertIsNone(compact['finalWorkbook'])
        self.assertIn('未配置运营商', Path(result['report']).read_text())
        self.assertNotIn('2000000001', Path(result['report']).read_text())

    def test_foreign_run_malformed_and_submission_claims_do_not_mask_original_failure(self):
        for patch_fields in (
            {'runId': 'foreign'}, {'task': 'E'}, {'stage': 'other'}, {'category': []},
            {'code': 'other'}, {'category': 'unknown'}, {'notificationSubmitted': True},
            {'businessWrites': False}, {'businessWrites': 1}, {'slot': {}},
        ):
            with self.subTest(patch_fields), self.assertRaisesRegex(RUNNER.PatrolError, 'task_d_prepare失败，退出码1'):
                self.invoke({**self.failure, **patch_fields})
            self.assertNotIn('taskDPreparationFailure', self.runner.state)
        for malformed in (None, [], 'bad'):
            with self.assertRaisesRegex(RUNNER.PatrolError, 'task_d_prepare失败，退出码1'):
                self.invoke(malformed)

    def test_other_stage_cannot_reuse_d_preparation_error(self):
        with self.assertRaisesRegex(RUNNER.PatrolError, 'task_d_expiry_retention失败，退出码1'):
            self.invoke(self.failure, 'task_d_expiry_retention')
        self.assertNotIn('taskDPreparationFailure', self.runner.state)


if __name__ == '__main__':
    unittest.main()
