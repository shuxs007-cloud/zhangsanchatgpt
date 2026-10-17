"""Independent, file-only scope, one-shot intent and readback verification."""
import hashlib
import json
from datetime import date
from pathlib import Path
import re


def identity_for(row):
    # Match the executor's identity contract before hashing. Invalid population
    # rows are counted/skipped; invalid submitted/readback rows must still fail.
    raw_id=row.get('id')
    if type(raw_id) in (int,float):
        if abs(raw_id)>9007199254740991 or (type(raw_id) is float and not raw_id.is_integer()):
            raise ValueError('identity invalid')
        raw_id=str(int(raw_id))
    phone=row.get('cardNo');card_store=row.get('cardStore');slot=row.get('slotIdentif')
    if (not isinstance(raw_id,str) or not re.fullmatch(r'[0-9]+',raw_id)
            or not isinstance(phone,str) or not re.fullmatch(r'[0-9]{10}',phone)
            or not isinstance(card_store,str) or not isinstance(slot,str) or not slot.strip()):
        raise ValueError('identity invalid')
    identity={'id':raw_id,'phone':phone,
              'iccid':re.sub('[Ff]$','',card_store.strip()),'slot':slot}
    if not re.fullmatch(r'[0-9]{18,22}',identity['iccid']):
        raise ValueError('identity invalid')
    return identity


