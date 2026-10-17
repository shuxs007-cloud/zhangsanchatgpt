#!/usr/bin/env python3
"""Restore supplied ICCIDs' complete historical Excel rows, without backend/DB access."""
import argparse
from collections import Counter, defaultdict, OrderedDict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, datetime
import gzip
import hashlib
import io
import json
from pathlib import Path
import posixpath
import re
import time
import zipfile

from lxml import etree
from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format, is_datetime
from openpyxl.utils.datetime import from_excel, CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900

import opensms_task_f_slot_history as base
from opensms_find_iccid_slots_in_excel import read_list
from opensms_task_f_all_slot_history import export_excel

CACHE = OrderedDict()
DATE_FIELDS = {
    '上卡日期': {'上卡日期', '上卡时间', '上卡時間'},
    '创建时间': {'创建时间', '创建日期', '創建時間', '創建日期', 'createtime', 'createdate', 'createdat', 'created_at', 'create_time'},
    '激活日期': {'激活日期', '激活时间', '激活時間', 'activationdate', 'activation_date'},
    '截止日': {'截止日', '截止日期', '到期日期', '預計過期日', '预计过期日', 'expiredate', 'expirydate'},
}
FIELDS = {
    **DATE_FIELDS,
    '手机号': {'手机号', '手機號', '手机号码', '電話', '电话', '卡号', '卡號', 'phone', 'phonenumber'},
    '运营商': {'运营商', '電信運營商', '电信运营商', '电信商', '電信商', '電信商/價格', '电信商/价格', 'carrier'},
    'OPR': {'opr'}, '机房': {'机房', '機房'},
    '状态': {'状态', '狀態', 'status'}, '备注': {'备注', '備註', '备注信息', 'remark', 'remarks'},
}
FIELDS = {k: {base.header(x) for x in v} for k, v in FIELDS.items()}
ID_HEADERS = base.ICCID_HEADERS | {'sim卡iccid', 'simiccid', 'sim卡存储', 'icc-id'}
SLOT_HEADERS = base.SLOT_HEADERS | {'卡槽位置', '原始卡槽', '系统卡槽', '系統卡槽', 'slots', 'source_slot'}
DATE_PATTERN = re.compile(r'^\s*(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})')


