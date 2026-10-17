"""Independent consumers agree on bounded offline-identity evidence; no I/O."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import unittest

PIPELINE = Path(__file__).resolve().parents[1] / 'services/opensms_document_api/pipeline'
sys.path.insert(0, str(PIPELINE))
from archived_system_identity import index_phones, match_archived


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, PIPELINE / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


GEN = module('archive_test_generator', 'process_card_slots.py')
VER = module('archive_test_verifier', 'opensms_verify_card_slot_fast.py')


class ArchivedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.row = {'电话': '14385550123', '卡槽': '130_1.01', '状态': '上架',
                    '创建时间': '2026-09-01 00:00:00', 'ICCID': '8912230200081836799F'}
        self.backend = {'id': 1, 'cardNo': '4385550123', 'slotIdentif': None,
                        'showAble': False, 'status': '猫池空号已下架', 'machineName': '130',
                        'createTime': '2026-09-01 00:00:00', 'cardStore': '8912230200081836799f',
                        'chatrActivateDate': None, 'chatrPlanExpire': None}

    def assert_consumers(self, row, records, expected):
        before = copy.deepcopy((row, records))
        index = index_phones(records)
        kwargs = dict(phone=row['电话'], slot=row['卡槽'], status=row.get('状态'),
                      created=row.get('创建时间'), iccid=row.get('ICCID'))
        self.assertEqual(match_archived(index, **kwargs) is not None, expected)
        source = {'index': {}, 'phone_index': index}
        if expected:
            self.assertEqual(GEN.resolve_system_kooda_row(source, **kwargs,
                has_iccid='ICCID' in row)['backend_id'], str(records[0]['id']))
        else:
            with self.assertRaises(RuntimeError):
                GEN.resolve_system_kooda_row(source, **kwargs, has_iccid='ICCID' in row)
        snapshot = {'index': {}, 'phone_index': {key: [{'raw': raw} for raw in hits]
                    for key, hits in index.items()}}
        verified = VER.find_system_record(snapshot, row)
        self.assertEqual(verified is not None, expected)
        if expected:
            self.assertEqual(verified['raw']['id'], records[0]['id'])
        self.assertEqual((row, records), before)

    def test_exact_empty_pool_identity_preserves_original_historical_row(self):
        for status in ('上架', '下架'):
            self.assert_consumers({**self.row, '状态': status}, [self.backend], True)

    def test_any_missing_conflicting_or_ambiguous_proof_fails_in_every_consumer(self):
        for key, value in [('状态', '注册失败'), ('状态', None), ('电话', '123'),
                           ('卡槽', ''), ('卡槽', '131_1.01'), ('创建时间', ''),
                           ('创建时间', '2026-09-01 00:00:01'), ('ICCID', None),
                           ('ICCID', 8912230200081836799), ('ICCID', '8912230200081836798F')]:
            with self.subTest(source=(key, value)):
                self.assert_consumers({**self.row, key: value}, [self.backend], False)
        for key, value in [('showAble', True), ('showAble', 0), ('showAble', None),
                           ('status', '注册失败'), ('status', '其他已下架'), ('status', '换卡已下线'),
                           ('machineName', '131'), ('slotIdentif', '130_2.01'),
                           ('createTime', '2026-09-01 00:00:01'), ('cardStore', ''),
                           ('cardStore', '8912230200081836798F')]:
            with self.subTest(inventory=(key, value)):
                self.assert_consumers(self.row, [{**self.backend, key: value}], False)
        self.assert_consumers(self.row, [self.backend, {**self.backend, 'id': 2}], False)
        self.assert_consumers({**self.row, 'ICCID': ''}, [{**self.backend, 'cardStore': ''}], False)

    def test_legacy_off_shelf_blank_pair_does_not_expand_to_listed_source(self):
        raw = {**self.backend, 'status': '换卡已下线', 'cardStore': ''}
        self.assert_consumers({**self.row, '状态': '下架', 'ICCID': ''}, [raw], True)
        self.assert_consumers({**self.row, 'ICCID': ''}, [raw], False)

    def test_confirmed_swapped_lifecycle_requires_all_offline_flags(self):
        raw = {**self.backend, 'status': '换卡已下线', 'displayStatus': '卡槽异常',
               'showInTable': False, 'userAble': False}
        self.assert_consumers(self.row, [raw], True)
        self.assert_consumers({**self.row, 'ICCID': ''}, [{**raw, 'cardStore': None}], True)
        for key, value in [('displayStatus', None), ('showInTable', True),
                           ('showInTable', 0), ('userAble', None), ('userAble', 0),
                           ('showAble', True), ('slotIdentif', '130_1.01'),
                           ('machineName', '131'), ('createTime', '2026-09-01 00:00:01'),
                           ('cardStore', '8912230200081836798F')]:
            with self.subTest(key=key, value=value):
                self.assert_consumers(self.row, [{**raw, key: value}], False)
        self.assert_consumers(self.row, [raw, {**raw, 'id': 2}], False)
        self.assert_consumers({**self.row, 'ICCID': ''}, [raw], False)

    def test_workbook_generation_and_independent_readback_preserve_snapshot_status(self):
        # Use the existing isolated workbook fixture, but invoke Python stages
        # directly: no shell here-document, browser, network, DB or real runner.
        from test_opensms_system_kooda_integration import SystemKoodaIntegrationTests
        from openpyxl import load_workbook
        fixture = SystemKoodaIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        book = load_workbook(fixture.raw)
        sheet = book['sheet1']; col = sheet.max_column + 1
        sheet.cell(1, col, 'ICCID')
        sheet.cell(2, col, self.row['ICCID'])
        sheet.cell(3, col, '8912230200081836798f')
        book.save(fixture.raw); book.close()
        payload = json.loads(fixture.system.read_text())
        payload['systemRows'][0].update(slotIdentif=None, showAble=False, status='换卡已下线',
            displayStatus='卡槽异常', showInTable=False, userAble=False,
            machineName='130', createTime='2026-09-01 00:00:00')
        fixture.system.write_text(json.dumps(payload))
        env = {**fixture.env, 'OPENSMS_MAIN_PATH': str(fixture.raw),
               'OPENSMS_OUTPUT_DIR': str(fixture.output.parent), 'OPENSMS_REPORT_ROOT': str(PIPELINE),
               'OPENSMS_GOOGLE_ASSOCIATION_ENABLED': '0', 'EXPORT4DAY_SERVER_DATE': '2026-09-06',
               'OPENSMS_SYSTEM_KOODA_SHA256': hashlib.sha256(fixture.system.read_bytes()).hexdigest(),
               'PROCESS_JSON_PATH': str(fixture.root / 'process.json'),
               'ENRICH_JSON_PATH': str(fixture.root / 'enrichment.json')}
        before = fixture.raw.read_bytes()
        generated = subprocess.run([sys.executable, '-B', str(PIPELINE / 'process_card_slots.py')],
            env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(generated.returncode, 0, generated.stderr[-3000:])
        process = json.loads(generated.stdout)
        Path(env['PROCESS_JSON_PATH']).write_text(generated.stdout)
        Path(env['ENRICH_JSON_PATH']).write_text(json.dumps(process['enrichment']))
        self.assertEqual(process['system_kooda_source']['archived_matched_rows'], 1)
        checked = subprocess.run([sys.executable, '-B', str(PIPELINE / 'opensms_verify_card_slot_fast.py'),
            str(fixture.output), str(fixture.raw)], env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(checked.returncode, 0, checked.stderr[-3000:])
        review = json.loads(checked.stdout)
        for name in ('system_kooda_source_review', 'date_resolution_review', 'slot_alias_review'):
            self.assertTrue(review[name]['matches'], name)
        self.assertEqual(review['formula_error_matches'], 0)
        self.assertEqual(review['system_kooda_source_review']['matched_rows'], 2)
        final = load_workbook(fixture.output, read_only=True)
        try:
            sheet = final['sheet1']; values = list(sheet.values)
            columns = {name: n for n, name in enumerate(values[0])}
            self.assertEqual(values[1][columns['状态']], '上架')  # Export snapshot, not a live-state assertion.
            self.assertEqual(values[1][columns['卡槽']], '130_1')
            self.assertEqual(values[1][columns['ICCID']], self.row['ICCID'])
            self.assertEqual(len(values) - 1, 3)
        finally:
            final.close()
        self.assertEqual(fixture.raw.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
