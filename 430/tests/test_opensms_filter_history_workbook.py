from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from opensms_filter_history_workbook import filter_sheet, ROW, values

A='89302720535822804694'
B='8912230200278097270'


class FilterTests(unittest.TestCase):
    def xml(self, first=A):
        return ('<worksheet><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>ICCID</t></is></c></row>'
                f'<row r="2"><c r="A2" s="2" t="inlineStr"><is><t>{first}</t></is></c></row>'
                f'<row r="3"><c r="A3" s="2" t="inlineStr"><is><t>{B}</t></is></c><c r="B3" s="3" t="inlineStr"><is><t>保留原值</t></is></c></row>'
                '</sheetData><autoFilter ref="A1:B3"/></worksheet>').encode()

    def test_exact_filter_reindexes_and_keeps_style(self):
        out,stats=filter_sheet('历史记录',self.xml(),{A},1)
        self.assertEqual(stats['removed'],1)
        rows=list(ROW.finditer(out))
        self.assertEqual(values(rows[-1].group())['A'],B)
        self.assertIn(b'r="B2" s="3"',out)
        self.assertIn(b'ref="A1:B2"',out)
        self.assertNotIn(A.encode(),out)

    def test_trailing_f(self):
        _,stats=filter_sheet('全部字段',self.xml(A+'F'),{A},1)
        self.assertEqual(stats['removed'],1)

    def test_not_partial_match(self):
        out,stats=filter_sheet('历史记录',self.xml('1'+A),{A},2)
        self.assertEqual(stats['removed'],0)

    def test_unrelated_row_reference_requires_review(self):
        xml=self.xml(B).replace('保留原值'.encode(),A.encode())
        with self.assertRaises(ValueError):filter_sheet('全部字段',xml,{A},1)


if __name__=='__main__':unittest.main()
