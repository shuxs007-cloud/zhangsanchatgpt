import tempfile
from pathlib import Path
import unittest
from openpyxl import Workbook
from scripts.opensms_device_overall_report import aggregate, FIELDS
from scripts.opensms_finalize_patrol_report import device_daily_report_lines


class DeviceOverallTests(unittest.TestCase):
    def test_weighting_prefix_zero_and_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.xlsx'
            book = Workbook()
            book.active.title = 'sheet1'
            book.active.append(['卡槽', '机房', *FIELDS])
            for row in [('110_01','二',100,10,500,30),
                        ('110_2.01','二',900,9,4500,20),
                        ('r110_01','二',25,1,80,2),
                        ('111_01','二',0,0,0,0),
                        ('112_01','二',None,1,30,2)]:
                book.active.append(row)
            book.save(path)
            book.close()
            result = {r[0]: r for r in aggregate(path)}
            self.assertEqual(result['110'][2:8], [2,1000,19,.019,5000,50])
            self.assertEqual(result['r110'][5], .04)
            self.assertIsNone(result['111'][5])
            self.assertIsNone(result['112'][3])
            self.assertIsNone(result['112'][5])

    def test_report_hides_subprogram_timings(self):
        report = {'process': {'device_daily_statistics': {
            'device_count': 1, 'aggregation_seconds': 98765,
            'write_seconds': 87654}}, 'verification': {}}
        text = '\n'.join(device_daily_report_lines(report))
        self.assertNotIn('98765', text)
        self.assertNotIn('87654', text)
        source = Path('scripts/opensms_finalize_patrol_report.py').read_text()
        self.assertNotIn('| 子程序：', source)
        self.assertNotIn('| 阶段耗时：', source)


if __name__ == '__main__':
    unittest.main()
