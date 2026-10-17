import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import opensms_carrier_reply_comparison as module
from opensms_chat_summary import render_summary


def balance(day='2026-09-26', observed='2026-09-26T01:30:00+08:00', value=5.5, identif='carrier-1', name='B1-fixture'):
    return {'serverDate': day, 'carrierReplyRates': [{
        'carrierIdentif': identif, 'carrierName': name, 'metric': 'firstReplyRate',
        'status': 'ok', 'statDate': day, 'replyRatePct': value,
        'observedAt': observed, 'dailyRow': {'firstReplyRate': f'{value}%'},
    }]}


def current(**kwargs):
    data = balance(**kwargs)
    return module.snapshot(data, data['serverDate'].replace('-', '') + 'T013000+0800', data['serverDate'])


def previous(**kwargs):
    return current(day='2026-09-25', observed=kwargs.pop('observed', '2026-09-25T01:30:00+08:00'), **kwargs)


class ComparisonTests(unittest.TestCase):
    def test_exact_same_id_and_metric_with_percentage_points(self):
        result = module.compare(current(), [previous(value=4)])
        row = result['rows'][0]
        self.assertEqual(row['comparisonStatus'], 'exact')
        self.assertEqual(row['deltaPercentagePoints'], 1.5)
        self.assertEqual(result['comparableCount'], 1)

    def test_matching_window_boundary_nearest_and_no_full_day_fallback(self):
        for offset, yes in [('01:00:00', True), ('02:00:00', True), ('00:59:59', False),
                            ('02:00:01', False), ('01:20:00', True), ('23:59:59', False)]:
            with self.subTest(offset=offset):
                result = module.compare(current(), [previous(observed=f'2026-09-25T{offset}+08:00')])
                self.assertEqual(result['comparableCount'], int(yes))
        result = module.compare(current(), [previous(observed='2026-09-25T01:25:00+08:00', value=4),
                                            previous(observed='2026-09-25T01:31:00+08:00', value=3)])
        self.assertEqual(result['rows'][0]['previousReplyRatePct'], 3)
        self.assertEqual(result['rows'][0]['timeDifferenceSeconds'], 60)

    def test_legacy_window_and_report_label(self):
        old = previous(observed='2026-09-25T01:20:00+08:00')
        self.assertEqual(module.compare(current(), [old], 300)['comparableCount'], 0)
        result = module.compare(current(), [old])
        self.assertEqual(result['matchingWindowSeconds'], 1800)
        self.assertEqual(result['comparableCount'], 1)
        result['independentReadbackPassed'] = True
        self.assertIn('±30分钟', '\n'.join(module.report_lines(result)))
        for value in (True, 1801, 1800.0):
            with self.assertRaises(ValueError):
                module.compare(current(), [old], value)

    def test_id_not_name_and_renamed_id_is_disclosed(self):
        self.assertEqual(module.compare(current(), [previous(identif='new-id')])['comparableCount'], 0)
        result = module.compare(current(), [previous(name='旧名称')])
        result['independentReadbackPassed'] = True
        self.assertIn('昨日名：旧名称', '\n'.join(module.report_lines(result)))

    def test_zero_is_real_not_missing(self):
        row = module.compare(current(value=0), [previous(value=0)])['rows'][0]
        self.assertEqual(row['deltaPercentagePoints'], 0)
        self.assertEqual(row['previousReplyRatePct'], 0)

    def test_missing_history_remains_unknown(self):
        row = module.compare(current(), [])['rows'][0]
        self.assertIsNone(row['previousReplyRatePct'])
        self.assertIsNone(row['deltaPercentagePoints'])
        self.assertEqual(row['comparisonReason'], '无昨日同期快照')

    def test_source_validation_fails_closed_per_row(self):
        for changes in [dict(metric='replyRate'), dict(status='error'), dict(statDate='2026-09-25'),
                        dict(observedAt=None), dict(observedAt='2026-09-26T01:30:00'),
                        dict(replyRatePct=True), dict(replyRatePct=float('nan')), dict(replyRatePct=101),
                        dict(replyRatePct=-1), dict(dailyRow={'firstReplyRate': '5.5garbage'}),
                        dict(dailyRow={'firstReplyRate': '4%'})]:
            with self.subTest(changes=changes):
                data = balance()
                data['carrierReplyRates'][0].update(changes)
                result = module.snapshot(data, '20260926T013000+0800', data['serverDate'])
                self.assertIsNone(result['rows'][0]['replyRatePct'])

    def test_duplicate_ids_and_conflicting_same_instant_are_not_selected(self):
        data = balance()
        data['carrierReplyRates'] *= 2
        result = module.snapshot(data, '20260926T013000+0800', data['serverDate'])
        self.assertTrue(all(row['status'] == 'unknown' for row in result['rows']))
        result = module.compare(current(), [previous(value=4), previous(value=3)])
        self.assertEqual(result['rows'][0]['comparisonReason'], '同刻快照冲突')

    def test_wrong_business_day_metric_timezone_and_duplicate_history_are_excluded(self):
        for mutation in [('serverDate', '2026-09-24'), ('metric', 'replyRate'), ('timeZone', 'UTC')]:
            old = previous()
            old[mutation[0]] = mutation[1]
            self.assertEqual(module.compare(current(), [old])['comparableCount'], 0)
        old = previous()
        old['rows'] *= 2
        self.assertEqual(module.compare(current(), [old])['comparableCount'], 0)

    def test_month_boundary_uses_calendar_day_and_timezone(self):
        now = current(day='2026-10-01', observed='2026-09-30T16:01:00Z')
        old = current(day='2026-09-30', observed='2026-09-29T16:01:00Z')
        self.assertEqual(module.compare(now, [old])['rows'][0]['comparisonStatus'], 'exact')

    def test_snapshot_independent_readback_idempotence_and_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            module.persist_and_compare(balance(day='2026-09-25', observed='2026-09-25T01:30:00+08:00', value=4),
                                       '20260925T013000+0800', '2026-09-25', directory)
            result = module.persist_and_compare(balance(), '20260926T013000+0800', '2026-09-26', directory)
            self.assertTrue(result['independentReadbackPassed'])
            self.assertEqual(result['comparableCount'], 1)
            self.assertEqual(result, module.persist_and_compare(balance(), '20260926T013000+0800', '2026-09-26', directory))
            with self.assertRaisesRegex(ValueError, '禁止覆盖'):
                module.persist_and_compare(balance(value=6), '20260926T013000+0800', '2026-09-26', directory)
            old = Path(directory) / '2026-09-25' / '20260925T013000+0800.json'
            changed = json.loads(old.read_text())
            changed['payload']['rows'][0]['replyRatePct'] = 99
            old.write_text(json.dumps(changed))
            result = module.persist_and_compare(balance(), '20260926T013000+0800', '2026-09-26', directory)
            self.assertEqual(result['comparableCount'], 0)
            self.assertEqual(len(result['historyWarnings']), 1)

    def test_comparison_stays_in_full_report_and_json_but_not_chat(self):
        import opensms_patrol_runner as runner
        result = module.compare(current(name='Carrier|A\nB'), [previous()])
        result['independentReadbackPassed'] = True
        compact = runner.compact_launcher_result({'status': 'success', 'carrierReplyComparison': result})
        self.assertNotIn('rows', compact['carrierReplyComparison'])
        self.assertEqual(compact['carrierReplyComparison']['rowsDetailCount'], len(result['rows']))
        self.assertTrue(compact['carrierReplyComparison']['independentReadbackPassed'])
        self.assertTrue(compact['carrierReplyComparison']['detailsInCompleteResult'])
        self.assertIn('rows', result)  # Full JSON/MD still retains every carrier.
        full_report = '\n'.join(module.report_lines(result))
        self.assertIn('Carrier\\|A B', full_report)
        self.assertIn('5.500%', full_report)
        self.assertIn('昨日同期', full_report)
        rendered = render_summary({'status': 'success', 'carrierReplyComparison': result})
        self.assertNotIn('Carrier\\|A B', rendered)
        self.assertNotIn('5.500%', rendered)
        self.assertNotIn('昨日同期', rendered)
        self.assertEqual(sum(line.startswith('## ') for line in rendered.splitlines()), 7)
        script_root = Path(__file__).resolve().parents[1] / 'scripts'
        self.assertIn('carrier_reply_lines(balance.get("carrierReplyComparison"))',
                      (script_root / 'opensms_finalize_patrol_report.py').read_text())
        self.assertIn('balance.get("carrierReplyComparison")', (script_root / 'opensms_patrol_runner.py').read_text())

    def test_chat_comparison_omission_preserves_other_summary_content(self):
        comparison = module.compare(current(), [previous(value=4)])
        comparison['independentReadbackPassed'] = True
        for status, a_only in [('success', False), ('success', True), ('failed', False)]:
            with self.subTest(status=status, a_only=a_only):
                payload = {'status': status, 'currentZero': 3, 'overallReplyRatePct': 8.25,
                           'taskAOnly': a_only, 'taskSelection': 'A' if a_only else 'default',
                           'balanceGroups': [{'group': 'A1', 'percentRemaining': 71.5}],
                           'report': '/fixture/full-report.md'}
                baseline = render_summary(payload)
                rendered = render_summary({**payload, 'carrierReplyComparison': comparison})
                self.assertEqual(rendered, baseline)
                self.assertIn('71.50%', rendered)
                self.assertIn('/fixture/full-report.md', rendered)
                self.assertEqual(sum(line.startswith('## ') for line in rendered.splitlines()), 7)
                if status == 'success':
                    self.assertIn('0回复 3 个；整体回复率 8.25%', rendered)

    def test_final_audit_reopens_inputs_and_rejects_changed_calculated_results(self):
        with tempfile.TemporaryDirectory() as directory:
            data = balance()
            comparison = module.persist_and_compare(data, '20260926T013000+0800', data['serverDate'], directory)
            data['carrierReplyComparison'] = comparison
            self.assertEqual(module.verify_comparison(data)['carrierCount'], 1)
            comparison['rows'][0]['currentReplyRatePct'] = 99
            with self.assertRaisesRegex(ValueError, '独立重算'):
                module.verify_comparison(data)


if __name__ == '__main__':
    unittest.main()