def key_for(row):
    identity=identity_for(row)
    return hashlib.sha256(json.dumps(identity,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def valid_date(value):
    try:
        return isinstance(value,str) and date.fromisoformat(value).isoformat()==value
    except ValueError:
        return False


def verify_receipt(probe):
    path=Path(probe['receipt'])
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=probe['receiptSha256']:
        raise ValueError('receipt hash changed')
    receipt=json.loads(raw)
    modern=receipt.get('scope')=='expired_zones_all_listing_states_v2'
    for field in ('runId','scope','status','serverDate','candidateCount','onlineVerifiedCount',
                  'acceptedCount','pendingCount','alreadyTestedSkippedCount','blockedCount'):
        if receipt.get(field)!=probe.get(field):
            raise ValueError('summary mismatch: '+field)
    if (receipt['scope'] not in ('expired_zones_deadline_before_platform_today_v1','expired_zones_all_listing_states_v2')
            or receipt.get('retryCount')!=0 or receipt.get('carrierWriteCount')!=0
            or receipt.get('dryRun') is not False or receipt.get('error')):
        raise ValueError('scope/write contract failed')
    if modern and receipt.get('deadlineFilterEnabled') is not False:
        raise ValueError('deadline filter must be disabled')
    population_raw=Path(receipt['populationReceipt']).read_bytes()
    if hashlib.sha256(population_raw).hexdigest()!=receipt['populationSha256']:
        raise ValueError('population hash changed')
    population=json.loads(population_raw)
    if population['first']!=population['second']:
        fields=('id','cardNo','cardStore','slotIdentif','carrireIdentif','displayStatus','userAble')
        if not modern:fields+=('chatrPlanExpire',)
        normalize=lambda rows:sorted([tuple(str(row[field]) if field in ('id','carrireIdentif') else row.get(field)
                                           for field in fields) for row in rows],key=lambda row:row[0])
        if normalize(population['first'])!=normalize(population['second']):
            raise ValueError('population changed')
    zones={z['name']:z['identif'] for z in receipt['zones']}
    if set(zones)!={'过期下架卡T','过期下架卡C'} or len(set(zones.values()))!=2:
        raise ValueError('zone scope invalid')
    today=receipt['serverDate']
    if not valid_date(today) or population['today']!=today or population['zones']!=receipt['zones']:
        raise ValueError('date scope invalid')
    input_raw=Path(receipt['inputPath']).read_bytes()
    inputs=json.loads(input_raw)
    if (hashlib.sha256(input_raw).hexdigest()!=receipt['inputSha256'] or inputs['runId']!=receipt['runId']
            or inputs['serverDate']!=today or inputs['serverDateVerified'] is not True
            or inputs['dateReceipts']!=receipt['dateReceipts'] or len(receipt['dateReceipts'])!=2):
        raise ValueError('runner date proof invalid')
    for proof in receipt['dateReceipts']:
        raw=Path(proof['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=proof['sha256'] or json.loads(raw)['serverDate']!=today:
            raise ValueError('platform date receipt changed')
    if modern and (inputs.get('scope')!=receipt['scope'] or inputs.get('deadlineFilterEnabled') is not False):
        raise ValueError('runner no-date scope invalid')
    rows=population['second'];eligible={};unknown=abnormal=future=invalid=0
    ids=set();phones=set()
    for row in rows:
        if str(row['id']) in ids or str(row['carrireIdentif']) not in zones.values():
            raise ValueError('duplicate identity or wrong zone')
        ids.add(str(row['id']))
        cutoff=row.get('chatrPlanExpire')
        if not modern:
            if not valid_date(cutoff): unknown+=1;continue
            if cutoff>=today: future+=1;continue
        if not ((row.get('displayStatus')=='已上架' and row.get('userAble') is True)
                or (row.get('displayStatus')=='未上架' and row.get('userAble') is False)):
            abnormal+=1;continue
        try:key=key_for(row)
        except (ValueError,TypeError):invalid+=1;continue
        if row['cardNo'] in phones:raise ValueError('duplicate candidate phone')
        phones.add(row['cardNo'])
        eligible[key]=row
    if (len(eligible)!=receipt['candidateCount'] or len(rows)!=receipt['populationCount']
            or (not modern and (unknown!=receipt['unknownDateCount'] or future!=receipt['notExpiredCount']))
            or abnormal!=receipt['ineligibleStatusCount'] or invalid!=receipt['invalidIdentityCount']):
        raise ValueError('independent population counts differ')
    results=receipt['results']
    if len(results)!=len(eligible) or {r['key'] for r in results}!=set(eligible):
        raise ValueError('result coverage failed')
    def assert_item(item):
        source=eligible[item['key']]
        expected={**identity_for(source),
                  'carrierIdentif':str(source['carrireIdentif'])}
        if not modern:expected['cutoff']=source['chatrPlanExpire']
        if any(item.get(k)!=v for k,v in expected.items()):raise ValueError('intent identity changed')
    for result in results:assert_item(result)
    priors={};initial_states={}
    restored=set();offlined=set();online=set()
    for start in range(0,len(eligible),500):
        batch_keys=set(list(eligible)[start:start+500])
        initial=json.loads(Path(str(path)+f'.initial_readback_{start}.json').read_text())['rows']
        if len(initial)!=len(batch_keys) or {key_for(row) for row in initial}!=batch_keys:
            raise ValueError('initial readback coverage failed')
        for row in initial:
            key=key_for(row);source=eligible[key]
            if str(row['carrireIdentif'])!=str(source['carrireIdentif']) or (not modern and row.get('chatrPlanExpire')!=source['chatrPlanExpire']):
                raise ValueError('initial readback scope changed')
            state=('online' if row['displayStatus']=='已上架' and row['userAble'] is True else
                   'offline' if row['displayStatus']=='未上架' and row['userAble'] is False else None)
            if state is None:raise ValueError('initial listing state unconfirmed')
            initial_states[key]=state
        prior_rows=json.loads(Path(str(path)+f'.prior_ledger_{start}.json').read_text())
        if len(prior_rows)!=len(batch_keys) or {prior['key'] for prior in prior_rows}!=batch_keys:
            raise ValueError('prior ledger coverage failed')
        for prior in prior_rows:
            priors[prior['key']]=prior['previous']
        for action, endpoint_key, accepted_field, collected in (
                ('offline','ids',None,offlined),('restore','phoneIds','acceptedCount',restored)):
            intent_path=Path(str(path)+f'.{action}_intent_{start}.json')
            if not intent_path.exists():continue
            intent=json.loads(intent_path.read_text());items=intent['items']
            if not items or len(items)>500 or any(item['key'] not in eligible for item in items):
                raise ValueError('intent scope failed')
            expected_endpoint='/phone/config/batchOffline' if action=='offline' else '/phone/config/offlineRecover'
            if intent.get('method')!='POST' or intent.get('endpoint')!=expected_endpoint or intent.get('body')!={endpoint_key:[item['id'] for item in items]}:
                raise ValueError('request endpoint/identity invalid')
            for item in items:assert_item(item)
            for item in items:
                if item['key'] in collected:raise ValueError('duplicate submission')
                collected.add(item['key'])
            accepted=json.loads(Path(str(path)+f'.{action}_acceptance_{start}.json').read_text())
            if accepted.get('code')!=200:raise ValueError('acceptance failed')
            if accepted_field and (accepted.get('data',{}).get('acceptedCount')!=len(items)
                                   or accepted['data'].get('skippedCount')!=0):
                raise ValueError('acceptance count failed')
            readback=json.loads(Path(str(path)+f'.{action}_readback_{start}.json').read_text())['rows']
            by_key={key_for(row):row for row in readback}
            if set(by_key)!={item['key'] for item in items}:raise ValueError('readback identities differ')
            for item in items:
                row=by_key[item['key']]
                if str(row['carrireIdentif'])!=item['carrierIdentif'] or (not modern and row.get('chatrPlanExpire')!=item['cutoff']):
                    raise ValueError('readback scope changed')
                if action=='offline' and not (row['displayStatus']=='未上架' and row['userAble'] is False):
                    raise ValueError('offline not verified before restore')
                if action=='restore' and row['displayStatus']=='已上架' and row['userAble'] is True:online.add(item['key'])
    for result in results:
        previous=priors.get(result['key'])
        if result['initialState']!=initial_states[result['key']]:
            raise ValueError('initial listing state differs')
        if previous is not None:
            if (previous.get('key')!=result['key'] or type(previous.get('attempts')) is not int
                    or previous['attempts']<0 or previous.get('status') not in
                    ('pending_offline','pending_restore','awaiting_async_readback','online_verified')
                    or previous['status']!='pending_offline' and previous['attempts']<1):
                raise ValueError('prior ledger invalid')
        if previous and previous['status']=='pending_offline':expected='blocked_uncertain_offline'
        elif previous and (previous['status']=='pending_restore' or previous['status']=='awaiting_async_readback'
                           and (not modern or result['initialState']!='online')):expected='read_only_pending'
        elif modern and previous and previous.get('runId')==receipt['runId']:expected='skip_same_run_already_tested'
        elif result['initialState']=='offline' and previous and previous['attempts']>0:expected='skip_offline_already_tested'
        else:expected='offline_then_restore' if result['initialState']=='online' else 'restore_once'
        if result['action']!=expected:
            raise ValueError('one-shot action repeated or not proved')
        allowed_statuses={
            'blocked_uncertain_offline':{'uncertain_no_replay'},'skip_offline_already_tested':{'skipped'},
            'skip_same_run_already_tested':{'skipped'},
            'read_only_pending':{'online_verified_pending_resolved'} if result['initialState']=='online' else {'awaiting_async_readback'},
            'offline_then_restore':{'online_verified','awaiting_async_readback'},
            'restore_once':{'online_verified','awaiting_async_readback'},
        }
        if result['status'] not in allowed_statuses[expected]:raise ValueError('action status not proved')
        if result['action']=='skip_offline_already_tested' and (not previous or previous['attempts']<1 or result['initialState']!='offline'):
            raise ValueError('one-shot skip not proved')
        if result['action']=='restore_once' and previous and previous['attempts']>0:
            raise ValueError('offline recovery repeated')
        if result['status']=='online_verified' and result['key'] not in online:
            raise ValueError('acceptance counted as recovery')
        if result['status']=='online_verified_pending_resolved':
            if not previous or previous['status'] not in ('pending_restore','awaiting_async_readback') or initial_states[result['key']]!='online':
                raise ValueError('pending resolution unproved')
            online.add(result['key'])
    if offlined!={r['key'] for r in results if r['action']=='offline_then_restore'} or restored!={r['key'] for r in results if r['action'] in ('offline_then_restore','restore_once')}:
        raise ValueError('intent coverage differs from approved actions')
    if (len(restored)!=receipt['recoverySubmittedCount'] or len(restored)!=receipt['acceptedCount']
            or len(offlined)!=receipt['offlineSubmittedCount'] or len(online)!=receipt['onlineVerifiedCount']
            or sum(r['status']=='awaiting_async_readback' for r in results)!=receipt['pendingCount']
            or sum(r['status']=='skipped' for r in results)!=receipt['alreadyTestedSkippedCount']
            or sum(r['status']=='uncertain_no_replay' for r in results)!=receipt['blockedCount']):
        raise ValueError('submission/readback counts differ')
    return receipt
