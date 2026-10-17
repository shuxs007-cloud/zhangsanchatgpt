"""Independent, file-only keep-200 audit. Never connects to a browser or database."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from opensms_swap_keep_worst_metrics import extract


def sha(file):
    with Path(file).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def slot(value):
    return re.sub(r'_(\d+)$', lambda m: '_' + str(int(m[1])), str(value or '').strip())


def iccid(value):
    return str(value or '').strip().upper().removesuffix('F')


def identity(raw):
    return {'phone': str(raw['cardNo']).strip()[-10:], 'id': str(raw['id']),
            'slot': slot(raw.get('slotIdentif', raw.get('slot'))),
            'iccid': iccid(raw.get('cardStore', raw.get('iccid'))),
            'opr': str(raw.get('opr') or '').strip(),
            'carrierIdentif': str(raw.get('carrireIdentif') or raw.get('carrierIdentif'))}


def audit(directory, completed=False):
    directory = Path(directory)
    preview_path = directory / 'preview.json'
    preview = json.loads(preview_path.read_text())
    selection = preview['selection']
    metrics = extract(selection['ranking']['sourceProofs'][0]['path'])
    assert metrics['sourceProofs'] == selection['ranking']['sourceProofs'], 'SOURCE_PROOFS_CHANGED'
    assert metrics['serverDate'] == preview['serverDate'], 'SOURCE_DATE_MISMATCH'
    zones = {z['identif']: z['name'] for z in preview['zone']}
    by_phone = {}
    for row in metrics['rows']:
        by_phone.setdefault(row['phone'], []).append(row)
    ranked, unknown = [], []
    for raw in preview['population']:
        live = identity(raw)
        name = zones[live['carrierIdentif']]
        matches = by_phone.get(live['phone'], [])
        assert len(matches) <= 1, 'DUPLICATE_SOURCE_PHONE'
        row = matches[0] if matches else None
        if row is None:
            reason = 'not_in_latest_report'
        elif (not isinstance(row['iccid'], str) or not live['iccid'] or iccid(row['iccid']) != live['iccid']
              or (live['slot'] and slot(row['slot']) != live['slot'])):
            reason = 'metric_card_identity_changed_or_missing'
        elif str(row['carrier'] or '').strip() != name:
            reason = 'metric_source_zone_changed'
        elif row['replyRate'] is None:
            reason = 'reply_rate_missing_or_invalid'
        elif row['sendWindows'] is None or row['sendWindows'] <= 0:
            reason = 'send_windows_missing_invalid_or_zero'
        else:
            ranked.append(dict(live, zone=name, replyRate=row['replyRate'], sendWindows=row['sendWindows'], sourceRow=row['sourceRow']))
            continue
        unknown.append(dict(live, zone=name, reason=reason, sourceRow=row['sourceRow'] if row else None))
    ranked.sort(key=lambda r: (r['replyRate'], -r['sendWindows'], r['phone']))
    unknown.sort(key=lambda r: r['phone'])
    kept, restore = ranked[:200], ranked[200:]
    assert selection['ranking']['kept'] == kept, 'KEEP_RANKING_MISMATCH'
    assert selection['ranking']['restore'] == restore, 'RESTORE_RANKING_MISMATCH'
    assert selection['ranking']['unknown'] == unknown, 'UNKNOWN_PARTITION_MISMATCH'
    assert selection['phones'] == [r['phone'] for r in restore], 'RESTORE_SELECTION_MISMATCH'
    assert {r['phone'] for r in restore}.isdisjoint(r['phone'] for r in kept + unknown), 'PROTECTED_PHONE_SELECTED'
    candidate_ids = {r['id'] for g in preview['plan']['groups'] for r in g['rows']}
    excluded_ids = {r['id'] for r in preview['plan']['excluded']}
    assert candidate_ids.isdisjoint(excluded_ids), 'PLAN_PARTITIONS_OVERLAP'
    assert candidate_ids | excluded_ids == {r['id'] for r in restore}, 'RESTORE_PLAN_INCOMPLETE'
    result = {'status': 'passed', 'mode': 'completed-audit' if completed else 'readonly-prewrite-audit',
              'runId': preview['runId'], 'scope': preview['scope'], 'sourceRunId': metrics['runId'],
              'sourceAsOf': metrics['asOf'], 'sourceWorkbook': metrics['workbook'],
              'populationCount': len(preview['population']), 'keepRequestedCount': 200, 'keptCount': len(kept),
              'ranking': {'metric': '4日回复率', 'tieBreak': metrics['tieBreak'],
                          'cutoffReplyRate': kept[-1]['replyRate'] if kept else None,
                          'cutoffSendWindows': kept[-1]['sendWindows'] if kept else None},
              'unknownMetricCount': len(unknown), 'restoreCandidateCount': len(restore),
              'backendValidCount': len(candidate_ids), 'restorationExcludedCount': len(excluded_ids),
              'independentRankingPassed': True, 'previewSha256': sha(preview_path),
              'kept': kept, 'metricExcluded': unknown, 'restorationExcluded': preview['plan']['excluded']}
    if completed:
        execution_path = directory / 'execution.json'
        execution = json.loads(execution_path.read_text())
        assert execution['status'] == 'passed' and execution['finalIndependentReadbackPassed'] is True, 'RESTORE_NOT_VERIFIED'
        assert execution.get('ownedHiddenPageClosed') is True, 'OWNED_PAGE_CLEANUP_UNCONFIRMED'
        assert execution['previewSha256'] == result['previewSha256'] and execution['selection'] == selection, 'EXECUTION_PREVIEW_MISMATCH'
        restored = [r for g in execution['groups'] for r in g['rows']]
        assert all(g['status'] == 'independently_verified' for g in execution['groups']), 'PENDING_SUBMISSION_EXISTS'
        assert len(restored) == len({r['id'] for r in restored}) == len(candidate_ids), 'ACTUAL_WRITE_ID_MISMATCH'
        assert {r['id'] for r in restored} == candidate_ids, 'OUTSIDE_PLAN_WRITE'
        assert execution['actualWriteCount'] == execution['independentVerifiedCount'] == len(restored), 'WRITE_COUNTS_MISMATCH'
        expected_remaining = {identity(r)['id'] for r in preview['population']} - candidate_ids
        remaining = [identity(r) for r in execution['remainingPopulation']]
        assert len(remaining) == len({r['id'] for r in remaining}) == execution['remainingCount'], 'REMAINING_COUNT_MISMATCH'
        assert {r['id'] for r in remaining} == expected_remaining, 'REMAINING_ID_MISMATCH'
        assert {r['id'] for r in kept + unknown} <= expected_remaining, 'KEPT_OR_UNKNOWN_WAS_MOVED'
        status_counts = {}
        for batch in execution['groups']:
            for raw in batch['before']['rows']:
                k = 'confirmed_online' if raw.get('displayStatus') == '已上架' and raw.get('userAble') is True else 'not_confirmed_online'
                status_counts[k] = status_counts.get(k, 0) + 1
        result.update(actualWriteCount=len(restored), independentlyVerifiedCount=len(restored),
                      remainingCount=len(remaining), finalIndependentReadbackPassed=True,
                      restoredStatusCounts=status_counts, executionSha256=sha(execution_path))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--completed', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit(args.directory, args.completed)
    if args.output:
        if not args.completed or args.output.parent.resolve() != args.directory.resolve():
            raise ValueError('AUDIT_OUTPUT_SCOPE_INVALID')
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        assert json.loads(args.output.read_text()) == result, 'AUDIT_FILE_READBACK_FAILED'
    print(json.dumps({k: v for k, v in result.items() if k not in ('kept', 'metricExcluded', 'restorationExcluded')}, ensure_ascii=False))
