import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import opensms_find_iccid_slots_in_excel as scanner

KEY = '8912230000000000001'
OTHER = '8912230000000000002'


class ListExcelTests(unittest.TestCase):
    def test_list_only_preserves_digits(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'list.txt';p.write_text('ICCID\n'+KEY+'\n'+KEY+'F\n58236185891223000050\n')
            records,digest=scanner.read_list(p)
            self.assertEqual([r['iccid'] for r in records],[KEY,KEY,'58236185891223000050'])
            self.assertEqual(records[1]['original'],KEY+'F')
            self.assertEqual(len(digest),64)

    def test_invalid_list_fails_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'list.txt';p.write_text('ICCID\n8.91223E18\n')
            with self.assertRaises(ValueError):scanner.read_list(p)

    def test_full_cli_excel_only_and_all_locations(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source_20260923.xlsx';out=root/'output'
            listed=root/'list.txt';listed.write_text('ICCID\n'+KEY+'\n'+OTHER+'\n')
            w=Workbook();s=w.active;s.append(['ICCID','卡槽'])
            s.append([KEY+'F','112_1.01']);s.append([KEY,'r112_1.01']);s.append(['8912230000000000003','114_1.01']);w.save(source)
            (root/'task_f_lookup_20260923.json').write_text(json.dumps({'rows':[{'queryIccid':OTHER,'slot':'999_1.01'}]}))
            (root/'task_f.sqlite3').write_bytes(b'Not a readable database')
            args=['script','--iccid-list',str(listed),'--root',str(root),'--start','2026-06-24','--end','2026-09-24',
                  '--workers','1','--output',str(out)]
            with patch.object(sys,'argv',args),contextlib.redirect_stdout(io.StringIO()):scanner.main()
            result=json.loads((out/'summary.json').read_text());rows=json.loads((out/'results.json').read_text())
            self.assertEqual(result['targetIccids'],2)
            self.assertEqual(result['matchedIccids'],1)
            self.assertEqual(result['distinctIccidSlotPairs'],2)
            self.assertEqual(result['selectedFiles'],1)
            self.assertFalse(result['databaseRead']);self.assertFalse(result['jsonLogsRead'])
            self.assertTrue(result['outputVerification']['independentlyVerified'])
            self.assertEqual(rows[0]['slots'],['112_1.01','r112_1.01'])
            self.assertEqual(rows[1]['slots'],[])


if __name__=='__main__':unittest.main()
