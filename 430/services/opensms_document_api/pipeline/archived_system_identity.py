"""Strict historical-record evidence when the backend has cleared its slot."""
from datetime import datetime
import re


def phone_key(value):
    digits = ''.join(c for c in str(value or '') if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def index_phones(rows):
    result = {}
    for row in rows:
        result.setdefault(phone_key(row.get('cardNo')), []).append(row)
    return result


def iccid_key(value):
    if value is None or value == '':
        return ''
    if not isinstance(value, str):
        return None
    normalized = re.sub(r'[Ff]$', '', value.strip())
    return normalized if re.fullmatch(r'[0-9]{18,22}', normalized) else None


def timestamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo is None else None
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', value):
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def match_archived(index, *, phone, slot, status, created, iccid):
    hits = index.get(phone_key(phone), [])
    if len(hits) != 1 or len(phone_key(phone)) != 10:
        return None
    raw = hits[0]
    # Export and inventory are separate snapshots. A formerly listed card can
    # have its slot cleared while the server is generating the export. Support
    # only the observed terminal state, never arbitrary status/phone fallbacks.
    old_archive = status == '下架' and raw.get('status') == '换卡已下线'
    swapped_archive = (status == '上架' and raw.get('status') == '换卡已下线'
                       and raw.get('displayStatus') == '卡槽异常'
                       and raw.get('showInTable') is False and raw.get('userAble') is False)
    empty_pool_archive = status in ('上架', '下架') and raw.get('status') == '猫池空号已下架'
    if not (old_archive or empty_pool_archive or swapped_archive):
        return None
    if raw.get('showAble') is not False or raw.get('slotIdentif') not in (None, ''):
        return None
    # The historical port remains from the export. Never borrow the new
    # occupant's slot, ICCID or dates, nor claim a live slot/phone match.
    machine = str(slot or '').split('_')[0]
    if not machine or machine != str(raw.get('machineName') or ''):
        return None
    stamp = timestamp(created)
    if stamp is None or stamp != timestamp(raw.get('createTime')):
        return None
    key = iccid_key(iccid)
    # The newly supported lifecycle requires a nonempty SIM identity; the
    # legacy already-off-shelf exception retains its original blank-pair rule.
    if empty_pool_archive and not key:
        return None
    if key is None or key != iccid_key(raw.get('cardStore')):
        return None
    return raw
