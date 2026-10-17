#!/usr/bin/env python3
"""Find only a supplied ICCID list in dated Excel files. No database/JSON-log inputs."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import time

import opensms_task_f_slot_history as excel_reader
from opensms_task_f_all_slot_history import export_excel


def read_list(path):
    raw = path.read_bytes()
    records, invalid = [], []
    for line_number, line in enumerate(raw.decode('utf-8-sig').splitlines(), 1):
        value = line.strip()
        if not value or value.upper() == 'ICCID':
            continue
        key = excel_reader.normalize_iccid(value)
        if key is None:
            invalid.append(line_number)
        else:
            records.append(dict(line=line_number, original=value, iccid=key))
    if invalid or not records:
        raise ValueError(f'输入清单存在非法ICCID或为空；非法行号：{invalid}')
    return records, hashlib.sha256(raw).hexdigest()


def summarize(keys, evidence):
    by_card, by_slot = defaultdict(list), defaultdict(list)
    for item in evidence:
        if item['iccid'] not in keys:
            raise ValueError('出现用户清单外ICCID')
        by_card[item['iccid']].append(item)
        by_slot[(item['iccid'], item['slot'])].append(item)
    summary, locations = [], []
    for key in keys:
        found = by_card[key]
        slots = sorted({r['slot'] for r in found})
        summary.append(dict(iccid=key, slots=slots, found=bool(slots),
                            files=len({r['file'] for r in found}), observations=len(found)))
    for (key, slot), found in sorted(by_slot.items()):
        example = max(found, key=lambda r: (r['date'], r['file'], r['sheet'], r['row']))
        locations.append(dict(iccid=key, slot=slot, files=len({r['file'] for r in found}),
                              observations=len(found), firstDate=min(r['date'] for r in found),
                              lastDate=max(r['date'] for r in found), example=example,
                              onlyAmbiguous=all(r['ambiguousColumns'] for r in found)))
    return summary, locations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iccid-list', type=Path, required=True)
    parser.add_argument('--start', type=date.fromisoformat, required=True)
    parser.add_argument('--end', type=date.fromisoformat, required=True)
    parser.add_argument('--root', action='append')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8 or args.start > args.end:
        parser.error('并发数为1至8，起止日期须有效')
    records, input_hash = read_list(args.iccid_list)
    keys = list(dict.fromkeys(r['iccid'] for r in records))
    key_set = set(keys)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    excel_reader.write_json(out/'input_list.json', dict(source=str(args.iccid_list.resolve()),
        sha256=input_hash, records=records, uniqueIccids=keys))
    files, discovery = excel_reader.discover(args.root or excel_reader.DEFAULT_ROOTS,
                                             str(args.start), str(args.end), out)
    discovery['excluded']['指定日期范围外'] = discovery['excluded'].pop('一个月范围外', 0)
    def progress(**values):
        values['updatedAt'] = datetime.now().astimezone().isoformat()
        temp = out/'progress.tmp'
        temp.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')
        temp.replace(out/'progress.json')
        print(json.dumps(values, ensure_ascii=False), flush=True)
    progress(event='discovered', requestedIccids=len(keys), excelFiles=len(files), workers=args.workers,
             databaseRead=False, jsonLogsRead=False)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        files = list(pool.map(excel_reader.fingerprint, files))
    groups = defaultdict(list)
    errors = []
    for item in files:
        if item.get('error'):
            item['scanStatus'] = 'failed'
            errors.append(dict(file=item['path'], error=item['error']))
        else:
            groups[item['sha256']].append(item)
    progress(event='hash_complete', uniqueContents=len(groups), duplicateFiles=sum(len(x)-1 for x in groups.values()))
    evidence, unresolved = [], []
    completed, last_progress = 0, time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        remaining = iter(groups.values())
        pending = {}
        def submit_next():
            aliases = next(remaining, None)
            if aliases is not None:
                pending[pool.submit(excel_reader.scan_workbook, aliases[0], key_set)] = aliases
        for _ in range(args.workers * 2):
            submit_next()
        while pending:
            ready, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in ready:
                aliases = pending.pop(future)
                result = future.result()
                for item in aliases:
                    item['scanStatus'] = 'failed' if result.get('error') else 'scanned'
                    if result.get('error'):
                        item['error'] = result['error']
                        errors.append(dict(file=item['path'], error=result['error']))
                        continue  # No partial evidence from a failed workbook.
                    for group, destination in [('evidence', evidence), ('unresolved', unresolved)]:
                        for row in result[group]:
                            destination.append(dict(row, file=item['path'], date=item['date'],
                                dateBasis=item['dateBasis'], sha256=item['sha256'],
                                sourceKind=excel_reader.source_kind(item['path'], row['sheet'])))
                completed += 1
                submit_next()
                if time.monotonic()-last_progress >= 15 or completed == len(groups):
                    progress(event='scan_progress', completed=completed, total=len(groups),
                             matchedIccids=len({r['iccid'] for r in evidence}))
                    last_progress = time.monotonic()
    evidence.sort(key=lambda r: (r['iccid'], r['slot'], r['date'], r['file'], r['sheet'], r['row']))
    summary, locations = summarize(keys, evidence)
    excel_reader.write_json(out/'results.json', summary)
    excel_reader.write_json(out/'evidence.json', evidence)
    excel_reader.write_json(out/'unresolved.json', unresolved)
    excel_reader.write_json(out/'files.json', files)
    if hashlib.sha256(args.iccid_list.read_bytes()).hexdigest() != input_hash:
        raise ValueError('查询期间用户清单发生变化')
    headers = ['ICCID', '是否找到卡槽', '不同卡槽数', '全部历史卡槽', '来源Excel文件数', '来源记录数']
    def summary_rows(rows):
        for r in rows:
            yield [r['iccid'], '找到' if r['found'] else '未找到', len(r['slots']),
                   '；'.join(r['slots']), r['files'], r['observations']]
    notes = [
        ['输入', str(args.iccid_list.resolve())], ['输入SHA256', input_hash],
        ['范围', f'{args.start} 至 {args.end}（含首尾），只读Excel，不读取任务F数据库或JSON日志'],
        ['匹配', '仅清单内ICCID；18至22位文本精确匹配，允许去末尾一个F/f；不改数字、不按尾号猜测'],
        ['卡槽', '每个ICCID保留全部不同卡槽，不合并r前缀；同一位置所有Excel来源在逐条Excel来源表'],
        ['时间', '文件名/归档目录日期优先，无明确日期才用修改时间；文件日期不是位置生效时间'],
        ['来源', 'Google、历史处理表、库衍生sheet均标明实际Excel来源；不是当前后台位置确认'],
        ['边界', '数字型/公式型ICCID或无法明确关联卡槽列不作可靠位置；无效文件和无卡槽命中单列'],
        ['副本', 'SHA256相同内容只解析一次，但保留每一来源文件、工作表、行号、单元格；排除本工具历史输出避免循环引用'],
        ['生产数据', '未访问后台，未读写生产数据库，未修改任何历史Excel'],
    ]
    tables = [
        ('ICCID卡槽汇总', headers, summary_rows(summary)),
        ('所有卡槽位置', ['ICCID','卡槽位置','来源文件数','记录数','最早文件日期','最晚文件日期',
                         '仅列关联待核对','示例Excel','工作表','ICCID单元格','卡槽单元格'],
         ([r['iccid'],r['slot'],r['files'],r['observations'],r['firstDate'],r['lastDate'],
           r['onlyAmbiguous'],r['example']['file'],r['example']['sheet'],
           r['example']['iccidCell'],r['example']['slotCell']] for r in locations)),
        ('逐条Excel来源', ['ICCID','卡槽位置','文件日期','日期依据','来源类型','Excel文件','SHA256',
                          '工作表','行号','ICCID单元格','卡槽单元格','列关联待核对'],
         ([r[k] for k in ('iccid','slot','date','dateBasis','sourceKind','file','sha256','sheet','row',
                          'iccidCell','slotCell','ambiguousColumns')] for r in evidence)),
        ('未找到', headers, summary_rows(r for r in summary if not r['found'])),
        ('命中但无可用卡槽', ['ICCID','原因','文件日期','Excel文件','工作表','行号','ICCID单元格'],
         ([r.get(k,'') for k in ('iccid','reason','date','file','sheet','row','iccidCell')] for r in unresolved)),
        ('扫描文件清单', ['Excel文件','文件日期','日期依据','SHA256','状态','错误'],
         ([r.get(k,'') for k in ('path','date','dateBasis','sha256','scanStatus','error')] for r in files)),
        ('说明', ['项目','说明'], notes)]
    progress(event='writing_and_verifying', matchedIccids=sum(r['found'] for r in summary),
             distinctIccidSlotPairs=len(locations), sourceRows=len(evidence))
    workbook = out/'指定ICCID_近三个月全部Excel卡槽位置.xlsx'
    verified = export_excel(workbook, tables)
    report = dict(status='completed_with_invalid_files' if errors else 'completed',
        sourceType='excel_only', databaseRead=False, jsonLogsRead=False, backendAccessed=False,
        inputFile=str(args.iccid_list.resolve()), inputSha256=input_hash, requestedRows=len(records),
        targetIccids=len(keys), matchedIccids=sum(r['found'] for r in summary),
        missingIccids=sum(not r['found'] for r in summary), distinctIccidSlotPairs=len(locations),
        uniqueSlotStrings=len({r['slot'] for r in locations}), evidenceRows=len(evidence),
        unresolvedRows=len(unresolved), selectedFiles=len(files), uniqueContents=len(groups),
        scanErrors=errors, discovery=discovery, start=str(args.start), end=str(args.end),
        minSelectedFileDate=min((r['date'] for r in files), default=None), workers=args.workers,
        workbook=str(workbook), outputVerification=verified, scriptSha256AtStart=script_hash,
        durationSeconds=round(time.monotonic()-started,2))
    excel_reader.write_json(out/'summary.json', report)
    (out/'查询说明.md').write_text('\n'.join([
        '# 指定ICCID：近三个月Excel卡槽查询', '',
        f'查询清单 {len(keys)} 个，找到 {report["matchedIccids"]} 个，未找到 {report["missingIccids"]} 个。',
        f'ICCID与卡槽不同组合 {len(locations)} 个，所有位置和逐条Excel来源均已列入工作簿。', '',
        f'范围：{args.start} 至 {args.end}；仅扫描Excel，未读取任务F数据库或JSON日志。',
        f'候选Excel文件 {len(files)} 份，内容去重后 {len(groups)} 份，无效文件 {len(errors)} 份。',
        '来源文件日期不代表卡槽实际生效时间；历史位置不等于当前位置。',
        '输入ICCID不纠错、不截短；Excel非文本或公式标识不作可靠匹配；明细保留全部来源，不选最后一个代替其它位置。',
        '输出已独立逐格回读、ICCID文本类型、内容哈希和ZIP CRC核验。未修改生产库或历史文件。', '',
        f'[结果Excel]({workbook})', f'[统计与错误清单]({out/"summary.json"})'])+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
