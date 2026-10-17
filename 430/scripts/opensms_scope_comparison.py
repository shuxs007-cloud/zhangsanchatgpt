"""Display-only overall/room snapshots. No network or business mutations."""
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opensms_carrier_reply_comparison import digest, instant
from opensms_device_reply_periods import build as build_device_periods, verify_rows as verify_device_periods
from services.opensms_document_api.pipeline.room_mapping import CONFIG_SHA256 as ROOM_CONFIG_SHA256, LABELS as ROOM_LABELS, room_for_device

METRICS = ('总发送量', '总回复量', '发送窗口数', '回复窗口数')
# A changed room configuration must not reuse snapshots grouped by old ranges.
CONTRACT = f'source-all-rows-room-map-{ROOM_CONFIG_SHA256}-window-reply-v1'


def count(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(',', ''))
        return int(number) if math.isfinite(number) and number >= 0 and number.is_integer() else None
    except (ValueError, TypeError):
        return None


def room(slot):
    prefix = str(slot or '').split('_', 1)[0].strip()
    if not prefix.isdigit():
        return '未识别'
    return ROOM_LABELS.get(room_for_device(prefix), '未识别')


def aggregate(headers, rows, server_date):
    names = ['整体', *ROOM_LABELS.values(), '未识别']
    buckets = {name: {m: 0 for m in METRICS} for name in names}
    columns = {}
    for m in METRICS:
        label = server_date[5:] + m
        columns[m] = headers.index(label) if headers.count(label) == 1 else None
    slot = headers.index('卡槽') if headers.count('卡槽') == 1 else None
    if slot is None:
        raise ValueError('机房统计缺少唯一卡槽列')
    for row in rows:
        for name in ('整体', room(row[slot] if slot < len(row) else None)):
            for m, i in columns.items():
                value = count(row[i]) if i is not None and i < len(row) else None
                buckets[name][m] = None if value is None or buckets[name][m] is None else buckets[name][m] + value
    for values in buckets.values():
        for m, i in columns.items():
            if i is None:
                values[m] = None
        send, reply = values['发送窗口数'], values['回复窗口数']
        values['回复率'] = reply / send * 100 if send and reply is not None else None
    return buckets


def compare(current, history):
    when = instant(current['observedAt'])
    target = when - timedelta(days=1)
    day = (date.fromisoformat(current['serverDate']) - timedelta(days=1)).isoformat()
    eligible = []
    for prior in history:
        time = instant(prior.get('observedAt'))
        if (prior.get('contract') != CONTRACT or prior.get('serverDate') != day or not time
                or time.date() != target.date()):
            continue
        offset = (time - target).total_seconds()
        if abs(offset) <= 1800:
            eligible.append((abs(offset), time, prior['runId'], offset, prior))
    eligible.sort(key=lambda x: x[:3])
    selected = eligible[0][-1] if eligible else None
    conflict = selected and any(x[1] == eligible[0][1] and x[-1]['values'] != selected['values'] for x in eligible)
    if conflict:
        selected = None
    result = []
    for name, values in current['values'].items():
        old = selected['values'].get(name, {}) if selected else {}
        send, prev = values['总发送量'], old.get('总发送量')
        rate, prev_rate = values['回复率'], old.get('回复率')
        result.append({'scope': name, 'current': values, 'previous': old,
                       'sendDelta': send - prev if send is not None and prev is not None else None,
                       'sendChangePct': (send - prev) / prev * 100 if prev and send is not None else None,
                       'rateDeltaPoints': rate - prev_rate if rate is not None and prev_rate is not None else None})
    return {'rows': result, 'observedAt': current['observedAt'],
            'previousObservedAt': selected['observedAt'] if selected else None,
            'previousRunId': selected['runId'] if selected else None,
            'offsetSeconds': eligible[0][-2] if selected else None,
            'reason': None if selected else '同刻快照冲突' if conflict else '无昨日±30分钟有效快照'}


