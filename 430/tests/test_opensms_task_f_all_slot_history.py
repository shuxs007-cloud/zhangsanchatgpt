import gzip
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import opensms_task_f_all_slot_history as scan

KEY = '8912230000000000001'
OTHER = '8912230000000000002'


class FullHistoryTests(unittest.TestCase):
    def test_union_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'test.sqlite3'
            c = sqlite3.connect(p)
            c.execute('CREATE TABLE kooda_activation(query_iccid,source_slot,lookup_status,queried_at)')
            c.execute('CREATE TABLE kooda_inventory(query_iccid,slot,last_seen_at)')
            c.execute('INSERT INTO kooda_activation VALUES(?,?,?,?)', (KEY, 'r112_1.01', 'found', '2026-09-23'))
            c.executemany('INSERT INTO kooda_inventory VALUES(?,?,?)', [(KEY, '', ''), (OTHER, '', '')])
            c.commit(); c.close()
            targets = scan.frozen_targets(p)
            self.assertEqual(set(targets), {KEY, OTHER})
            self.assertEqual(targets[KEY]['lookupStatus'], 'found')

    def test_json_identity_no_tail_or_mismatch(self):
        self.assertEqual(scan.json_identity({'queryIccid':KEY, 'cardStore':KEY+'F'}), KEY)
        self.assertIsNone(scan.json_identity({'queryIccid':KEY, 'cardStore':OTHER}))
        self.assertIsNone(scan.json_identity({'queryIccid':int(KEY)}))

    def make_scan(self, rows):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'x.xlsx'
            w = Workbook(); s = w.active
            for row in rows:
                s.append(row)
            w.save(p)
            scan.init_worker({KEY}, folder, '2026-06-24', '2026-09-24')
            with zipfile.ZipFile(p) as z:
                return scan.fast_sheet(z.read('xl/worksheets/sheet1.xml'), [], '')

    def test_fast_selects_correct_column_and_preserves_slot(self):
        ev, un, _ = self.make_scan([['备注', 'ICCID', '卡槽', '其它ICCID'],
                                  [OTHER, KEY+'F', 'r00112_01.01', OTHER]])
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]['iccidCell'], 'B2')
        self.assertEqual(ev[0]['slot'], 'r00112_01.01')

    def test_shared_strings_and_numeric_rejection(self):
        scan.init_worker({KEY}, '/tmp', '', '')
        xml = (f'<worksheet xmlns="{scan.base.NS}"><sheetData>'
               '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
               '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c></row>'
               f'<row r="3"><c r="A3"><v>{KEY}</v></c><c r="B3" t="s"><v>3</v></c></row>'
               '</sheetData></worksheet>').encode()
        ev, un, _ = scan.fast_sheet(xml, ['ICCID', '卡槽', KEY+'F', '00112_01.01'], 'test')
        self.assertEqual([r['row'] for r in ev], [2])
        self.assertEqual(un[0]['reason'], 'ICCID非可靠文本或公式')

    def test_no_headers_or_formula_or_numeric_not_inferred(self):
        self.assertFalse(self.make_scan([[KEY, '112_1.01']])[0])
        self.assertFalse(self.make_scan([['ICCID', '卡槽'], [KEY, '=1+1']])[0])
        self.assertFalse(self.make_scan([['ICCID', '卡槽'], ['="'+KEY+'"', '112_1.01']])[0])

    def test_multiple_columns_marked(self):
        ev, _, _ = self.make_scan([['系统ICCID', 'GoogleICCID', '卡槽'], [KEY, OTHER, '112_1.01']])
        self.assertTrue(ev[0]['ambiguousColumns'])

    def test_json_ledger_and_nested_exact_only(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder); (out/'逐条证据').mkdir()
            p = out/'receipt.json'
            p.write_text(json.dumps({'status':'passed', 'rows':[
                {'queryIccid':KEY,'slot':'112_1.01','sourceOccurrences':[
                    {'queryIccid':KEY,'slot':'r112_1.01'},
                    {'slot':'115_1.01'},
                    {'queryIccid':KEY, 'cardStore':OTHER, 'slot':'113_1.01'}]}]}))
            st = p.stat(); item = scan.base.fingerprint(dict(path=str(p), size=st.st_size, mtimeNs=st.st_mtime_ns, format='json'))
            scan.init_worker({KEY}, folder, '2026-06-24', '2026-09-24')
            r = scan.scan_file(item)
            self.assertNotIn('error', r)
            self.assertEqual(r['evidenceRows'], 2)
            with gzip.open(r['detailFile'], 'rt') as f:
                self.assertEqual(len(list(f)), 3)

    def test_fail_closed_json_status(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder); (out/'逐条证据').mkdir()
            p = out/'receipt.json'; p.write_text(json.dumps({'status':'failed','rows':[]}))
            st = p.stat(); item = scan.base.fingerprint(dict(path=str(p), size=st.st_size, mtimeNs=st.st_mtime_ns, format='json'))
            scan.init_worker({KEY}, folder, '', '')
            r = scan.scan_file(item)
            self.assertIn('error', r); self.assertFalse(r['aggregates'])

    def test_aggregate_no_duplicate_alias_counts_or_r_merge(self):
        with tempfile.TemporaryDirectory() as folder:
            c = scan.make_audit_db(Path(folder)/'index.sqlite3')
            aliases=[dict(path=p, date=d, dateBasis='test', sha256='x',format='excel')
                     for p,d in [('opensms_a.xlsx','2026-08-01'),('opensms_b.xlsx','2026-08-02')]]
            result=dict(detailFile='x.gz',evidenceRows=2,sha256='x',aggregates=[
                dict(iccid=KEY,slot=s,sheet='sheet1',ambiguous=False,sourceKind='',count=1,example='A2')
                for s in ['112_1.01','r112_1.01']])
            scan.record_result(c,result,aliases)
            self.assertEqual(c.execute('SELECT SUM(observations) FROM slots').fetchone()[0],2)
            r=scan.card_summaries(c,{KEY:dict(origin='test',lookupStatus='found')})[0]
            self.assertEqual(len(r['slots']),2)
            c.close()

    def test_independent_excel_hash_and_text(self):
        with tempfile.TemporaryDirectory() as folder:
            result = scan.export_excel(Path(folder)/'x.xlsx', [('全部任务F卡', ['ICCID','备注'], [[KEY,'=1+1']])])
            self.assertTrue(result['independentlyVerified'])
            self.assertEqual(result['sheets']['全部任务F卡'],1)


if __name__ == '__main__':
    unittest.main()
