// Pure guards, shared by the scheduled importer and offline regression tests.
export const normalizeIccid = value => typeof value === 'string'
  ? value.trim().replace(/[Ff]$/, '') : '';
const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value+'T00:00:00Z'))
  && new Date(value+'T00:00:00Z').toISOString().slice(0,10) === value;
export function validateImportInput(input, runId) {
  if (input?.version !== 1 || input.runId !== runId
      || !/^\d{8}T\d{6}\+0800$/.test(runId || '')
      || input.scope !== 'current_task_a_system_missing_f_dates'
      || input.independentReadbackPassed !== true || !Array.isArray(input.records)
      || !Number.isSafeInteger(input.fileRows) || input.fileRows < 1
      || !/^[a-f0-9]{64}$/.test(input.fileSha256 || '')
      || typeof input.file !== 'string' || !input.file.startsWith('/')
      || input.records.length !== input.fileRows) throw Error('F_IMPORT_INPUT_UNCONFIRMED');
  const keys = new Set(), phones = new Set();
  for (const r of input.records) {
    if (!/^\d{18,22}$/.test(r.iccid || '') || typeof r.iccid !== 'string'
        || !/^\d{10}$/.test(r.phone || '') || typeof r.phone !== 'string'
        || typeof r.slot !== 'string' || !r.slot || !date(r.activation) || !date(r.cutoff)
        || (Date.parse(r.cutoff)-Date.parse(r.activation))/86400000 !== 29
        || keys.has(r.iccid) || phones.has(r.phone)) throw Error('F_IMPORT_IDENTITY_OR_DATE_INVALID');
    keys.add(r.iccid); phones.add(r.phone);
  }
  return input.records;
}
export function checkBackend(input, rows, before = null, requireDates = false) {
  if (!Array.isArray(rows) || rows.length !== input.records.length) throw Error('F_IMPORT_BACKEND_COVERAGE_FAILED');
  const byPhone = new Map(), ids = new Set();
  for (const row of rows) {
    if (typeof row.cardNo !== 'string' || !/^\d{10}$/.test(row.cardNo)
        || row.id == null || !/^\d+$/.test(String(row.id))
        || byPhone.has(row.cardNo) || ids.has(String(row.id))) throw Error('F_IMPORT_BACKEND_IDENTITY_FAILED');
    byPhone.set(row.cardNo, row); ids.add(String(row.id));
  }
  const previous = new Map((before || []).map(row=>[row.cardNo,row]));
  let matchedDates = 0;
  for (const r of input.records) {
    const row = byPhone.get(r.phone), old = previous.get(r.phone);
    if (!row || normalizeIccid(row.cardStore) !== r.iccid || row.slotIdentif !== r.slot
        || (before && (!old || row.id !== old.id))) throw Error('F_IMPORT_BACKEND_IDENTITY_CHANGED');
    // The list endpoint omits null date properties. Identity comes from the
    // exact phone, backend ID, ICCID and slot, not the presence of nullable keys.
    // Missing dates remain unverified after submission; matchedDates below
    // requires both actual date values, so omission can never prove completion.
    for (const [field, expected] of [['chatrActivateDate',r.activation],['chatrPlanExpire',r.cutoff]]) {
      const actual = row[field];
      if (actual !== null && actual !== '' && actual !== undefined && actual !== expected) {
        throw Error('F_IMPORT_EXISTING_DATE_CONFLICT');
      }
    }
    if (row.chatrActivateDate === r.activation && row.chatrPlanExpire === r.cutoff) matchedDates++;
  }
  return {matchedDates, passed: !requireDates || matchedDates === input.fileRows};
}
export function validateImportAcceptance(response, count) {
  const diagnostic={httpStatus:Number.isSafeInteger(response?.status)?response.status:null,
    expectedRows:count,responseCode:null,acceptedRows:null,messageShapeMatched:false};
  const fail=code=>{throw Object.assign(new Error(code),{acceptanceDiagnostic:diagnostic});};
  if (response?.status !== 200 || response?.ok !== true || typeof response.text !== 'string') {
    fail('F_IMPORT_HTTP_RESPONSE_UNCONFIRMED');
  }
  let body; try {body = JSON.parse(response.text);} catch {fail('F_IMPORT_RESPONSE_INVALID');}
  diagnostic.responseCode=Number.isSafeInteger(body?.code)?body.code:null;
  // HTTP 200 only confirms transport. Preserve a distinct business failure
  // without logging the server message (which may contain private values).
  if (diagnostic.responseCode !== null && diagnostic.responseCode !== 200) {
    fail('F_IMPORT_BACKEND_BUSINESS_ERROR');
  }
  const match = typeof body?.msg === 'string'
    ? /^已提交后台处理，有效 (\d+) 行。完成后请刷新列表查看。$/.exec(body.msg) : null;
  diagnostic.messageShapeMatched=!!match;
  diagnostic.acceptedRows=match&&Number.isSafeInteger(Number(match[1]))?Number(match[1]):null;
  if (body?.code !== 200 || !match || diagnostic.acceptedRows !== count) fail('F_IMPORT_ACCEPTED_COUNT_UNCONFIRMED');
  return {acceptedRows:count, accepted:true, completed:false};
}
