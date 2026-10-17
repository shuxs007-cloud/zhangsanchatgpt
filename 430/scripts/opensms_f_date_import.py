"""Prepare only the current A's independently verified file; never access the website."""
import json
import re
from pathlib import Path
from opensms_system_missing_f_dates import digest, read_source, select_rows, template_rows
from opensms_export_task_f_deadlines import verify_output


def prepare_import(receipt, run_id):
    if (receipt.get('runId') != run_id or receipt.get('status') != 'passed'
            or receipt.get('independentReadbackPassed') is not True
            or receipt.get('formatVersion') != 'task_f_deadline_template_v2'
            or type(receipt.get('exportedRowCount')) is not int
            or receipt['exportedRowCount'] < 1):
        raise ValueError('F_IMPORT_CURRENT_FILE_UNCONFIRMED')
    for name, hash_name in (('source','sourceSha256'), ('output','sha256'),
                            ('databaseSnapshot','databaseSnapshotSha256'),
                            ('sourceDetail','sourceDetailSha256'), ('template','templateSha256')):
        if digest(receipt[name]) != receipt[hash_name]:
            raise ValueError('F_IMPORT_FILE_CHANGED')
    headers, raw = read_source(receipt['source'])
    for name in ('电话', '卡槽', 'ICCID', '激活日期'):
        if headers.count(name) != 1:
            raise ValueError('F_IMPORT_SOURCE_IDENTITY_MISSING')
    snapshot = json.loads(Path(receipt['databaseSnapshot']).read_text())
    selected = select_rows(headers, raw, snapshot)
    expected = template_rows(selected)
    if len(selected) != receipt['rowCount'] or len(expected) != receipt['exportedRowCount']:
        raise ValueError('F_IMPORT_COUNT_CHANGED')
    verify_output(Path(receipt['output']), expected)
    records = {}
    for values, key, item in selected:
        phone = str(values[headers.index('电话')] or '').strip()
        if len(phone) == 11 and phone.startswith('1'):
            phone = phone[1:]
        slot = str(values[headers.index('卡槽')] or '').strip()
        record = dict(iccid=key, phone=phone, slot=slot,
                      activation=item['activation_date'], cutoff=item['cutoff_date'])
        if key in records and records[key] != record:
            raise ValueError('F_IMPORT_SOURCE_IDENTITY_AMBIGUOUS')
        records[key] = record
    return dict(version=1, runId=run_id, scope='current_task_a_system_missing_f_dates',
                independentReadbackPassed=True, file=receipt['output'],
                fileSha256=receipt['sha256'], fileRows=len(expected),
                records=[records[key] for key in sorted(records)])


def import_summary(result):
    result = result or {}
    status = result.get('status')
    if status == 'passed' and result.get('independentReadbackPassed') is True:
        return f"已提交系统，{result.get('verifiedDatePairs')} 条日期独立回读通过。"
    if status == 'skipped_backend_already_matches':
        return '后台日期已一致，未重复提交。'
    if status == 'skipped_no_new_data':
        return '本轮没有新数据，未提交。'
    if status in ('submission_outcome_uncertain', 'accepted_pending_readback'):
        return '提交或落库结果尚未确认，未自动重试；需要排查。'
    if status == 'failed_before_submission':
        error = result.get('error')
        if error == 'F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED':
            run = result.get('blockingRunId')
            prior = f'前一轮（{run}）' if isinstance(run, str) and re.fullmatch(r'\d{8}T\d{6}\+0800', run) else '前一轮'
            evidence = result.get('blockingImport') or {}
            code = evidence.get('responseCode')
            count = evidence.get('fileRows')
            if (evidence.get('source') == 'local_historical_receipt'
                    and evidence.get('currentBackendVerified') is False
                    and type(code) is int and code != 200):
                rows = f'{count}条' if type(count) is int and count > 0 else ''
                return (f'{prior}{rows}导入的历史回执显示后台业务码{code}，日期落库未确认；'
                        '本轮未提交。该信息不是当前后台回读，需人工授权核对或重新提交，不能仅清除意图。')
            return f'{prior}导入结果仍未确认，本轮未提交、未自动重试；需先核实旧提交。'
        if error == 'F_IMPORT_EXISTING_DATE_CONFLICT':
            return '后台已有日期与本轮模板冲突，未提交、未覆盖；需要排查。'
        code = f'（{error}）' if isinstance(error, str) and re.fullmatch(r'F_IMPORT_[A-Z_]+', error) else ''
        return f'导入前核验失败{code}，未提交；需要排查。'
    return '未上传系统。'
