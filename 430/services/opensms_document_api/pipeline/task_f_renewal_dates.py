"""Renewal overlay; raw Grab activation/+29 dates remain immutable and auditable."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import re

RULE = 'current_source_cutoff_plus_30_if_balance_gte_30_else_source_cutoff'
VERIFIED_RULE = 'verified_source_cutoff_else_positive_balance_prior_effective_plus_29'

def validate_verified_renewal(proof, iccid, activation, cutoff):
    """Explicit human-verified renewal; not a fresh Grab activation or automatic import."""
    if (proof.get('version') != 2 or proof.get('authorization') != 'current-user-verified-renewal-source'
        or proof.get('rule') != VERIFIED_RULE or proof.get('queryIccid') != iccid
        or proof.get('activationDate') != activation or proof.get('cutoffDate') != cutoff
        or proof.get('identitySourceKind') != 'verified_system_export'
        or not re.fullmatch(r'[a-f0-9]{64}', proof.get('sourceSha256', ''))
        or not re.fullmatch(r'[a-f0-9]{64}', proof.get('identitySourceSha256', ''))
        or not re.fullmatch(r'\d{10}', proof.get('phone', ''))
        or not re.fullmatch(r'\d{8}T\d{6}\+0800', proof.get('runId', ''))
        or not re.fullmatch(r'\d{8}T\d{6}\+0800', proof.get('identityRunId', ''))
        or type(proof.get('sourceRow')) is not int or proof['sourceRow'] < 2
        or type(proof.get('identitySourceRow')) is not int or proof['identitySourceRow'] < 2
        or not proof.get('slot') or type(proof.get('planType')) is not int
        or proof['planType'] not in (29, 30) or type(proof.get('extensionDays')) is not int):
        raise ValueError('人工核验续费来源或身份凭证无效')
    try:
        amount = Decimal(proof['balanceCad'])
    except (KeyError, InvalidOperation, TypeError):
        raise ValueError('人工核验续费余额无效') from None
    if not amount.is_finite() or amount < 0:
        raise ValueError('人工核验续费余额无效')
    a, c = canonical_date(activation), canonical_date(cutoff)
    base = canonical_date(proof.get('baseCutoffDate'))
    if base != a + timedelta(days=29):
        raise ValueError('人工续费不能改写原激活周期')
    if proof.get('dateMode') == 'source_explicit':
        if (proof.get('extensionDays') != 0 or proof.get('provisional') is not False
            or c != canonical_date(proof.get('sourceCutoff'))):
            raise ValueError('人工续费截止日必须直接等于表内日期')
    elif proof.get('dateMode') == 'positive_balance_prior_plus_29':
        if (proof.get('sourceCutoff') is not None or amount <= 0
            or proof.get('extensionDays') != 29 or proof.get('provisional') is not True
            or c != canonical_date(proof.get('priorEffectiveCutoff')) + timedelta(days=29)):
            raise ValueError('余额缺日期临时推算规则不一致')
    else:
        raise ValueError('人工续费日期模式未知')
    return c

def canonical_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('续费日期不是yyyy-mm-dd')
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError('续费日期无效')
    return result

def validate_renewal(proof, iccid, activation, cutoff):
    if not isinstance(proof, dict):
        raise ValueError('续费日期缺少来源凭证')
    if proof.get('version') == 2:
        return validate_verified_renewal(proof, iccid, activation, cutoff)
    if (proof.get('version') != 1 or proof.get('authorization') != 'current-user-renewal-source'
        or proof.get('rule') != RULE or proof.get('queryIccid') != iccid
        or proof.get('activationDate') != activation or proof.get('cutoffDate') != cutoff
        or proof.get('status') not in {'充值成功', '不需要充值'}
        or not re.fullmatch(r'[a-f0-9]{64}', proof.get('sourceSha256', ''))
        or not re.fullmatch(r'\d{10}', proof.get('phone', ''))
        or not re.fullmatch(r'\d{8}T\d{6}\+0800', proof.get('runId', ''))
        or type(proof.get('sourceRow')) is not int or proof['sourceRow'] < 2
        or not str(proof.get('backendId', '')).isdigit() or not proof.get('slot')):
        raise ValueError('续费日期来源或身份凭证无效')
    try:
        amount = Decimal(proof['balanceCad'])
    except (KeyError, InvalidOperation, TypeError):
        raise ValueError('续费余额无效') from None
    if not amount.is_finite() or amount < 0:
        raise ValueError('续费余额无效')
    a, c = canonical_date(activation), canonical_date(cutoff)
    base, source = canonical_date(proof.get('baseCutoffDate')), canonical_date(proof.get('sourceCutoff'))
    extension = 30 if amount >= 30 else 0
    if (base != a + timedelta(days=29) or type(proof.get('extensionDays')) is not int
        or proof['extensionDays'] != extension or c != source + timedelta(days=extension)):
        raise ValueError('续费截止日与来源独立计算不一致')
    return c

def read_renewals(connection):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kooda_renewal_current'").fetchone():
        return {}
    result = {}
    for key, encoded in connection.execute('SELECT query_iccid,proof_json FROM kooda_renewal_current ORDER BY query_iccid'):
        proof = json.loads(encoded)
        validate_renewal(proof, key, proof.get('activationDate'), proof.get('cutoffDate'))
        if key in result:
            raise ValueError('续费库ICCID重复')
        result[key] = proof
    return result
