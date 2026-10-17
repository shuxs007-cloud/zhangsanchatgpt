"""Display-only carrier first-reply-rate observations and previous-day matching.

No network, workbook, F database or business writes. Only small immutable JSON
observations are persisted; never infer intraday data from a full-day export.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Hong_Kong')
METRIC = 'firstReplyRate'
WINDOW_SECONDS = 1800
VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def instant(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(ZONE) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def rate(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
        return None
    return value


def raw_rate(value):
    if type(value) in (int, float):
        return rate(value)
    if isinstance(value, str) and re.fullmatch(r'\s*\d+(?:\.\d+)?%?\s*', value):
        return rate(float(value.strip().rstrip('%')))
    return None


def snapshot(balance, run_id, server_date):
    if not re.fullmatch(r'\d{8}T\d{6}\+0800', run_id):
        raise ValueError('回复率快照运行编号无效')
    date.fromisoformat(server_date)
    if balance.get('serverDate') != server_date:
        raise ValueError('回复率快照服务器日期未绑定')
    rows = []
    source = balance.get('carrierReplyRates') or []
    identities = [row.get('carrierIdentif') for row in source]
    for row in source:
        identif = row.get('carrierIdentif')
        observed = instant(row.get('observedAt'))
        value = rate(row.get('replyRatePct'))
        raw = (row.get('dailyRow') or {}).get('firstReplyRate')
        reason = None
        if not isinstance(identif, str) or not identif or identities.count(identif) != 1:
            reason = '运营商标识缺失或重复'
        elif row.get('metric') != METRIC or row.get('status') != 'ok':
            reason = '首次回复率查询未通过'
        elif row.get('statDate') != server_date:
            reason = '统计日期与服务器日期不一致'
        elif observed is None:
            reason = '缺少本次实际采集时间'
        elif value is None or raw_rate(raw) != value:
            reason = '首次回复率缺失、非法或与原始值不一致'
        rows.append({
            'carrierIdentif': identif, 'carrierName': str(row.get('carrierName') or '未知运营商'),
            'metric': METRIC, 'statDate': row.get('statDate'),
            'observedAt': observed.isoformat() if observed else None,
            'replyRatePct': value if reason is None else None,
            'status': 'ok' if reason is None else 'unknown', 'reason': reason,
        })
    return {'version': VERSION, 'runId': run_id, 'serverDate': server_date,
            'scope': 'all_current_carrier_configs', 'metric': METRIC,
            'timeZone': str(ZONE), 'rows': rows}


def compare(current, history, window_seconds=WINDOW_SECONDS):
    if type(window_seconds) is not int or window_seconds not in (300, 1800):
        raise ValueError('无效的同期匹配窗口')
    previous_date = (date.fromisoformat(current['serverDate']) - timedelta(days=1)).isoformat()
    prior_by_id = {}
    for record in history:
        if (record.get('version') != VERSION or record.get('metric') != METRIC
                or record.get('timeZone') != str(ZONE) or record.get('serverDate') != previous_date):
            continue
        rows = record.get('rows') or []
        ids = [row.get('carrierIdentif') for row in rows]
        for row in rows:
            identif = row.get('carrierIdentif')
            if (ids.count(identif) == 1 and row.get('status') == 'ok' and row.get('metric') == METRIC
                    and row.get('statDate') == previous_date and rate(row.get('replyRatePct')) is not None
                    and instant(row.get('observedAt'))):
                prior_by_id.setdefault(identif, []).append({**row, 'sourceRunId': record['runId'],
                                                         'snapshotSha256': digest(record)})
    result = []
    for row in current['rows']:
        observed = instant(row.get('observedAt'))
        target = observed - timedelta(days=1) if observed else None
        eligible = []
        if row['status'] == 'ok' and target:
            for previous in prior_by_id.get(row['carrierIdentif'], []):
                when = instant(previous['observedAt'])
                deviation = (when - target).total_seconds()
                if when.date() == target.date() and abs(deviation) <= window_seconds:
                    eligible.append((abs(deviation), when, previous['sourceRunId'], deviation, previous))
        eligible.sort(key=lambda item: item[:3])
        chosen = eligible[0][-1] if eligible else None
        deviation = eligible[0][-2] if eligible else None
        # Equal timestamps with conflicting values cannot be silently selected.
        conflict = chosen and any(item[1] == eligible[0][1] and item[-1]['replyRatePct'] != chosen['replyRatePct']
                                  for item in eligible)
        if conflict:
            chosen, deviation = None, None
        result.append({**row,
            'currentReplyRatePct': row['replyRatePct'],
            'previousReplyRatePct': chosen['replyRatePct'] if chosen else None,
            'previousObservedAt': chosen['observedAt'] if chosen else None,
            'previousCarrierName': chosen['carrierName'] if chosen else None,
            'previousRunId': chosen['sourceRunId'] if chosen else None,
            'previousSnapshotSha256': chosen['snapshotSha256'] if chosen else None,
            'targetPreviousTime': target.isoformat() if target else None,
            'timeDifferenceSeconds': deviation,
            'deltaPercentagePoints': row['replyRatePct'] - chosen['replyRatePct'] if chosen else None,
            'comparisonStatus': ('exact' if deviation == 0 else 'nearby_snapshot') if chosen else 'unavailable',
            'comparisonReason': ('同刻快照冲突' if conflict else row['reason'] or '无昨日同期快照') if not chosen else None,
        })
    return {'version': VERSION, 'runId': current['runId'], 'serverDate': current['serverDate'],
            'metric': METRIC, 'scope': current['scope'], 'timeZone': str(ZONE),
            'matchingWindowSeconds': window_seconds, 'rows': result,
            'carrierCount': len(result), 'comparableCount': sum(row['previousReplyRatePct'] is not None for row in result),
            'currentUnknownCount': sum(row['currentReplyRatePct'] is None for row in result),
            'historyWarnings': [], 'independentReadbackPassed': False}


def persist_and_compare(balance, run_id, server_date, history_root):
    current = snapshot(balance, run_id, server_date)
    root = Path(history_root)
    folder = root / server_date
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'{run_id}.json'
    envelope = {'payload': current, 'sha256': digest(current)}
    if target.exists():
        if json.loads(target.read_text(encoding='utf-8')) != envelope:
            raise ValueError('同轮运营商回复率快照冲突，禁止覆盖')
    else:
        with target.open('x', encoding='utf-8') as handle:
            json.dump(envelope, handle, ensure_ascii=False, allow_nan=False)
    reread = json.loads(target.read_text(encoding='utf-8'))
    if reread != envelope or digest(reread['payload']) != reread['sha256']:
        raise ValueError('运营商回复率快照独立回读失败')
    prior_folder = root / (date.fromisoformat(server_date) - timedelta(days=1)).isoformat()
    history, warnings, sources = [], [], []
    for file in sorted(prior_folder.glob('*.json')):
        try:
            item = json.loads(file.read_text(encoding='utf-8'))
            payload = item['payload']
            if (digest(payload) != item['sha256'] or file.stem != payload['runId']
                    or file.parent.name != payload['serverDate'] or not isinstance(payload.get('rows'), list)
                    or any(not isinstance(row, dict) for row in payload['rows'])):
                raise ValueError('快照身份或SHA256不符')
            history.append(payload)
            sources.append({'path': str(file), 'sha256': item['sha256']})
        except (ValueError, TypeError, KeyError, OSError) as exc:
            warnings.append(f'{file.name}: {type(exc).__name__}')
    result = compare(current, history)
    result.update(historyWarnings=warnings, independentReadbackPassed=True,
                  snapshotPath=str(target), snapshotSha256=envelope['sha256'], historySources=sources)
    verify_comparison({**balance, 'carrierReplyComparison': result})
    return result


def verify_comparison(balance):
    """Final-audit readback: reopen exact frozen inputs and recalculate values."""
    evidence = balance.get('carrierReplyComparison') or {}
    if evidence.get('independentReadbackPassed') is not True:
        raise ValueError('运营商同期对比缺少独立核验')
    source = Path(evidence['snapshotPath'])
    stored = json.loads(source.read_text(encoding='utf-8'))
    current = snapshot(balance, evidence['runId'], balance['serverDate'])
    if (stored != {'payload': current, 'sha256': digest(current)}
            or stored['sha256'] != evidence['snapshotSha256']
            or source.name != current['runId'] + '.json' or source.parent.name != current['serverDate']):
        raise ValueError('运营商当前快照与原始采集/运行身份不一致')
    history = []
    previous_day = (date.fromisoformat(current['serverDate']) - timedelta(days=1)).isoformat()
    for item in evidence['historySources']:
        file = Path(item['path'])
        if file.parent.resolve() != (source.parent.parent / previous_day).resolve():
            raise ValueError('历史快照目录不属于昨日')
        previous = json.loads(file.read_text(encoding='utf-8'))
        if (digest(previous['payload']) != previous['sha256'] or previous['sha256'] != item['sha256']
                or file.stem != previous['payload']['runId'] or previous['payload']['serverDate'] != previous_day):
            raise ValueError('历史快照SHA256或身份不符')
        history.append(previous['payload'])
    expected = compare(current, history, evidence['matchingWindowSeconds'])
    if any(evidence.get(key) != value for key, value in expected.items() if key not in ('historyWarnings', 'independentReadbackPassed')):
        raise ValueError('运营商回复率对比独立重算不一致')
    return {'carrierCount': expected['carrierCount'], 'comparableCount': expected['comparableCount'],
            'currentUnknownCount': expected['currentUnknownCount']}


def report_lines(comparison):
    if not comparison:
        return []  # Legacy receipts remain readable; never retrofit old data.
    if comparison.get('independentReadbackPassed') is not True:
        return ['', '### 各运营商昨日同期回复率', '', '- 对比未通过核验，数值未知。', '']
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
    def pct(value):
        return '未知' if rate(value) is None else f'{value:.3f}%'
    def when(value):
        parsed = instant(value)
        return parsed.strftime('%m-%d %H:%M:%S') if parsed else '未知'
    lines = ['', '### 各运营商昨日同期回复率', '',
             '- 口径：后台首次回复率，当日累计至各自采集时点；按精确运营商ID匹配，不平均号码回复率。',
             f"- 时间为北京时间；昨日采用±{comparison.get('matchingWindowSeconds', 300) // 60}分钟内最近有效快照并列出偏差，非同刻为近同期，不是昨日全天值。未保存/未匹配显示未知。", '',
             '| 运营商 | 当前回复率 | 昨日同期 | 变化（百分点） | 当前采集 | 昨日采集 | 偏差/说明 |',
             '| --- | ---: | ---: | ---: | --- | --- | --- |']
    for row in comparison.get('rows') or []:
        change = row.get('deltaPercentagePoints')
        delta = f'{change:+.3f}' if type(change) in (int, float) and math.isfinite(change) else '未知'
        offset = row.get('timeDifferenceSeconds')
        note = (f'{offset:+.0f}秒' if offset is not None else row.get('comparisonReason') or '未知')
        if row.get('previousCarrierName') and row['previousCarrierName'] != row['carrierName']:
            note += '；昨日名：' + row['previousCarrierName']
        lines.append('| ' + ' | '.join(map(cell, [row['carrierName'], pct(row.get('currentReplyRatePct')),
            pct(row.get('previousReplyRatePct')), delta, when(row.get('observedAt')),
            when(row.get('previousObservedAt')), note])) + ' |')
    if comparison.get('historyWarnings'):
        lines.append(f"\n- {len(comparison['historyWarnings'])}份历史快照无效，已排除。")
    return lines + ['']
