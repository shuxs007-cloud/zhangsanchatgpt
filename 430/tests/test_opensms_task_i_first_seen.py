import sys
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import opensms_task_i as I
import opensms_task_i_first_seen as F

KEY = '8912230000581572468'


class FirstSeenTests(unittest.TestCase):
    def test_cloud_placeholder_is_excluded_before_open(self):
        with patch.object(F.sys, 'platform', 'darwin'), patch.object(Path, 'stat', return_value=SimpleNamespace(st_flags=0x40000000)), patch.object(I, 'content_key') as read:
            result = F.fingerprint(dict(path='/local/placeholder.xlsx'))
        self.assertEqual(result['status'], 'unreadable')
        self.assertIn('占位符', result['error'])
        read.assert_not_called()

    def test_fingerprint_timeout_becomes_coverage_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'blocked.xlsx'
            path.write_bytes(b'local')
            with patch.object(F, 'READ_TIMEOUT_SECONDS', .02), patch.object(I, 'content_key', side_effect=lambda p: time.sleep(.2)):
                result = F.fingerprint(dict(path=str(path)))
        self.assertEqual(result['status'], 'unreadable')
        self.assertIn('读取超过', result['error'])
        self.assertEqual(F.signal.getitimer(F.signal.ITIMER_REAL)[0], 0)

    def item(self, path):
        st = path.stat()
        return dict(path=str(path), size=st.st_size, mtime=st.st_mtime_ns,
                    signature=I.content_key(path))

    def workbook(self, path, sheet):
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>')
            z.writestr('xl/workbook.xml', f'<workbook xmlns="{I.NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="隐藏来源" sheetId="1" state="hidden" r:id="r1"/></sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>')
            z.writestr('xl/sharedStrings.xml', f'<sst xmlns="{I.NS}"><si><r><t>{KEY[:10]}</t></r><r><t>{KEY[10:]}F</t></r></si></sst>')
            z.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{I.NS}"><sheetData>{sheet}</sheetData></worksheet>')

    def test_normalization_and_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'input.txt'
            p.write_text(f'ICCID\n{KEY}F\n {KEY} \n{KEY}FF\n')
            keys, originals, invalid = F.normalize_list(p)
            self.assertEqual(keys, [KEY])
            self.assertEqual(originals[KEY], KEY + 'F')
            self.assertEqual(len(invalid), 1)

    def test_month_end_and_year(self):
        self.assertEqual(F.previous_month(date(2026, 3, 31)), date(2026, 2, 28))
        self.assertEqual(F.previous_month(date(2026, 1, 16)), date(2025, 12, 16))

    def test_filename_clock_and_parent_clock(self):
        h = F.time_hint('/x/opensms_patrol_20261009T050712+0800/export4day_卡槽关联结果_20261009_050951.xlsx')
        self.assertEqual(h['observed'], '2026-10-09T05:09:51+08:00')
        self.assertEqual(h['precision'], 'second')
        self.assertEqual(F.time_hint('/x/20261009T050712+0800/export4day.xlsx')['observed'], '2026-10-09T05:07:12+08:00')
        self.assertEqual(F.time_hint('/x/export_20261009T010000Z.xlsx')['observed'], '2026-10-09T09:00:00+08:00')

    def test_no_unrelated_folder_mtime_or_template_time(self):
        self.assertIsNone(F.time_hint('/Documents/Codex/2026-09-25/old.xlsx')['observed'])
        h = F.time_hint('/x/report_20261009.xlsx')
        self.assertEqual(h['precision'], 'day')

    def test_day_precision_does_not_invent_midnight_order(self):
        base = dict(path='/x', sheet='s', row=2, cell='A2')
        day = dict(base, observed='2026-10-09T00:00:00+08:00', precision='day')
        early = dict(base, observed='2026-10-09T05:00:00+08:00', precision='second')
        later = dict(base, observed='2026-10-09T10:00:00+08:00', precision='second')
        unknown = dict(base, observed=None, precision='unknown')
        self.assertEqual(len(F.earliest_candidates([day, early, later, unknown])), 2)
        self.assertEqual(F.earliest_candidates([unknown]), [])

    def test_all_sheets_no_header_rich_text_and_cell_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report_20261009T050000+0800.xlsx'
            self.workbook(path, f'<row r="3"><c r="A3" t="s"><v>0</v></c><c r="B3" t="inlineStr"><is><t>{KEY}F</t></is></c><c r="C3"><v>{KEY}</v></c><c r="D3" t="str"><f>"{KEY}"</f><v>{KEY}</v></c></row>')
            result = F.scan(self.item(path), [KEY])
            self.assertEqual(result['status'], 'scanned')
            self.assertEqual({h['cell'] for h in result['hits']}, {'A3', 'B3'})
            self.assertTrue(all(h['sheetState'] == 'hidden' for h in result['hits']))

    def test_csv_all_columns_and_full_value_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'x.csv'
            path.write_text(f'备注,其它\n{KEY}F,{KEY}FF\n号码是{KEY},空\n')
            result = F.scan(self.item(path), [KEY])
            self.assertEqual([(h['row'], h['cell']) for h in result['hits']], [(2, '第1列')])
            self.assertTrue(F.verify_file((self.item(path), result['hits']))['passed'])

    def test_inline_rich_text_or_entities_cannot_be_prefiltered_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rich.xlsx'
            self.workbook(path, f'<row r="2"><c r="A2" t="inlineStr"><is><r><t>{KEY[:3]}</t></r><r><t>{KEY[3:]}F</t></r></is></c><c r="B2" t="inlineStr"><is><t>&#56;{KEY[1:]}F</t></is></c></row>')
            self.assertEqual({h['cell'] for h in F.scan(self.item(path), [KEY])['hits']}, {'A2','B2'})

    def test_plain_unrelated_shared_strings_do_not_need_full_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'unrelated.xlsx'
            self.workbook(path, '<row r="2"><c r="A2" t="s"><v>0</v></c></row>')
            # The fixture's rich-text shared string must use full decoding.
            self.assertTrue(F.possible_text_hit(b'<si><r><t>891</t></r></si>', {KEY}))
            self.assertTrue(F.possible_text_hit(b'<si><t>&#56;91223</t></si>', {KEY}))
            self.assertFalse(F.possible_text_hit(b'<si><t>14389392694</t></si>', {KEY}))

    def test_changed_source_fails_without_partial_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'x.csv'
            path.write_text(KEY)
            item = self.item(path)
            path.write_text(KEY + 'F')
            result = F.scan(item, [KEY])
            self.assertEqual(result['status'], 'unreadable')
            self.assertEqual(result['hits'], [])

    def test_legacy_xls_and_known_blocked_sync_are_coverage_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'x.xls'
            path.write_bytes(b'not an OOXML workbook')
            self.assertEqual(F.scan(self.item(path), [KEY])['status'], 'unreadable')
        blocked = F.fingerprint(dict(path=I.UNAVAILABLE_PREFIX + 'x.xlsx'))
        self.assertEqual(blocked['status'], 'unreadable')


if __name__ == '__main__':
    unittest.main()