def build(cache, source, export_receipt, run_id, server_date, history_root):
    from opensms_export_scan_cache import load_scan_cache, sha256
    data = load_scan_cache(cache, expected_source=source)
    receipt = json.loads(Path(export_receipt).read_text())
    if receipt.get('serverDate') != server_date or not instant(receipt.get('downloadedAt')):
        raise ValueError('同期统计缺少已核验服务器日期/下载时间')
    current = {'contract': CONTRACT, 'runId': run_id, 'serverDate': server_date,
               'observedAt': receipt['downloadedAt'], 'sourceSha256': sha256(Path(source)),
               'values': aggregate(data['headers'], data['rows'], server_date)}
    path = Path(history_root) / server_date / (run_id + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {'payload': current, 'sha256': digest(current)}
    if path.exists():
        if json.loads(path.read_text()) != envelope:
            raise ValueError('同期统计同轮快照冲突')
    else:
        with path.open('x') as stream:
            json.dump(envelope, stream, ensure_ascii=False, allow_nan=False)
    day = (date.fromisoformat(server_date) - timedelta(days=1)).isoformat()
    sources, history, warnings = [], [], []
    for file in sorted((Path(history_root) / day).glob('*.json')):
        try:
            item = json.loads(file.read_text())
            if item['sha256'] != digest(item['payload']) or item['payload']['runId'] != file.stem or item['payload']['serverDate'] != day:
                raise ValueError('历史快照校验失败')
            sources.append({'path': str(file), 'sha256': item['sha256']})
            history.append(item['payload'])
        except (ValueError, KeyError, TypeError, OSError):
            warnings.append(file.name)
    result = {'snapshotPath': str(path), 'snapshotSha256': digest(current),
              'inputs': {'cache': str(cache), 'cacheSha256': sha256(Path(cache)), 'source': str(source),
                         'exportReceipt': str(export_receipt), 'exportReceiptSha256': sha256(Path(export_receipt))},
              'historySources': sources, 'historyWarnings': warnings, 'comparison': compare(current, history)}
    # Reuse these already loaded immutable rows; do not add a workbook scan,
    # history read, website request or another concurrent local job.
    result['deviceReplyPeriods'] = build_device_periods(
        data['headers'], data['rows'], run_id, server_date,
        current['sourceSha256'], current['observedAt'])
    result['deviceReplyPeriodsRequired'] = True
    verify(result)
    return result


def verify(evidence):
    from opensms_export_scan_cache import load_scan_cache, sha256
    path = Path(evidence['snapshotPath'])
    item = json.loads(path.read_text())
    current = item['payload']
    if item['sha256'] != digest(current) or item['sha256'] != evidence['snapshotSha256'] or path.stem != current['runId'] or path.parent.name != current['serverDate']:
        raise ValueError('同期快照独立回读失败')
    inputs = evidence['inputs']
    if (sha256(Path(inputs['cache'])) != inputs['cacheSha256']
            or sha256(Path(inputs['exportReceipt'])) != inputs['exportReceiptSha256']
            or sha256(Path(inputs['source'])) != current['sourceSha256']):
        raise ValueError('同期统计源哈希不一致')
    data = load_scan_cache(inputs['cache'], expected_source=inputs['source'])
    receipt = json.loads(Path(inputs['exportReceipt']).read_text())
    if (receipt.get('downloadedAt') != current['observedAt'] or receipt.get('serverDate') != current['serverDate']
            or aggregate(data['headers'], data['rows'], current['serverDate']) != current['values']):
        raise ValueError('同期统计源独立重算失败')
    if evidence.get('deviceReplyPeriodsRequired') is True and not evidence.get('deviceReplyPeriods'):
        raise ValueError('本轮设备期间回复率证据缺失')
    if 'deviceReplyPeriods' in evidence:
        verify_device_periods(evidence['deviceReplyPeriods'], data['headers'], data['rows'],
                             current['runId'], current['serverDate'], current['sourceSha256'], current['observedAt'])
    history = []
    day = (date.fromisoformat(current['serverDate']) - timedelta(days=1)).isoformat()
    for source in evidence['historySources']:
        file = Path(source['path'])
        old = json.loads(file.read_text())
        if (file.parent.resolve() != (path.parent.parent / day).resolve() or old['sha256'] != digest(old['payload'])
                or old['sha256'] != source['sha256'] or file.stem != old['payload']['runId']):
            raise ValueError('昨日快照独立回读失败')
        history.append(old['payload'])
    if compare(current, history) != evidence['comparison']:
        raise ValueError('整体/机房同期对比独立重算失败')


def report_lines(evidence):
    verify(evidence)
    c = evidence['comparison']
    def fmt(v, digits=0):
        return '未知' if v is None else f'{v:,.{digits}f}'
    lines = ['', '### 整体及机房：昨日同期发送量与回复率', '',
             '- 总发送量为短信条数；回复率=回复窗口数合计÷发送窗口数合计，不平均号码回复率。包含本轮源表全部记录；未配置设备单列未识别。',
             '- 采集时间采用新鲜导出下载完成时间（非文件修改时间），不代表服务器精确统计截点；昨日匹配前一服务器业务日、前一自然日±30分钟最近快照。',
             f"- 当前采集（北京时间）：{instant(c['observedAt']).isoformat()}；昨日采集：{instant(c['previousObservedAt']).isoformat() if c['previousObservedAt'] else '未知'}；偏差：{fmt(c['offsetSeconds'])}秒。{c['reason'] or ''}", '',
             '| 范围 | 今日总发送量 | 昨日同期总发送量 | 发送增减量 | 发送变化% | 今日回复率 | 昨日同期回复率 | 变化百分点 |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in c['rows']:
        a, b = row['current'], row['previous']
        values = [row['scope'], fmt(a['总发送量']), fmt(b.get('总发送量')), fmt(row['sendDelta']), fmt(row['sendChangePct'], 2),
                  fmt(a['回复率'], 3), fmt(b.get('回复率'), 3), fmt(row['rateDeltaPoints'], 3)]
        lines.append('| ' + ' | '.join(values) + ' |')
    return lines