def pack(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def digest(value):
    return hashlib.sha256(pack(value).encode()).hexdigest()


def date_value(value):
    match = DATE_PATTERN.match(value or '')
    if not match:
        return False
    try:
        date(*map(int, match.groups()))
        return True
    except ValueError:
        return False


def styles(book):
    if 'xl/styles.xml' not in book.namelist():
        return ['General']
    root = etree.fromstring(book.read('xl/styles.xml'))
    formats = dict(BUILTIN_FORMATS)
    formats.update({int(x.get('numFmtId')): x.get('formatCode', '')
                    for x in root.findall('.//{' + base.NS + '}numFmt')})
    xfs = root.find('{' + base.NS + '}cellXfs')
    return [formats.get(int(x.get('numFmtId', 0)), 'General') for x in xfs] if xfs is not None else ['General']


def cells(fragment, strings, namespaces, formats, epoch):
    declarations = ' '.join(('xmlns' + (':' + k if k else '') + '="' + v + '"') for k, v in namespaces.items())
    root = etree.fromstring(('<wrapper ' + declarations + '>').encode() + fragment + b'</wrapper>',
                           etree.XMLParser(resolve_entities=False, no_network=True))
    row, result = root[0], {}
    for cell in row:
        if base.local_name(cell) != 'c':
            continue
        ref = cell.get('r', '')
        col = re.sub(r'\d', '', ref)
        kind = cell.get('t', 'n')
        formula_node = next((x for x in cell if base.local_name(x) == 'f'), None)
        raw = next((x.text or '' for x in cell if base.local_name(x) == 'v'), '')
        value = strings[int(raw)] if kind == 's' and raw else base.rich_text(cell) if kind == 'inlineStr' else raw
        style_index = int(cell.get('s', 0))
        fmt = formats[style_index] if style_index < len(formats) else 'General'
        if kind == 'n' and raw and is_date_format(fmt):
            try:
                value_date = from_excel(float(raw), epoch=epoch)
                if isinstance(value_date, datetime) and is_datetime(fmt) == 'date':
                    value = value_date.date().isoformat()
                else:
                    value = value_date.isoformat(sep=' ') if isinstance(value_date, datetime) else value_date.isoformat()
            except (ValueError, OverflowError, TypeError):
                pass
        result[col] = dict(column=col, value=value, raw=raw, type=kind, numberFormat=fmt,
                           formula=(formula_node.text or '') if formula_node is not None else '',
                           formulaPresent=formula_node is not None,
                           formulaAttributes=dict(formula_node.attrib) if formula_node is not None else {})
    return int(row.get('r', '0')), result


def scan_sheet(xml, strings, string_hash, targets, formats, epoch):
    key = (hashlib.sha256(xml).hexdigest(), string_hash, digest(formats), str(epoch), tuple(sorted(targets)))
    if key in CACHE:
        CACHE.move_to_end(key)
        return CACHE[key]
    ids = [str(i).encode() for i, x in enumerate(strings) if base.normalize_iccid(x) in targets]
    reference = re.compile(rb'<(?:[\w.-]+:)?v>\s*(?:' + b'|'.join(ids) + rb')\s*</(?:[\w.-]+:)?v>') if ids else None
    literals = re.compile(rb'[0-9]{18,}[Ff]?')
    wanted = {x.encode() for x in targets}
    def contains(data):
        return (any(x.group().removesuffix(b'F').removesuffix(b'f') in wanted for x in literals.finditer(data))
                or (reference is not None and reference.search(data))
                or re.search(rb'<(?:[\w.-]+:)?is\b[^>]*>\s*<(?:[\w.-]+:)?r\b', data))
    found, rejected = [], []
    if contains(xml):
        namespaces = next(etree.iterparse(io.BytesIO(xml), events=('start',), resolve_entities=False, no_network=True))[1].nsmap
        headers, header_row = {}, 0
        for fragment in base.list_first_rows(xml, 50):
            num, decoded = cells(fragment, strings, namespaces, formats, epoch)
            if any(base.header(v['value']) in ID_HEADERS for v in decoded.values()):
                headers = {col: v['value'] for col, v in decoded.items()}
                header_row = num
                break
        for match in base.ROW.finditer(xml):
            fragment = match.group()
            if not contains(fragment):
                continue
            rownum, decoded = cells(fragment, strings, namespaces, formats, epoch)
            for col, c in decoded.items():
                iccid = base.normalize_iccid(c['value'])
                if iccid not in targets:
                    continue
                location = dict(iccid=iccid, row=rownum, iccidCell=f'{col}{rownum}')
                if c['type'] not in ('s', 'str', 'inlineStr') or c['formulaPresent']:
                    rejected.append(dict(location, reason='ICCID非文本或为公式，不能确认精度', rawValue=c['value']))
                    continue
                identified = rownum > header_row > 0 and base.header(headers.get(col, '')) in ID_HEADERS
                other_ids = {base.normalize_iccid(v['value']) for k, v in decoded.items()
                             if base.header(headers.get(k, '')) in ID_HEADERS and base.normalize_iccid(v['value'])}
                trusted = identified and len(other_ids) == 1
                note = '明确ICCID列' if trusted else '同一行有不同ICCID，字段归属待核对' if identified else '未识别明确ICCID表头，仅保留原行待核对'
                fields = [dict(v, header=headers.get(k, '')) for k, v in decoded.items()
                          if v['value'] != '' or v['formulaPresent']]
                record = dict(iccid=iccid, identityColumn=col, trusted=trusted, identityNote=note, fields=fields)
                found.append(dict(location, record=record, headerRow=header_row))
    result = dict(rows=found, rejected=rejected)
    CACHE[key] = result
    while len(CACHE) > 64:
        CACHE.popitem(last=False)
    return result


def scan_book(item, targets):
    result = dict(rows=[], rejected=[], sheets=0)
    try:
        if not zipfile.is_zipfile(item['path']):
            raise ValueError('非OOXML工作簿：空文件、下载响应或旧二进制格式，不猜测内容')
        with zipfile.ZipFile(item['path']) as book:
            root = etree.fromstring(book.read('xl/workbook.xml'))
            props = root.find('{' + base.NS + '}workbookPr')
            epoch = CALENDAR_MAC_1904 if props is not None and props.get('date1904') in ('1', 'true') else CALENDAR_WINDOWS_1900
            rels = etree.fromstring(book.read('xl/_rels/workbook.xml.rels'))
            members = {r.get('Id'): posixpath.normpath('xl/' + r.get('Target', '')) if not r.get('Target', '').startswith('/') else r.get('Target').lstrip('/')
                       for r in rels if r.get('TargetMode') != 'External'}
            string_bytes = book.read('xl/sharedStrings.xml') if 'xl/sharedStrings.xml' in book.namelist() else b''
            strings = [base.rich_text(x) for x in etree.fromstring(string_bytes)] if string_bytes else []
            formats = styles(book)
            sheets = root.findall('.//{' + base.NS + '}sheet')
            if not sheets:
                raise ValueError('无可识别工作表')
            for sheet in sheets:
                member = members[sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')]
                if not member.endswith('.xml'):
                    raise ValueError('不支持的二进制工作表')
                data = scan_sheet(book.read(member), strings, hashlib.sha256(string_bytes).hexdigest(), targets, formats, epoch)
                for category in ('rows', 'rejected'):
                    result[category].extend(dict(x, sheet=sheet.get('name'), sheetMember=member) for x in data[category])
                result['sheets'] += 1
        stat = Path(item['path']).stat()
        if (stat.st_size, stat.st_mtime_ns) != (item['size'], item['mtimeNs']):
            raise ValueError('扫描期间源文件变化，整份结果弃用')
    except Exception as exc:
        result.update(rows=[], rejected=[], error=f'{type(exc).__name__}: {exc}')
    return result


def extracted(record):
    result = {k: [] for k in FIELDS}
    result.update(卡槽=[], 归一化卡槽=[])
    if not record['trusted']:
        return result
    for cell in record['fields']:
        if cell['formulaPresent'] or cell['type'] == 'e':
            continue
        name, value = base.header(cell['header']), cell['value'].strip()
        if name in SLOT_HEADERS and base.SLOT.fullmatch(value):
            result['归一化卡槽' if name == '归一化卡槽' else '卡槽'].append(value)
        for k, aliases in FIELDS.items():
            if name in aliases and value and (k not in DATE_FIELDS or date_value(value)):
                result[k].append(value)
    return {k: sorted(set(v)) for k, v in result.items()}


def write_gzip(path, rows):
    with gzip.open(path, 'xt', encoding='utf-8', compresslevel=1) as stream:
        for row in rows:
            stream.write(pack(row) + '\n')


def output_tables(keys, records, observations, files, rejected, notes):
    per_card = defaultdict(list)
    per_record = defaultdict(list)
    filemap = {x['fileId']: x for x in files}
    for x in observations:
        per_record[x['recordId']].append(x)
    for r in records.values():
        r['extracted'] = extracted(r)
        per_card[r['iccid']].append(r)
    summary, combos = [], {}
    for key in keys:
        found = per_card[key]
        union = {k: sorted({v for r in found for v in r['extracted'][k]}) for k in ['卡槽', '归一化卡槽', *FIELDS]}
        slots = sorted(set(union['卡槽'] + union['归一化卡槽']))
        source_ids = {o['fileId'] for r in found for o in per_record[r['recordId']]}
        days = sorted(filemap[x]['date'] for x in source_ids)
        summary.append(dict(iccid=key, found=bool(found), slots=slots, values=union,
                            records=len(found), sourceFiles=len(source_ids), firstFileDate=days[0] if days else '',
                            lastFileDate=days[-1] if days else '', pending=sum(not r['trusted'] for r in found)))
        for r in found:
            v = r['extracted']
            for slot in sorted(set(v['卡槽'] + v['归一化卡槽'])) or ['']:
                signature = (key, slot, *(tuple(v[k]) for k in FIELDS))
                combos.setdefault(signature, set()).add(r['recordId'])
    join = lambda values: '；'.join(values)
    summary_headers = ['ICCID', '查询状态', '所有历史卡槽', '上卡日期（全部）', '创建时间（全部）', '激活日期（全部）',
                       '截止日（全部）', '手机号（全部）', '运营商（全部）', '记录版本数', '来源Excel数', '最早文件日期', '最晚文件日期', '待核对记录数']
    def summary_rows(items):
        for s in items:
            yield [s['iccid'], '找到历史记录' if s['found'] else '未找到可靠文本记录', join(s['slots']),
                   *(join(s['values'][k]) for k in ['上卡日期','创建时间','激活日期','截止日','手机号','运营商']),
                   s['records'], s['sourceFiles'], s['firstFileDate'], s['lastFileDate'], s['pending']]
    def combo_rows():
        for key, record_ids in sorted(combos.items()):
            sources = [o for rid in record_ids for o in per_record[rid]]
            dates = [filemap[o['fileId']]['date'] for o in sources]
            yield [key[0], key[1], *(join(v) for v in key[2:]), digest(key), len(record_ids), min(dates), max(dates)]
    def combo_links():
        for key, record_ids in sorted(combos.items()):
            for rid in sorted(record_ids):
                yield [key[0], digest(key), rid]
    def record_rows():
        for r in records.values():
            obs = per_record[r['recordId']]
            example = max(obs, key=lambda o: (filemap[o['fileId']]['date'], o['fileId']))
            yield [r['iccid'], r['recordId'], r['identityNote'], *(join(r['extracted'][k]) for k in ['卡槽','归一化卡槽',*FIELDS]),
                   filemap[example['fileId']]['path'], example['sheet'], example['row'], example['iccidCell'], len(obs)]
    def field_rows():
        for r in records.values():
            for c in r['fields']:
                strings = [c['value'], c['raw'], c['formula'], pack(c['formulaAttributes'])]
                parts = max(1, max((len(v)+29999)//30000 for v in strings))
                for index in range(parts):
                    yield [r['iccid'], r['recordId'], c['column'], c['header'], index+1,
                           strings[0][index*30000:(index+1)*30000], strings[1][index*30000:(index+1)*30000],
                           c['type'], c['numberFormat'], c['formulaPresent'], strings[2][index*30000:(index+1)*30000],
                           strings[3][index*30000:(index+1)*30000]]
    def source_rows():
        for o in observations:
            f = filemap[o['fileId']]
            yield [o['iccid'], o['recordId'], o['fileId'], f['date'], f['dateBasis'], f['path'], o['sheet'],
                   o['row'], o['iccidCell'], o['headerRow'], base.source_kind(f['path'], o['sheet']), f['sha256']]
    field_headers = ['ICCID','记录编号','列','原字段名','长文本分段序号','字段值','XML原值','原单元格类型','原数字格式','是否公式','公式原文（不执行）','公式属性']
    tables = [
        ('查询总览', summary_headers, summary_rows(summary)),
        ('卡槽日期历史', ['ICCID','卡槽',*FIELDS,'组合编号','关联记录数','最早文件日期','最晚文件日期'], combo_rows()),
        ('卡槽记录关联', ['ICCID','组合编号','记录编号'], combo_links()),
        ('历史记录', ['ICCID','记录编号','关联状态','卡槽','归一化卡槽',*FIELDS,'示例来源Excel','工作表','行号','ICCID单元格','来源记录数'], record_rows()),
        ('全部字段', field_headers, field_rows()),
        ('逐条来源', ['ICCID','记录编号','文件编号','文件日期','日期依据','Excel文件','工作表','行号','ICCID单元格','表头行','来源类型','SHA256'], source_rows()),
        ('缺失与待核对', summary_headers, summary_rows(s for s in summary if not s['slots'] or not s['values']['上卡日期'] or not s['values']['创建时间'] or s['pending'])),
        ('不可靠标识命中', ['ICCID','原因','原始值','Excel文件','工作表','行号','ICCID单元格'],
         ([x['iccid'], x['reason'], x['rawValue'], filemap[x['fileId']]['path'], x['sheet'], x['row'], x['iccidCell']] for x in rejected)),
        ('扫描文件清单', ['文件编号','Excel文件','文件日期','日期依据','SHA256','状态','错误'],
         ([f.get(k,'') for k in ['fileId','path','date','dateBasis','sha256','scanStatus','error']] for f in files)),
        ('说明', ['项目','说明'], notes)]
    return tables, summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--iccid-list', type=Path, required=True)
    p.add_argument('--start', type=date.fromisoformat, required=True)
    p.add_argument('--end', type=date.fromisoformat, required=True)
    p.add_argument('--root', action='append')
    p.add_argument('--workers', type=int, default=8)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if not 1 <= args.workers <= 8 or args.start > args.end:
        p.error('并发数1至8，开始日期不能晚于结束日期')
    inputs, input_hash = read_list(args.iccid_list)
    keys = list(dict.fromkeys(x['iccid'] for x in inputs))
    started = time.monotonic()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    base.write_json(out/'input_list.json', dict(source=str(args.iccid_list), sha256=input_hash, records=inputs))
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    def progress(**values):
        values['updatedAt'] = datetime.now().astimezone().isoformat()
        temporary = out/'progress.tmp'
        temporary.write_text(pack(values), encoding='utf-8')
        temporary.replace(out/'progress.json')
        print(json.dumps(values, ensure_ascii=False), flush=True)
    files, discovery = base.discover(args.root or base.DEFAULT_ROOTS, str(args.start), str(args.end), out)
    discovery['excluded']['日期范围外'] = discovery['excluded'].pop('一个月范围外', 0)
    progress(event='discovered', targetIccids=len(keys), files=len(files), workers=args.workers)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        files = list(pool.map(base.fingerprint, files))
    groups = defaultdict(list)
    errors = []
    for num, f in enumerate(files, 1):
        f['fileId'] = f'F{num:06}'
        if f.get('error'):
            f['scanStatus'] = 'failed'
            errors.append(dict(file=f['path'], error=f['error']))
        else:
            groups[f['sha256']].append(f)
    records, observations, rejected = {}, [], []
    progress(event='hashed', uniqueContents=len(groups), duplicateCopies=len(files)-len(groups))
    completed, last_update = 0, time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        remaining, pending = iter(groups.values()), {}
        def submit():
            aliases = next(remaining, None)
            if aliases is not None:
                pending[pool.submit(scan_book, aliases[0], set(keys))] = aliases
        for _ in range(args.workers*2):
            submit()
        while pending:
            ready, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in ready:
                aliases = pending.pop(future)
                result = future.result()
                for f in aliases:
                    f['scanStatus'] = 'failed' if result.get('error') else 'scanned'
                    if result.get('error'):
                        f['error'] = result['error']
                        errors.append(dict(file=f['path'], error=f['error']))
                if not result.get('error'):
                    for row in result['rows']:
                        record = row['record']
                        rid = digest(record)
                        records.setdefault(rid, dict(record, recordId=rid))
                        observations.extend(dict(iccid=row['iccid'], recordId=rid, fileId=f['fileId'], sheet=row['sheet'],
                                                 row=row['row'], iccidCell=row['iccidCell'], headerRow=row['headerRow']) for f in aliases)
                    rejected.extend(dict(r, fileId=f['fileId']) for r in result['rejected'] for f in aliases)
                completed += 1
                submit()
                if time.monotonic()-last_update >= 15 or completed == len(groups):
                    progress(event='scanning', completed=completed, total=len(groups),
                             matchedIccids=len({r['iccid'] for r in records.values()}), uniqueRecords=len(records))
                    last_update = time.monotonic()
    if hashlib.sha256(args.iccid_list.read_bytes()).hexdigest() != input_hash:
        raise ValueError('输入清单在运行期间发生变化')
    records = dict(sorted(records.items(), key=lambda x: (x[1]['iccid'], x[0])))
    notes = [
        ['目标', f'仅用户清单的{len(keys)}个ICCID，完整输入和哈希见input_list.json'],
        ['范围', f'{args.start}至{args.end}（含首尾）；只读本地Excel，未读任务F库、JSON日志或访问后台'],
        ['使用方法', '先看查询总览；卡槽日期历史按同条记录展示；历史记录的记录编号可关联全部字段和逐条来源'],
        ['完整性', '保留命中行全部非空字段、原列名、XML值、日期格式、公式文本及全部Excel路径/工作表/单元格；相同内容版本去重但所有出处保留'],
        ['日期', '上卡日期、创建时间、激活日期严格区分；不推算、不互填；00:00:00可能只是日期存储方式，不能视为精确上卡时刻'],
        ['历史与现在', '文件名/归档目录日期优先，无明确日期用修改时间；文件日期不是上卡日期或位置生效时间；历史卡槽不代表当前卡槽'],
        ['歧义', '多ICCID且值不同或无明确ICCID列的行仅保留原字段待核对，不参与日期及卡槽汇总；多版本/不同来源均保留，不覆盖'],
        ['公式与精度', 'ICCID必须为可靠文本，允许仅去末尾一个F/f；数字型、公式型标识单列；原公式仅保存文本不执行，公式缓存不填入核心日期'],
        ['来源', 'Excel可能包含Google或历史加工/本地库衍生sheet，来源类型明确标注；未读取Excel之外的业务数据'],
        ['原始格式', 'Excel日期按原1900/1904基准转换显示，同时保留XML原值和数字格式；不添加时区；超长字段按30000字符分段，不截断'],
        ['缺失', '缺失留空；扫描文件清单列明不能解析文件；仅说明本次可访问文件范围，无历史备份挂载不等于数据不存在'],
        ['安全', '未更改系统、生产数据库或历史文件；新文件为离线历史查询副本，不是生产数据恢复']]
    tables, summary = output_tables(keys, records, observations, files, rejected, notes)
    base.write_json(out/'summary_by_iccid.json', summary)
    base.write_json(out/'files.json', files)
    write_gzip(out/'all_records.jsonl.gz', records.values())
    write_gzip(out/'all_sources.jsonl.gz', observations)
    write_gzip(out/'unreliable_matches.jsonl.gz', rejected)
    progress(event='writing_verifying', matchedIccids=sum(x['found'] for x in summary), uniqueRecords=len(records), sourceRows=len(observations))
    workbook = out/'指定ICCID_近半年完整历史还原.xlsx'
    checked = export_excel(workbook, tables)
    counts = dict(targetIccids=len(keys), matchedIccids=sum(x['found'] for x in summary),
                  withSlots=sum(bool(x['slots']) for x in summary),
                  withCardLoadedDate=sum(bool(x['values']['上卡日期']) for x in summary),
                  withCreationTime=sum(bool(x['values']['创建时间']) for x in summary),
                  withBothDates=sum(bool(x['values']['上卡日期']) and bool(x['values']['创建时间']) for x in summary),
                  uniqueRecords=len(records), sourceRows=len(observations), unreliableMatches=len(rejected))
    report = dict(status='completed_with_invalid_files' if errors else 'completed', **counts,
                  start=str(args.start), end=str(args.end), selectedFiles=len(files), uniqueContents=len(groups),
                  discovery=discovery, minSelectedFileDate=min((x['date'] for x in files),default=''),
                  errors=errors, databaseRead=False, jsonLogsRead=False, backendAccessed=False,
                  scriptSha256=script_hash, inputSha256=input_hash, workbook=str(workbook), verification=checked,
                  durationSeconds=round(time.monotonic()-started,2))
    base.write_json(out/'summary.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
