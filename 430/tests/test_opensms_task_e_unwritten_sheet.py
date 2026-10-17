import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import opensms_task_e_unwritten_sheet as module


class TaskEUnwrittenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.xlsx'
        self.source.write_bytes(b'offline source hash fixture')
        self.run_id = 'offline-run'
        self.manifest_path = self.root / 'opensms_task_e_offline-run_manifest.json'
        self.receipt_path = self.root / 'opensms_task_e_offline-run_receipt.json'
        self.phones = ['2369785939', '4377840953']
        common = dict(task='E', sourcePath=str(self.source), sourceSha256=module.sha256_file(self.source))
        self.manifest = dict(common, forceGate=True, automationGate=True, candidateCount=2,
                             groups=[dict(expireDate='2026-10-01', cardNos=self.phones)])
        self.row = dict(phone=self.phones[0], expireDate='2026-10-01', reason='backend_not_found')
        raw = dict(inputCount=2, matchedCardCount=1, notFound=[self.phones[0]], updatedChatCount=0)
        self.receipt = dict(common, manifestPath=str(self.manifest_path),
                            status='passed_with_not_found', businessReceiptPassed=True,
                            transactionalVerificationPassed=True, ledgerVerificationPassed=True,
                            inputCount=2, submittedCount=1, notWrittenCount=1, notWrittenCards=[self.row],
                            batches=[dict(raw, cardNos=self.phones, expireDate='2026-10-01',
                                          serverResponse=dict(code=200, data=raw))])

    def read_rows(self):
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.receipt_path.write_text(json.dumps(self.receipt))
        return module.confirmed_rows(self.source, self.manifest_path, self.receipt_path, self.run_id)

    def test_confirmed_not_found_only_and_no_change_is_not_failure(self):
        self.assertEqual(self.read_rows(), [self.row])

    def test_reject_stale_run(self):
        self.run_id = 'another-run'
        with self.assertRaisesRegex(ValueError, '不是本轮'):
            self.read_rows()

    def test_reject_source_changed(self):
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.read_rows()

    def test_reject_wrong_manifest(self):
        self.receipt['manifestPath'] = str(self.root / 'old.json')
        with self.assertRaisesRegex(ValueError, '清单绑定'):
            self.read_rows()

    def test_reject_uncertain_or_failed(self):
        for status in ['failed', 'in_progress', 'submission_outcome_uncertain']:
            self.receipt['status'] = status
            with self.assertRaisesRegex(ValueError, '不确定'):
                self.read_rows()

    def test_reject_unauthorized(self):
        self.manifest['forceGate'] = False
        with self.assertRaisesRegex(ValueError, '授权'):
            self.read_rows()

    def test_reject_unverified(self):
        self.receipt['ledgerVerificationPassed'] = False
        with self.assertRaises(ValueError):
            self.read_rows()

    def test_reject_changed_top_summary(self):
        self.receipt['notWrittenCards'] = [dict(self.row, phone=self.phones[1])]
        with self.assertRaisesRegex(ValueError, '汇总未写入'):
            self.read_rows()

    def test_reject_changed_raw_response(self):
        self.receipt = copy.deepcopy(self.receipt)
        self.receipt['batches'][0]['serverResponse'] = dict(code=200, data={
            'inputCount': 2, 'matchedCardCount': 2, 'notFound': [], 'updatedChatCount': 1})
        with self.assertRaises(ValueError):
            self.read_rows()

    def test_reject_duplicate_batch(self):
        self.receipt['batches'] *= 2
        with self.assertRaisesRegex(ValueError, '重复'):
            self.read_rows()

    def test_reject_mismatched_date(self):
        self.receipt['batches'][0]['expireDate'] = '2026-10-02'
        with self.assertRaisesRegex(ValueError, '截止日'):
            self.read_rows()

    def test_zero_missing_skips_authoring(self):
        self.read_rows()
        args = argparse.Namespace(input=self.source, manifest=self.manifest_path,
                                  receipt=self.receipt_path, run_id=self.run_id)
        with mock.patch.object(module, 'confirmed_rows', return_value=[]), mock.patch.object(module.subprocess, 'run') as author:
            result = module.run(args)
        author.assert_not_called()
        self.assertEqual(result['output'], str(self.source))
        self.assertIsNone(result['sheet'])

    def test_source_overwrite_rejected(self):
        with self.assertRaisesRegex(ValueError, '不得覆盖'):
            module.graft_sheet(self.source, self.root / 'unused.xlsx', self.source, [])

    def test_runner_orders_sheet_before_final_refresh(self):
        text = (ROOT / 'scripts/opensms_patrol_runner.py').read_text()
        start = text.index('                self.run_task_e(document, acquire)')
        code = text[start:]
        self.assertLess(code.index('document = self.add_task_e_unwritten_sheet(document)'),
                        code.index('refresh = self.refresh_workbook(document)'))
        self.assertIn('"taskENotWrittenSheetRequired": task_e.get("executed") is True', text)

    def test_report_has_one_phone_per_line_copy_block_and_alert(self):
        text = (ROOT / 'scripts/opensms_finalize_patrol_report.py').read_text()
        self.assertIn('"```text"', text)
        self.assertIn("*[str(row.get('phone') or '') for row in task_e_not_written]", text)
        self.assertIn('请人工排查以下后台未命中号码', text)
        summary = (ROOT / 'scripts/opensms_chat_summary.py').read_text()
        self.assertIn('完整报告提供一行一个号码的复制区', summary)

    def test_a_only_guard_does_not_read_receipts(self):
        spec = importlib.util.spec_from_file_location('runner_unwritten_test', ROOT / 'scripts/opensms_patrol_runner.py')
        runner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = runner
        spec.loader.exec_module(runner)
        cls = next(value for value in vars(runner).values() if isinstance(value, type) and hasattr(value, 'add_task_e_unwritten_sheet'))
        patrol = object.__new__(cls)
        patrol.authorization = {'taskE': False}
        patrol.state = {'stages': {'taskE': {'executed': False}}}
        patrol.command = mock.Mock(side_effect=AssertionError('no process in A-only run'))
        document = {'cardResult': 'current-A.xlsx'}
        self.assertIs(patrol.add_task_e_unwritten_sheet(document), document)


if __name__ == '__main__':
    unittest.main()
