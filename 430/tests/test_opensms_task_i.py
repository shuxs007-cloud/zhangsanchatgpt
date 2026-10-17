import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import sys

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import opensms_task_i as I
import opensms_task_i_pipeline as P

class TaskITests(unittest.TestCase):
    def test_current_positions_are_explicit_and_offline_default_stays_offline(self):
        key='8912230200278094459'
        self.assertFalse(P.validate_current_position_request(SimpleNamespace(),[key]))
        args=SimpleNamespace(current_positions=True,authorization_source='current-user',current_search_receipt=None)
        self.assertTrue(P.validate_current_position_request(args,[key]))
        for invalid in (
            SimpleNamespace(current_positions=True),
            SimpleNamespace(authorization_source='current-user'),
            SimpleNamespace(current_positions=True,authorization_source='current-user',current_search_receipt=Path('/old.json')),
        ):
            with self.assertRaises(ValueError):P.validate_current_position_request(invalid,[key])
        for keys in ([key,'14389392694'],[str(8912230200278094459+n) for n in range(501)]):
            with self.assertRaises(ValueError):P.validate_current_position_request(args,keys)

    def test_current_export_is_once_and_attached_before_report(self):
        result=dict(queries=[dict(query='8912230200278094459',queryType='iccid')],totals=[100,5,130,8])
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)
            def fake_export(command,**kwargs):
                directory=Path(command[3]);directory.mkdir()
                (directory/'export_receipt.json').write_text(json.dumps(dict(numberManagementRequestCount=3)))
                self.assertEqual(command[-1],'--current-user-authorized-task-i-search-export')
                self.assertEqual(kwargs['cwd'],P.WORKDIR)
                self.assertTrue(kwargs['check'])
            def fake_attach(data,path):data['currentPositionSource']=dict(status='passed')
            with patch.object(P.subprocess,'run',side_effect=fake_export) as export,patch.object(I,'attach_current_positions',side_effect=fake_attach) as attach:
                receipt=P.export_current_positions(result,out)
                self.assertEqual(export.call_count,1)
                attach.assert_called_once_with(result,out/'current_search_export'/'export_receipt.json')
            self.assertEqual(receipt['numberManagementRequestCount'],3)
            self.assertEqual(result['totals'],[100,5,130,8])
            self.assertEqual(result['currentPositionSource']['authorizationSource'],'current-user')

    def test_current_export_failure_does_not_retry_or_build_fallback(self):
        result=dict(queries=[dict(query='8912230200278094459',queryType='iccid')])
        with tempfile.TemporaryDirectory() as tmp,patch.object(P.subprocess,'run',side_effect=P.subprocess.CalledProcessError(1,['export'])) as export,patch.object(I,'attach_current_positions') as attach:
            with self.assertRaisesRegex(RuntimeError,'最新位置搜索导出失败，未重试'):
                P.export_current_positions(result,Path(tmp))
            self.assertEqual(export.call_count,1)
            attach.assert_not_called()

    def test_pipeline_normalized_input_and_dynamic_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'第二批.txt';path.write_text('8912230200278094459F\n+1 (438) 939-2694\n\n')
            keys,batches,count=P.normalize_inputs(['8912230200278094459','14389392694'],[path])
            self.assertEqual(keys,['8912230200278094459','14389392694'])
            self.assertEqual(count,4)
            self.assertEqual(set(batches.values()),{'查询清单'})
            with self.assertRaises(ValueError):P.normalize_inputs(['错误'],[])
    def test_pipeline_no_data_and_cross_query_aggregation(self):
        key='8912230200278094459';phone='14389392694'
        queries=[dict(query=k,documentCount=1,dailyRecordCount=1) for k in (key,phone)]
        base=dict(date='2026-10-10',iccid=key,phone='4389392694',path='/x.xlsx',values=[100,5,130,8])
        result=dict(queries=queries,daily=[dict(base,query=k) for k in (key,phone)])
        P.prepare_result(result,{key:'一批',phone:'一批'},dict(documentCount=1))
        self.assertIsNone(result['batches'][0]['totals'])
        self.assertIsNone(result['batches'][1]['totals'])
        self.assertEqual(result['usedSources'][0]['selectedDailyRows'],2)
        self.assertEqual(P.choose_samples([]),[])
    def test_slot_history_latest_ties_and_unknown_times(self):
        base=dict(issue='',path='/x/example.xlsx',sheet_name='s',row_number=2,iccid_cell='B2',phone_cell='A2',iccid='8912230200278094459',phone='4389392694')
        old=dict(base,slot='101_35.01',observed='2026-10-10T10:00:00',date_basis='文件名或归档运行编号')
        latest=dict(base,slot='102_8.01',observed='2026-10-11T10:00:00',date_basis='文件名或归档运行编号')
        unknown_time=dict(base,slot='999_1.01',observed='2026-10-12T10:00:00',date_basis='修改时间，非确定采集时间')
        ambiguous=dict(base,slot='888_1.01',observed='2026-10-12T10:00:00',date_basis='文件名或归档运行编号',issue='同条多个ICCID不一致，统计排除')
        r=I.slot_summary([latest,old,unknown_time,ambiguous])
        self.assertEqual(r['historicalSlots'],['101_35.01','102_8.01','999_1.01'])
        self.assertEqual(r['latestSlot'],'102_8.01')
        tie=I.slot_summary([dict(latest,slot='103_1.01')])
        merged=I.merge_slot_summaries([r,tie])
        self.assertIsNone(merged['latestSlot'])
        self.assertEqual(merged['latestSlots'],['102_8.01','103_1.01'])
        self.assertEqual(merged['latestSlotStatus'],'conflicting-same-time')
        self.assertEqual(I.slot_summary([unknown_time])['latestSlotStatus'],'time-unknown')
        self.assertEqual(I.slot_summary([])['latestSlotStatus'],'no-slot-record')
    def test_identity_text_and_padding(self):
        self.assertEqual(I.iccid_key(' 8912230200278094459F '),'8912230200278094459')
        self.assertIsNone(I.iccid_key(8912230200278094459))
        self.assertIsNone(I.iccid_key('8912230200278094459FF'))
        self.assertEqual(I.phone_key('+1 (438) 939-2694'),'4389392694')
        self.assertIsNone(I.phone_key('123.4'))

    def test_year_rollover_and_missing_metrics(self):
        h={str(i):(s,'str',False,'') for i,s in enumerate(['12-31'+m for m in I.METRICS])}
        self.assertEqual(set(I.metric_columns(h,'2026-01-01T05:00:00')),{'2025-12-31'})
        self.assertIsNone(I.count_cell(('', 'n',False,'')))
        self.assertEqual(I.count_cell(('0','n',False,'')),0)
        self.assertIsNone(I.count_cell(('1','n',True,'')))

    def test_header_root_xml_declaration(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'x.zip'
            with zipfile.ZipFile(p,'w') as z:z.writestr('sheet.xml',f'<?xml version="1.0"?><worksheet xmlns="{I.NS}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>ICCID</t></is></c></row></sheetData></worksheet>')
            with zipfile.ZipFile(p) as z:
                fr,ns=I.peek(z,'sheet.xml');n,v=I.read_cells(fr[0],[],ns)
                self.assertEqual(v['A'][0],'ICCID')
                n,fast=I.read_selected_cells(fr[0],[],{'A'})
                self.assertEqual(fast,v)

    def test_same_day_snapshots_not_added_and_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'index.sqlite3';db=sqlite3.connect(p);db.executescript(I.SCHEMA)
            db.execute("INSERT INTO meta VALUES('updated_at','2026-10-12T10:00:00')")
            db.execute("INSERT INTO sheets VALUES(1,'s','{}',1)")
            key='8912230200278094459'
            for n,(obs,values,latest) in enumerate([('2026-10-11T10:00:00',[100,5,130,8],'2026-10-10'),('2026-10-12T10:00:00',[120,6,150,9],'2026-10-11')],1):
                db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?)',(n,f'/x/OpenSMS近10日新鲜导出_{n}.xlsx',str(n),1,1,obs,'文件名或归档运行编号',1,'已检查',''))
                db.execute('INSERT INTO file_sheets VALUES(?,?,?)',(n,n,'sheet1'))
                if n==2:db.execute("INSERT INTO sheets VALUES(2,'s2','{}',1)")
                db.execute('INSERT INTO records(sheet_id,row_number,iccid,phone,phone_raw,iccid_cell,phone_cell,iccid_label,slot,carrier,created_raw,daily_json,latest_day,issue) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(n,2,key,'4389392694','14389392694','B2','A2','ICCID','x','A1','',json.dumps({'2026-10-10':values}),latest,''))
            db.commit();db.close()
            r=I.query(p,[key,'8912230200000000000'])
            self.assertEqual(r['totals'],[120,6,150,9])
            self.assertEqual(r['queries'][0]['replyRate'],.05)
            self.assertEqual(r['queries'][0]['documentCount'],2)
            self.assertEqual(r['queries'][1]['totals'],[None]*4)
            self.assertEqual(r['daily'][0]['duplicateSnapshots'],2)
            compact=I.parallel_query(p,[key,'8912230200000000000'],workers=2)
            for field in ('totals','daily','conflicts','unknown','indexedAt','phoneSlots'):
                self.assertEqual(compact[field],r[field])
            for expected,actual in zip(r['queries'],compact['queries']):
                self.assertEqual({k:v for k,v in expected.items() if k!='locations'},actual)
            self.assertEqual(compact['execution']['actualWorkers'],2)
            self.assertNotIn('locations',compact['queries'][0])
            self.assertEqual(compact['queries'][0]['locationCount'],2)
            overlapping=I.parallel_query(p,[key,'14389392694'],workers=2)
            self.assertIsNone(overlapping['totals'])
            self.assertIn('portfolioWarning',overlapping)
            duplicate=I.parallel_query(p,[key,key],workers=2)
            self.assertEqual(duplicate['uniqueInputCount'],1)
            self.assertEqual(duplicate['totals'],r['totals'])

if __name__=='__main__':unittest.main()
