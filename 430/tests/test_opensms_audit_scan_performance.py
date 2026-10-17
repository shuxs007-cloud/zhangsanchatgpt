import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('audit_scan_perf', ROOT / 'scripts/opensms_parallel_audit.py')
AUDIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


def fixture(path):
    book = Workbook()
    sheet = book.active
    sheet.title = 'sheet1'
    sheet.append(['电话', '运营商', '其它', '换号状态', 'koo是否2天', '激活后天数'])
    for i in range(8):
        sheet.append(['12345678901', 'B11', '00123', None, '2天', i])
    sheet['G2'] = '=1/0'
    sheet['H2'] = '#DIV/0!'
    sheet['I2'] = '123456789012345678901'
    sheet['J2'] = True
    extra = book.create_sheet('后台更新未在源表')
    extra.append(['电话', '最新运营商', '原始其它'])
    extra.append(['99999999999', 'B12', '00123'])
    extra.append(['12345678901', 'SHOULD_NOT_REPLACE', '00123'])
    extra.append(['99999999999', 'SHOULD_NOT_REPLACE', '00123'])
    book.save(path)


class AuditScanPerformanceTests(unittest.TestCase):
    def test_selected_columns_preserve_duplicates_and_supplemental_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.xlsx'
            fixture(path)
            with patch.object(AUDIT, 'cell_value', wraps=AUDIT.cell_value) as value:
                rows = AUDIT.xlsx_main_phone_carriers(path)
            self.assertEqual(rows, {'2345678901': ['B11'] * 8, '9999999999': ['B12']})
            # 6+3 headers, two requested cells per data row (8+3).
            self.assertEqual(value.call_count, 9 + 2 * 11)

    def test_structure_parses_main_once_and_retains_formula_errors_and_text_types(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.xlsx'
            fixture(path)
            with ZipFile(path) as archive:
                main_xml = archive.read('xl/worksheets/sheet1.xml')
            parser = ET.fromstring
            main_reads = []

            def parse(raw, *args, **kwargs):
                if raw == main_xml:
                    main_reads.append(1)
                return parser(raw, *args, **kwargs)

            with patch.object(AUDIT.ET, 'fromstring', side_effect=parse):
                result = AUDIT.inspect_result_workbook(path, '2026-09-18', False)
            self.assertEqual(len(main_reads), 1)
            self.assertEqual(result['main_rows'], 8)
            self.assertEqual(result['formula_errors'], [{'sheet': 'sheet1', 'cell': 'H2', 'error': '#DIV/0!'}])
            review = result['numeric_text_review']
            self.assertFalse(review['conversion_enabled'])
            self.assertIn('00123', [row['value'] for row in review['examples']])
            self.assertIn('123456789012345678901', [row['value'] for row in review['examples']])

    def test_classification_cache_is_private_to_each_independent_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.xlsx'
            fixture(path)
            with patch.object(AUDIT, 'numeric_text_status', wraps=AUDIT.numeric_text_status) as classify:
                first = AUDIT.inspect_result_workbook(path, None, False)
                calls = classify.call_count
                self.assertEqual(sum(c.args[0] == '00123' for c in classify.call_args_list), 1)
                second = AUDIT.inspect_result_workbook(path, None, False)
            self.assertEqual(classify.call_count, calls * 2)
            self.assertEqual(first, second)

    def test_cell_values_cover_shared_inline_rich_formula_cache_and_empty(self):
        ns = AUDIT.MAIN_NS
        for body, expected in [
            ('<c t="s"><v>0</v></c>', '00123'),
            ('<c t="s"><v>99</v></c>', ''),
            ('<c t="inlineStr"><is><r><t>12</t></r><r><t>34</t></r></is></c>', '1234'),
            ('<c t="str"><f>TEXT(1)</f><v>001</v></c>', '001'),
            ('<c t="b"><v>1</v></c>', '1'), ('<c/>', ''),
        ]:
            cell = ET.fromstring(body.replace('<c', f'<c xmlns="{ns}"', 1))
            self.assertEqual(AUDIT.cell_value(cell, ['00123']), expected)

    def test_malformed_unselected_cell_xml_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            path, corrupt = Path(folder) / 'fixture.xlsx', Path(folder) / 'corrupt.xlsx'
            fixture(path)
            with ZipFile(path) as source, ZipFile(corrupt, 'w', ZIP_DEFLATED) as target:
                for info in source.infolist():
                    content = source.read(info.filename)
                    if info.filename == 'xl/worksheets/sheet1.xml':
                        content = content.replace(b'</worksheet>', b'<broken></worksheet>')
                    target.writestr(info, content)
            with self.assertRaises(ET.ParseError):
                AUDIT.xlsx_main_phone_carriers(corrupt)
            with self.assertRaises(ET.ParseError):
                AUDIT.inspect_result_workbook(corrupt, None, False)


if __name__ == '__main__':
    unittest.main()
