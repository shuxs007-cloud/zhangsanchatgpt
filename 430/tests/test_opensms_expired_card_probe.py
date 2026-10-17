import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from opensms_expired_card_probe_audit import key_for,verify_receipt
from opensms_chat_summary import render_summary

class ProbeAuditTests(unittest.TestCase):
    def fixture(self,directory):
        path=Path(directory)/'receipt.json'
        row={'id':'1','cardNo':'6470000001','cardStore':'12345678901234567891F',
             'slotIdentif':'101_1','carrireIdentif':'10','chatrPlanExpire':'2026-10-01',
             'displayStatus':'未上架','userAble':False}
        key=key_for(row)
        zones=[{'name':'过期下架卡T','identif':'10'},{'name':'过期下架卡C','identif':'20'}]
        item={'key':key,'id':'1','phone':row['cardNo'],'iccid':'12345678901234567891',
              'slot':'101_1','carrierIdentif':'10','cutoff':'2026-10-01','initialState':'offline'}
        def write(suffix,value):
            p=Path(str(path)+suffix);p.write_text(json.dumps(value));return p,hashlib.sha256(p.read_bytes()).hexdigest()
        population,pop_hash=write('.population.json',{'today':'2026-10-02','zones':zones,'first':[row],'second':[row]})
        write('.initial_readback_0.json',{'rows':[row]})
        write('.prior_ledger_0.json',[{'key':key,'previous':None}])
        write('.restore_intent_0.json',{'items':[item],'method':'POST','endpoint':'/phone/config/offlineRecover','body':{'phoneIds':['1']}})
        write('.restore_acceptance_0.json',{'code':200,'data':{'acceptedCount':1,'skippedCount':0}})
        write('.restore_readback_0.json',{'rows':[{**row,'displayStatus':'已上架','userAble':True}]})
        receipt={'version':1,'runId':'current','scope':'expired_zones_deadline_before_platform_today_v1',
                 'status':'passed','serverDate':'2026-10-02','zones':zones,'candidateCount':1,'populationCount':1,
                 'unknownDateCount':0,'ineligibleStatusCount':0,'invalidIdentityCount':0,'notExpiredCount':0,'onlineVerifiedCount':1,
                 'offlineSubmittedCount':0,'recoverySubmittedCount':1,'acceptedCount':1,'pendingCount':0,
                 'blockedCount':0,'alreadyTestedSkippedCount':0,'dryRun':False,'retryCount':0,'carrierWriteCount':0,
                 'populationReceipt':str(population),'populationSha256':pop_hash,
                 'results':[{**item,'action':'restore_once','status':'online_verified'}]}
        proofs=[]
        for kind in ('phone','day'):
            p,sha=write('.'+kind+'.json',{'serverDate':'2026-10-02'});proofs.append({'path':str(p),'sha256':sha})
        p,sha=write('.input.json',{'runId':'current','serverDate':'2026-10-02','serverDateVerified':True,'dateReceipts':proofs})
        receipt.update(inputPath=str(p),inputSha256=sha,dateReceipts=proofs)
        _,sha=write('',receipt)
        return {**receipt,'receipt':str(path),'receiptSha256':sha},write

    def modernize(self,probe,write):
        probe.update(scope='expired_zones_all_listing_states_v2',deadlineFilterEnabled=False)
        inputs=json.loads(Path(probe['inputPath']).read_text())
        inputs.update(scope=probe['scope'],deadlineFilterEnabled=False)
        _,probe['inputSha256']=write('.input.json',inputs)

    def save_receipt(self,probe,write):
        _,probe['receiptSha256']=write('',{k:v for k,v in probe.items() if k not in ('receipt','receiptSha256')})

    def test_missing_or_invalid_identity_is_skipped_only_in_population(self):
        mutations=[('cardNo','missing'),('cardStore','missing'),('slotIdentif','missing'),
                   ('cardNo',None),('cardNo',6470000002),('cardStore',12345678901234567892),
                   ('slotIdentif',None),('slotIdentif',' '),('id',9007199254740992),
                   ('cardNo','６４７００００００２')]
        for field,value in mutations:
            with self.subTest(field=field,value=value),tempfile.TemporaryDirectory() as d:
                probe,write=self.fixture(d);self.modernize(probe,write)
                population=json.loads(Path(probe['populationReceipt']).read_text())
                invalid={**population['second'][0],'id':'2','cardNo':'6470000002'}
                if value=='missing':invalid.pop(field)
                else:invalid[field]=value
                population['first'].append(invalid);population['second'].append(invalid)
                _,probe['populationSha256']=write('.population.json',population)
                probe.update(populationCount=2,invalidIdentityCount=1)
                self.save_receipt(probe,write)
                checked=verify_receipt(probe)
                self.assertEqual(checked['candidateCount'],1)
                self.assertEqual(checked['acceptedCount'],1)
                self.assertEqual(checked['invalidIdentityCount'],1)
                # The same missing identity is never tolerated in write evidence.
                write('.restore_readback_0.json',{'rows':[invalid]})
                with self.assertRaisesRegex(ValueError,'identity invalid'):verify_receipt(probe)

    def test_missing_listing_field_is_ineligible_not_an_audit_crash(self):
        for field in ('displayStatus','userAble'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as d:
                probe,write=self.fixture(d);self.modernize(probe,write)
                population=json.loads(Path(probe['populationReceipt']).read_text())
                invalid={**population['second'][0],'id':'2','cardNo':'6470000002'}
                invalid.pop(field)
                population['first'].append(invalid);population['second'].append(invalid)
                _,probe['populationSha256']=write('.population.json',population)
                probe.update(populationCount=2,ineligibleStatusCount=1)
                self.save_receipt(probe,write)
                self.assertEqual(verify_receipt(probe)['ineligibleStatusCount'],1)

    def test_invalid_population_does_not_loosen_counts_or_duplicate_guards(self):
        for kind in ('wrong_count','duplicate_id','duplicate_phone','wrong_zone'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d:
                probe,write=self.fixture(d);self.modernize(probe,write)
                population=json.loads(Path(probe['populationReceipt']).read_text())
                extra={**population['second'][0],'id':'2','cardNo':'6470000002'}
                if kind=='wrong_count':extra.pop('cardNo')
                elif kind=='duplicate_id':extra['id']='1'
                elif kind=='duplicate_phone':extra['cardNo']=population['second'][0]['cardNo']
                else:extra['carrireIdentif']='30'
                population['first'].append(extra);population['second'].append(extra)
                _,probe['populationSha256']=write('.population.json',population)
                probe['populationCount']=2
                self.save_receipt(probe,write)
                with self.assertRaises(ValueError):verify_receipt(probe)

    def test_identity_key_matches_safe_javascript_numeric_id(self):
        row={'id':'1','cardNo':'6470000001','cardStore':'12345678901234567891F','slotIdentif':'101_1'}
        for value in (1,1.0):self.assertEqual(key_for({**row,'id':value}),key_for(row))
        for value in (None,True,1.5,float('inf'),float('nan'),9007199254740992,'１'):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'identity invalid'):
                key_for({**row,'id':value})
        for field in ('id','cardNo','cardStore','slotIdentif'):
            missing=dict(row);missing.pop(field)
            with self.subTest(missing=field),self.assertRaisesRegex(ValueError,'identity invalid'):key_for(missing)

    def test_readback_and_all_scope_independently_rebuilt(self):
        with tempfile.TemporaryDirectory() as d:
            probe,_=self.fixture(d);self.assertEqual(verify_receipt(probe)['onlineVerifiedCount'],1)

    def test_acceptance_alone_cannot_prove_online(self):
        with tempfile.TemporaryDirectory() as d:
            probe,write=self.fixture(d)
            population=json.loads(Path(probe['populationReceipt']).read_text())
            write('.restore_readback_0.json',{'rows':population['second']})
            with self.assertRaisesRegex(ValueError,'acceptance counted'):verify_receipt(probe)

    def test_offline_prior_attempt_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            probe,write=self.fixture(d);key=probe['results'][0]['key']
            write('.prior_ledger_0.json',[{'key':key,'previous':{'key':key,'attempts':1,'status':'online_verified'}}])
            with self.assertRaisesRegex(ValueError,'repeated'):verify_receipt(probe)

    def test_pending_operation_never_authorizes_replay(self):
        with tempfile.TemporaryDirectory() as d:
            probe,write=self.fixture(d);key=probe['results'][0]['key']
            write('.prior_ledger_0.json',[{'key':key,'previous':{'key':key,'attempts':1,'status':'pending_restore'}}])
            with self.assertRaisesRegex(ValueError,'one-shot action'):verify_receipt(probe)

    def test_async_recovery_resolved_between_population_and_exact_readback(self):
        with tempfile.TemporaryDirectory() as d:
            probe,write=self.fixture(d);key=probe['results'][0]['key']
            population=json.loads(Path(probe['populationReceipt']).read_text())
            write('.initial_readback_0.json',{'rows':[{**population['second'][0],'displayStatus':'已上架','userAble':True}]})
            write('.prior_ledger_0.json',[{'key':key,'previous':{'key':key,'attempts':1,'status':'awaiting_async_readback'}}])
            for suffix in ('.restore_intent_0.json','.restore_acceptance_0.json','.restore_readback_0.json'):
                Path(probe['receipt']+suffix).unlink()
            probe.update(recoverySubmittedCount=0,acceptedCount=0)
            probe['results'][0].update(initialState='online',action='read_only_pending',status='online_verified_pending_resolved')
            _,sha=write('',{k:v for k,v in probe.items() if k not in ('receipt','receiptSha256')})
            probe['receiptSha256']=sha
            self.assertEqual(verify_receipt(probe)['onlineVerifiedCount'],1)

    def test_summary_not_legacy_permanent_disable_and_unknown_not_zero(self):
        text=render_summary({'status':'success','expiredCardProbe':{'status':'failed_no_replay','error':'bad'}})
        self.assertIn('候选 未知',text);self.assertIn('不重复测试',text)
        self.assertNotIn('永久关闭',text)
        self.assertEqual(sum(line.startswith('## ') for line in text.splitlines()),7)

    def test_no_date_filter_independently_includes_missing_invalid_today_and_future(self):
        for cutoff in (None, 'bad', '2026-10-02', '2099-10-03'):
            with self.subTest(cutoff=cutoff), tempfile.TemporaryDirectory() as d:
                probe,write=self.fixture(d)
                population=json.loads(Path(probe['populationReceipt']).read_text())
                row=population['second'][0]
                row['chatrPlanExpire']=cutoff
                population['first']=[row]
                _,sha=write('.population.json',population)
                probe.update(scope='expired_zones_all_listing_states_v2',deadlineFilterEnabled=False,populationSha256=sha)
                write('.initial_readback_0.json',{'rows':[row]})
                # Date can change after selection without changing card identity.
                write('.restore_readback_0.json',{'rows':[{**row,'chatrPlanExpire':'2100-01-01','displayStatus':'已上架','userAble':True}]})
                inputs=json.loads(Path(probe['inputPath']).read_text())
                inputs.update(scope=probe['scope'],deadlineFilterEnabled=False)
                _,sha=write('.input.json',inputs);probe['inputSha256']=sha
                _,sha=write('',{k:v for k,v in probe.items() if k not in ('receipt','receiptSha256')})
                probe['receiptSha256']=sha
                self.assertEqual(verify_receipt(probe)['candidateCount'],1)

if __name__=='__main__':unittest.main()
