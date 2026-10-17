"""Renewal parser, overlay isolation and independent snapshot validation, offline."""
import importlib.util
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'services/opensms_document_api/pipeline'))
from opensms_f_renewal import source_date,balance,phone,commit_renewals,RULE,digest
from opensms_report_date_cache import freeze
from task_f_renewal_dates import validate_renewal

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
DOC=load('renewal_doc','services/opensms_document_api/pipeline/process_card_slots.py')
VERIFY=load('renewal_verify','services/opensms_document_api/pipeline/opensms_verify_card_slot_fast.py')

class RenewalTests(unittest.TestCase):
    def test_supported_date_formats(self):
        for raw in ('Oct 13, 2026','October 13, 2026','13 October 2026','2026-10-13','2026/10/13','2026.10.13','2026年10月13日','10/13/2026','13/10/2026','2026-10-13 21:05:00','2026-10-13T23:00:00-07:00',date(2026,10,13),datetime(2026,10,13,23)):
            with self.subTest(raw=raw):self.assertEqual(source_date(raw),date(2026,10,13))
        from openpyxl.utils.datetime import to_excel
        self.assertEqual(source_date(to_excel(datetime(2026,10,13))),date(2026,10,13))
        self.assertIsNone(source_date(''))
        for raw in ('01/02/2026','2026-02-30','tomorrow',True,0):
            with self.subTest(raw=raw),self.assertRaises(ValueError):source_date(raw)

    def test_balance_and_phone_precision(self):
        self.assertEqual(balance('CAD 30.00'),Decimal(30));self.assertEqual(balance('$4.45'),Decimal('4.45'))
        self.assertIsNone(balance(None));self.assertEqual(phone('1 (438) 939-2694'),'4389392694')
        for raw in ('NaN','-1',True):
            with self.assertRaises(ValueError):balance(raw)
        with self.assertRaises(ValueError):phone('junk4389392694')

    def proof(self,amount='30'):
        return {'version':1,'authorization':'current-user-renewal-source','rule':RULE,'queryIccid':'8912230200278094459',
                'activationDate':'2026-09-14','baseCutoffDate':'2026-10-13','sourceCutoff':'2026-10-13',
                'cutoffDate':'2026-11-12' if Decimal(amount)>=30 else '2026-10-13','extensionDays':30 if Decimal(amount)>=30 else 0,
                'balanceCad':amount,'status':'充值成功','sourceSha256':'a'*64,'sourceRow':2,'phone':'4389392694',
                'runId':'20261013T120500+0800','slot':'r110_01','backendId':'123'}

    def test_exact_thirty_days_not_balance_months(self):
        for amount in ('0','4.45','20','29.99','30','35','45','50','60'):
            p=self.proof(amount);self.assertEqual(validate_renewal(p,p['queryIccid'],p['activationDate'],p['cutoffDate']).isoformat(),p['cutoffDate'])
        p=self.proof('60');p['cutoffDate']='2026-12-12'
        with self.assertRaises(ValueError):validate_renewal(p,p['queryIccid'],p['activationDate'],p['cutoffDate'])

    def test_snapshot_renewal_and_independent_verifier(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'db.sqlite';output=Path(tmp)/'snapshot.json';p=self.proof()
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE kooda_activation(query_iccid TEXT PRIMARY KEY,activation_date TEXT,cutoff_date TEXT,lookup_status TEXT)')
                c.execute('INSERT INTO kooda_activation VALUES (?,?,?,?)',(p['queryIccid'],p['activationDate'],p['baseCutoffDate'],'found'))
                c.execute('CREATE TABLE kooda_renewal_current(query_iccid TEXT PRIMARY KEY,proof_json TEXT)')
                c.execute('INSERT INTO kooda_renewal_current VALUES (?,?)',(p['queryIccid'],json.dumps(p)))
            result=freeze(db,output,p['runId']);self.assertEqual(result['recordCount'],1)
            cache=json.loads(output.read_text());self.assertEqual(cache['records'][0]['cutoffDate'],'2026-11-12')
            env={'OPENSMS_REPORT_DATE_CACHE_PATH':str(output),'OPENSMS_REPORT_DATE_CACHE_SHA256':digest(output),'OPENSMS_RUN_ID':p['runId']}
            with patch.dict(os.environ,env):
                self.assertEqual(DOC.load_report_date_cache()['index'][p['queryIccid']]['expiry_dt'].strftime('%Y-%m-%d'),'2026-11-12')
            checked=VERIFY.load_frozen_date_snapshot(output,digest(output),p['runId'])
            self.assertEqual(checked['index'][p['queryIccid']]['截止日'].strftime('%Y-%m-%d'),'2026-11-12')
            cache['records'][0]['renewal']['extensionDays']=60
            output.write_text(json.dumps(cache))
            with self.assertRaises((ValueError,RuntimeError)):
                VERIFY.load_frozen_date_snapshot(output,digest(output),p['runId'])
            with sqlite3.connect(db) as c:self.assertEqual(c.execute('SELECT cutoff_date FROM kooda_activation').fetchone()[0],'2026-10-13')

    def test_commit_backup_original_tables_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db=root/'db.sqlite';source=root/'source.fixture';source.write_text('no credentials')
            p=self.proof();source_hash=digest(source)
            item={'sourceRow':2,'phone':p['phone'],'status':p['status'],'balanceCad':'30','sourceCutoff':'2026-10-13','extensionDays':30,'cutoff':'2026-11-12'}
            plan={'rule':RULE,'scope':'current_user_authorized_renewal','source':str(source),'sourceSha256':source_hash,'rows':[item]}
            mapping={'rows':[{'phone':p['phone'],'iccid':p['queryIccid'],'activation':p['activationDate'],'cutoff':p['baseCutoffDate'],'id':'123','slot':p['slot']}]}
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE kooda_activation(query_iccid TEXT PRIMARY KEY,activation_date TEXT,cutoff_date TEXT,lookup_status TEXT)')
                c.execute('CREATE TABLE kooda_inventory(query_iccid TEXT PRIMARY KEY,phone TEXT)')
                c.execute('INSERT INTO kooda_activation VALUES (?,?,?,?)',(p['queryIccid'],p['activationDate'],p['baseCutoffDate'],'found'))
                c.execute('INSERT INTO kooda_inventory VALUES (?,?)',(p['queryIccid'],p['phone']))
            for i in range(2):
                output=root/f'attempt{i}';output.mkdir()
                with patch('opensms_f_renewal.plan',return_value=plan):
                    receipt=commit_renewals(plan,mapping,output,p['runId'],db)
                self.assertTrue(receipt['originalTablesUnchanged']);self.assertTrue(receipt['independentReadbackPassed'])
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM kooda_renewal_events').fetchone()[0],1)
                self.assertEqual(c.execute('SELECT cutoff_date FROM kooda_activation').fetchone()[0],'2026-10-13')

if __name__=='__main__':unittest.main()
