const HISTORY_URL = 'https://adminsms.smsoug.com/history';
const DEFAULT_TIMEOUT_MS = 180000;
const DEFAULT_POLL_MS = 5000;
const CLOCK_TOLERANCE_MS = 30000;

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export function parseBeijingTimestamp(value) {
  const match = String(value || '').trim().match(
    /^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$/,
  );
  if (!match) return Number.NaN;
  const [, year, month, day, hour, minute, second] = match;
  return Date.parse(`${year}-${month}-${day}T${hour}:${minute}:${second}+08:00`);
}

export function isAtRelatedRecord(record) {
  const type = String(record?.commandType || '').trim().toLowerCase();
  const detail = String(record?.commandDetail || '').trim();
  if (type === 'sendto') return false;
  return type === 'at' || /(?:^|\s)at\+/i.test(detail);
}

export function isExecutedSuccessfully(record) {
  const status = String(record?.executionStatus || '').trim();
  const result = String(record?.executionResult || '').trim();
  return status.includes('已执行') && !/FAILED|失败|错误|异常/i.test(result);
}

export function matchesAction(record, action) {
  const detail = String(record?.commandDetail || '').trim();
  if (action === 'mms') return /QICSGP|cfun/i.test(detail);
  if (action === 'restart') return /cfun|reset|reboot|qpowd/i.test(detail);
  return true;
}

export function selectFreshAtRecords(records, {
  action,
  submittedAt,
  selectedDeviceNames,
  clockToleranceMs = CLOCK_TOLERANCE_MS,
} = {}) {
  const submittedAtMs = new Date(submittedAt).getTime();
  if (!Number.isFinite(submittedAtMs)) throw new Error(`Invalid submission time: ${submittedAt}`);
  const selected = new Set((selectedDeviceNames || []).map(String));
  return records.filter(record => {
    const createdAtMs = parseBeijingTimestamp(record.createdAt);
    return isAtRelatedRecord(record)
      && isExecutedSuccessfully(record)
      && matchesAction(record, action)
      && Number.isFinite(createdAtMs)
      && createdAtMs >= submittedAtMs - clockToleranceMs
      && (!selected.size || selected.has(String(record.machineName || '')));
  });
}

async function waitForHistoryReady(client, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const state = await client.evaluate('({href: location.href, readyState: document.readyState})');
    if (state.href.startsWith(HISTORY_URL) && state.readyState === 'complete') return state;
    await sleep(500);
  }
  throw new Error('Command record page did not finish loading');
}

export async function readCommandRecordFirstPage(client) {
  const result = await client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const body = clean(document.body?.innerText);
    const headers = [...document.querySelectorAll('.el-table__header-wrapper th')]
      .map(cell => clean(cell.innerText));
    const wrappers = [...document.querySelectorAll('.el-table__body-wrapper')];
    const populated = wrappers.filter(wrapper => wrapper.querySelectorAll('tbody tr').length);
    const rows = populated.length === 1
      ? [...populated[0].querySelectorAll('tbody tr')].map(row =>
          [...row.querySelectorAll('td')].map(cell => clean(cell.innerText)))
      : [];
    return {
      href: location.href,
      loginExpired: /登录状态已过期|重新登录/.test(body) || /\\/login(?:$|[?#])/.test(location.href),
      headers,
      rows,
      totalText: body.match(/共\\s*\\d+\\s*条/)?.[0] || '',
      pageSize: [...document.querySelectorAll('input.el-input__inner')]
        .map(element => element.value).find(value => /条\\/页$/.test(value)) || '',
    };
  })()`);
  if (result.loginExpired) throw new Error('LOGIN_EXPIRED while reading command records');
  const requiredHeaders = [
    '机器名称', '指令类型', '指令详情', '执行状态', '创建时间', '最后更新时间', '执行结果',
  ];
  const missing = requiredHeaders.filter(header => !result.headers.includes(header));
  if (missing.length) throw new Error(`Command record headers are incomplete: ${missing.join(',')}`);
  const index = Object.fromEntries(result.headers.map((header, position) => [header, position]));
  return {
    href: result.href,
    pageSize: result.pageSize,
    totalText: result.totalText,
    headers: result.headers,
    records: result.rows.map(cells => ({
      machineId: cells[index['机器ID']] || '',
      machineName: cells[index['机器名称']] || '',
      slot: cells[index['卡槽']] || '',
      commandType: cells[index['指令类型']] || '',
      commandDetail: cells[index['指令详情']] || '',
      executionStatus: cells[index['执行状态']] || '',
      createdAt: cells[index['创建时间']] || '',
      updatedAt: cells[index['最后更新时间']] || '',
      executionResult: cells[index['执行结果']] || '',
    })),
  };
}

export async function verifyLatestAtCommand(client, {
  action,
  submittedAt,
  selectedDeviceNames,
  timeoutMs = Number(process.env.OPENSMS_COMMAND_RECORD_TIMEOUT_MS || DEFAULT_TIMEOUT_MS),
  pollMs = DEFAULT_POLL_MS,
} = {}) {
  if (!['maintenance', 'restart', 'mms'].includes(action)) {
    throw new Error(`Unsupported command-record action: ${action}`);
  }
  await client.send('Page.navigate', { url: HISTORY_URL }, 60000);
  await waitForHistoryReady(client);
  await sleep(1500);
  const deadline = Date.now() + timeoutMs;
  let attempts = 0;
  let lastPage = null;
  while (Date.now() < deadline) {
    attempts += 1;
    lastPage = await readCommandRecordFirstPage(client);
    const freshAtRecords = selectFreshAtRecords(lastPage.records, {
      action,
      submittedAt,
      selectedDeviceNames,
    });
    const sendToExcludedCount = lastPage.records.filter(record =>
      String(record.commandType || '').trim().toLowerCase() === 'sendto').length;
    if (freshAtRecords.length) {
      return {
        success: true,
        action,
        checked: true,
        firstPageOnly: true,
        checkedAt: new Date().toISOString(),
        attempts,
        pageUrl: lastPage.href,
        pageSize: lastPage.pageSize,
        totalText: lastPage.totalText,
        firstPageRecordCount: lastPage.records.length,
        sendToExcludedCount,
        atRelatedExecutedCount: freshAtRecords.length,
        latestAtRecord: freshAtRecords[0],
        matchedAtRecords: freshAtRecords,
        proof: '发现本次提交后、所选设备内、非 sendTo 的 AT 相关指令已执行记录',
      };
    }
    if (Date.now() + pollMs >= deadline) break;
    await sleep(pollMs);
    await client.send('Page.reload', { ignoreCache: false }, 60000);
    await waitForHistoryReady(client);
    await sleep(1000);
  }
  return {
    success: false,
    action,
    checked: true,
    firstPageOnly: true,
    checkedAt: new Date().toISOString(),
    attempts,
    pageUrl: lastPage?.href || HISTORY_URL,
    pageSize: lastPage?.pageSize || '',
    totalText: lastPage?.totalText || '',
    firstPageRecordCount: lastPage?.records?.length || 0,
    sendToExcludedCount: (lastPage?.records || []).filter(record =>
      String(record.commandType || '').trim().toLowerCase() === 'sendto').length,
    atRelatedExecutedCount: 0,
    latestAtRecord: null,
    matchedAtRecords: [],
    proof: '命令记录第一页未发现本次提交后的有效 AT 相关已执行记录',
  };
}
