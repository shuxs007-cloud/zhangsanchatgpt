import fs from 'node:fs/promises';
import path from 'node:path';
import {acquireSharedOpenSmsSession, SHARED_BROWSER_NAME} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage} from '../opensms_protocol_client.mjs';
import {verifyLatestAtCommand} from './opensms_device_command_record.mjs';

const PROFILE_ID = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const TARGET_URL = 'https://adminsms.smsoug.com/machineconfig';
const MODE = process.argv[2] || 'probe';
const SNAPSHOT_DIR = path.resolve('opensms_artifacts/snapshots');
const LAST_RUN_PATH = path.resolve('opensms_device_maintenance_last_run.json');
const LOCK_PATH = path.join(SNAPSHOT_DIR, '.opensms_device_maintenance.lock');

class CdpClient {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = new Map();
  }

  async connect() {
    await new Promise((resolve, reject) => {
      this.socket = new WebSocket(this.url);
      const timer = setTimeout(() => reject(new Error('CDP WebSocket connection timeout')), 10000);
      this.socket.addEventListener('open', () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
      this.socket.addEventListener('error', event => {
        clearTimeout(timer);
        reject(new Error(`CDP WebSocket error: ${event.message || 'unknown'}`));
      }, { once: true });
      this.socket.addEventListener('message', event => {
        const message = JSON.parse(event.data);
        if (message.method) {
          for (const handler of this.handlers.get(message.method) || []) {
            try {
              handler(message.params || {});
            } catch {}
          }
        }
        if (!message.id) return;
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        clearTimeout(pending.timer);
        if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
        else pending.resolve(message.result);
      });
    });
  }

  on(method, handler) {
    const handlers = this.handlers.get(method) || [];
    handlers.push(handler);
    this.handlers.set(method, handlers);
  }

  send(method, params = {}, timeoutMs = 30000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP command timeout: ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression, { awaitPromise = true, returnByValue = true } = {}) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise,
      returnByValue,
      userGesture: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    }
    return result.result?.value;
  }

  close() {
    this.socket?.close();
  }
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function beijingParts(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date);
  return Object.fromEntries(parts.map(part => [part.type, part.value]));
}

function beijingTimestamp(date = new Date()) {
  const p = beijingParts(date);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

function beijingDate(date = new Date()) {
  const p = beijingParts(date);
  return `${p.year}-${p.month}-${p.day}`;
}

function receiptStamp(date = new Date()) {
  const p = beijingParts(date);
  return `${p.year}${p.month}${p.day}${p.hour}${p.minute}${p.second}`;
}

async function waitForReady(client, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const state = await client.evaluate('({href: location.href, readyState: document.readyState})');
    if (state.href.startsWith(TARGET_URL) && state.readyState === 'complete') return state;
    await sleep(500);
  }
  throw new Error('machineconfig did not reach readyState=complete');
}

async function trustedClick(client, point, label) {
  if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) {
    throw new Error(`${label} click point is invalid: ${JSON.stringify(point)}`);
  }
  await client.send('Input.dispatchMouseEvent', {
    type: 'mousePressed',
    x: point.x,
    y: point.y,
    button: 'left',
    clickCount: 1,
  });
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased',
    x: point.x,
    y: point.y,
    button: 'left',
    clickCount: 1,
  });
}

async function inspectPage(client) {
  return client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) !== 0
        && rect.width > 0 && rect.height > 0;
    };
    const controls = [...document.querySelectorAll('button, [role="button"], select, input')]
      .filter(visible)
      .map((element, index) => ({
        index,
        tag: element.tagName,
        type: element.type || '',
        text: clean(element.innerText || element.getAttribute('aria-label') || element.placeholder),
        name: element.name || '',
        value: element.type === 'password' ? '[redacted]' : clean(element.value),
        checked: element.type === 'checkbox' ? element.checked : undefined,
        disabled: Boolean(element.disabled),
        className: clean(element.className),
        options: element.tagName === 'SELECT'
          ? [...element.options].map(option => ({ text: clean(option.textContent), value: option.value, selected: option.selected }))
          : undefined,
      }));
    const rows = [...document.querySelectorAll('table tbody tr')].map((row, index) => ({
      index,
      text: clean(row.innerText),
      checkboxCount: row.querySelectorAll('input[type="checkbox"]').length,
      checkedCount: [...row.querySelectorAll('input[type="checkbox"]')].filter(input => input.checked).length,
      cells: [...row.querySelectorAll('td')].map(cell => clean(cell.innerText)),
    }));
    return {
      href: location.href,
      title: document.title,
      bodyText: clean(document.body?.innerText).slice(0, 12000),
      controls,
      rowCount: rows.length,
      rows: rows.slice(0, 220),
      dialogs: [...document.querySelectorAll('[role="dialog"], .el-dialog, .ant-modal, .modal')]
        .filter(visible)
        .map(element => clean(element.innerText)),
    };
  })()`);
}

async function setPageSize200(client) {
  const opened = await client.evaluate(`(() => {
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const inputs = [...document.querySelectorAll('input.el-input__inner')]
      .filter(element => visible(element) && /^(50|100|200)条\\/页$/.test(element.value));
    if (inputs.length !== 1) return { ok: false, reason: 'page-size input is not unique', count: inputs.length };
    if (inputs[0].value === '200条/页') return { ok: true, already: true };
    inputs[0].scrollIntoView({ block: 'center', inline: 'nearest' });
    const rect = inputs[0].getBoundingClientRect();
    return {
      ok: true,
      already: false,
      previous: inputs[0].value,
      point: { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
    };
  })()`);
  if (!opened.ok) throw new Error(JSON.stringify(opened));
  if (!opened.already) {
    await trustedClick(client, opened.point, 'page-size input');
    const optionDeadline = Date.now() + 1500;
    let option = null;
    while (Date.now() < optionDeadline && !option) {
      option = await client.evaluate(`(() => {
        const visible = element => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        };
        const options = [...document.querySelectorAll('.el-select-dropdown__item')]
          .filter(visible)
          .filter(element => String(element.textContent || '').replace(/\\s+/g, '').trim() === '200条/页');
        if (options.length !== 1) return null;
        const rect = options[0].getBoundingClientRect();
        return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
      })()`);
      if (!option) await sleep(250);
    }
    if (option) {
      await trustedClick(client, option, '200-page option');
    } else {
      const fallback = await client.evaluate(`(() => {
        const options = [...document.querySelectorAll('.el-select-dropdown__item')]
          .filter(element => String(element.textContent || '').replace(/\\s+/g, '').trim() === '200条/页');
        if (options.length !== 1) return { ok: false, count: options.length };
        options[0].click();
        return { ok: true };
      })()`);
      if (!fallback.ok) throw new Error(`200-page option is not unique: ${JSON.stringify(fallback)}`);
    }
  }
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const state = await client.evaluate(`(() => {
      const body = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
      const totalMatch = body.match(/共\\s*(\\d+)\\s*条/);
      const wrappers = [...document.querySelectorAll('.el-table__body-wrapper')];
      return {
        pageSize: [...document.querySelectorAll('input.el-input__inner')]
          .map(element => element.value).find(value => /条\\/页$/.test(value)) || '',
        total: totalMatch ? Number(totalMatch[1]) : null,
        wrapperCount: wrappers.length,
        rowCount: wrappers.length === 1 ? wrappers[0].querySelectorAll('tbody tr').length : null,
      };
    })()`);
    if (state.pageSize === '200条/页' && state.wrapperCount === 1 && state.total > 0 && state.rowCount === state.total) return state;
    await sleep(500);
  }
  throw new Error('page size did not settle at 200条/页 with one complete current-page table');
}

async function inspectSelectionStructure(client) {
  return client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const wrappers = [...document.querySelectorAll('.el-table__body-wrapper')].map((wrapper, index) => {
      const rows = [...wrapper.querySelectorAll('tbody tr')];
      return {
        index,
        rowCount: rows.length,
        deviceNames: rows.map(row => clean(row.querySelectorAll('td')[1]?.innerText)).filter(Boolean),
        checkboxCount: wrapper.querySelectorAll('tbody input[type="checkbox"]').length,
        checkedCount: [...wrapper.querySelectorAll('tbody input[type="checkbox"]')].filter(input => input.checked).length,
      };
    });
    const headerCheckboxes = [...document.querySelectorAll('.el-table__header-wrapper input[type="checkbox"]')].map((input, index) => ({
      index,
      checked: input.checked,
      disabled: input.disabled,
      visible: visible(input),
      outerHTML: input.parentElement?.outerHTML.slice(0, 1000),
    }));
    return { wrappers, headerCheckboxes };
  })()`);
}

async function auditAvailableCards(client) {
  return client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const wrappers = [...document.querySelectorAll('.el-table__body-wrapper')];
    if (wrappers.length !== 1) return { ok: false, reason: 'main table wrapper is not unique', count: wrappers.length };
    const rows = [...wrappers[0].querySelectorAll('tbody tr')].map(row => {
      const cells = [...row.querySelectorAll('td')].map(cell => clean(cell.innerText));
      return { deviceName: cells[1], maxPorts: Number(cells[2]), availableCards: Number(cells[3]) };
    });
    return {
      ok: true,
      rowCount: rows.length,
      zeroAvailableDevices: rows.filter(row => row.availableCards === 0),
      positiveAvailableCount: rows.filter(row => row.availableCards > 0).length,
    };
  })()`);
}

async function readOpenConfirmation(client) {
  return client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const dialogs = [...document.querySelectorAll('.el-message-box__wrapper[role="dialog"]')].filter(visible);
    return {
      dialogCount: dialogs.length,
      dialogs: dialogs.map(dialog => ({
        text: clean(dialog.innerText),
        buttons: [...dialog.querySelectorAll('button')].filter(visible).map(button => ({
          text: clean(button.innerText),
          disabled: button.disabled,
          className: clean(button.className),
        })),
      })),
    };
  })()`);
}

function validateDeviceNames(deviceNames) {
  if (deviceNames.length < 41) throw new Error(`Device list is unexpectedly short: ${deviceNames.length}`);
  const unique = new Set(deviceNames);
  if (unique.size !== deviceNames.length) throw new Error('Device names are not unique');
  const missingSpecial = [];
  for (let number = 201; number <= 241; number += 1) {
    if (!unique.has(String(number))) missingSpecial.push(String(number));
  }
  if (missingSpecial.length) throw new Error(`Missing devices 201-241: ${missingSpecial.join(',')}`);
}

async function selectAllAndOpenMaintenance(client) {
  const before = await inspectSelectionStructure(client);
  if (before.wrappers.length !== 1) throw new Error('Main device table wrapper is not unique');
  const deviceNames = before.wrappers[0].deviceNames;
  validateDeviceNames(deviceNames);
  const expectedCount = deviceNames.length;
  if (before.headerCheckboxes.length !== 1) throw new Error('Header select-all checkbox is not unique');

  const alreadySelected = before.wrappers[0].checkedCount === expectedCount && before.headerCheckboxes[0].checked;
  if (!alreadySelected) {
    if (before.wrappers[0].checkedCount !== 0 || before.headerCheckboxes[0].checked) {
      throw new Error(`Unexpected partial device selection: ${JSON.stringify(before)}`);
    }
    const clicked = await client.evaluate(`(() => {
      const targets = [...document.querySelectorAll('.el-table__header-wrapper .el-checkbox__inner')];
      if (targets.length !== 1) return { ok: false, count: targets.length };
      targets[0].click();
      return { ok: true };
    })()`);
    if (!clicked.ok) throw new Error(`Select-all click failed: ${JSON.stringify(clicked)}`);
  }

  const selectDeadline = Date.now() + 10000;
  let selected = null;
  while (Date.now() < selectDeadline) {
    selected = await inspectSelectionStructure(client);
    if (selected.wrappers[0]?.checkedCount === expectedCount && selected.headerCheckboxes[0]?.checked) break;
    await sleep(250);
  }
  if (selected?.wrappers[0]?.checkedCount !== expectedCount || !selected.headerCheckboxes[0]?.checked) {
    throw new Error(`Select-all did not produce ${expectedCount} checked devices: ${JSON.stringify(selected)}`);
  }

  const existingDialog = await readOpenConfirmation(client);
  if (existingDialog.dialogCount > 1) {
    throw new Error(`Maintenance confirmation dialog is not unique: ${JSON.stringify(existingDialog)}`);
  }
  if (!existingDialog.dialogCount) {
    const opened = await client.evaluate(`(() => {
      const visible = element => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
      };
      const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
      const buttons = [...document.querySelectorAll('button')]
        .filter(visible)
        .filter(button => clean(button.innerText) === '一键维护');
      if (buttons.length !== 1) return { ok: false, reason: 'maintenance button is not unique', count: buttons.length };
      if (buttons[0].disabled) return { ok: false, reason: 'maintenance button is disabled' };
      buttons[0].click();
      return { ok: true };
    })()`);
    if (!opened.ok) throw new Error(`Open maintenance confirmation failed: ${JSON.stringify(opened)}`);
  }

  const dialogDeadline = Date.now() + 10000;
  let confirmation = null;
  while (Date.now() < dialogDeadline) {
    confirmation = await readOpenConfirmation(client);
    if (confirmation.dialogCount) break;
    await sleep(250);
  }
  if (confirmation?.dialogCount !== 1) {
    throw new Error(`Maintenance confirmation dialog is not unique: ${JSON.stringify(confirmation)}`);
  }
  return { deviceNames, selectedCount: expectedCount, confirmation };
}

function validateConfirmation(maintenance) {
  const dialog = maintenance.confirmation.dialogs[0];
  const text = dialog.text;
  const match = text.match(/确认对设备\s*"([^"]+)"\s*执行一键维护操作吗？/);
  if (!match) throw new Error('Confirmation does not identify a one-key maintenance device list');
  const modalNames = match[1].split('、').map(value => value.trim()).filter(Boolean);
  if (JSON.stringify(modalNames) !== JSON.stringify(maintenance.deviceNames)) {
    throw new Error('Confirmation device list does not exactly match the selected devices');
  }
  const requiredPhrases = [
    '执行一键维护操作吗',
    '自动配置设备的AT指令和APN配置',
    '各设备并行',
    '每步间隔10秒',
  ];
  const missingPhrases = requiredPhrases.filter(phrase => !text.includes(phrase));
  if (missingPhrases.length) throw new Error(`Confirmation is missing required phrases: ${missingPhrases.join(',')}`);
  const confirmButtons = dialog.buttons.filter(button => button.text === '确定' && !button.disabled);
  const cancelButtons = dialog.buttons.filter(button => button.text === '取消' && !button.disabled);
  if (confirmButtons.length !== 1 || cancelButtons.length !== 1) {
    throw new Error('Confirmation buttons are not unique');
  }
  return { confirmationText: text, modalNames, requiredPhrases };
}

async function submitMaintenance(client, maintenance, validation) {
  const requests = new Map();
  const responses = [];
  await client.send('Network.enable');
  client.on('Network.requestWillBeSent', params => {
    requests.set(params.requestId, {
      url: params.request?.url || '',
      method: params.request?.method || '',
      postData: params.request?.postData || '',
      timestamp: params.timestamp,
    });
  });
  client.on('Network.responseReceived', params => {
    const request = requests.get(params.requestId) || {};
    responses.push({
      requestId: params.requestId,
      url: params.response?.url || request.url || '',
      method: request.method || '',
      status: params.response?.status,
      statusText: params.response?.statusText || '',
      type: params.type || '',
      mimeType: params.response?.mimeType || '',
    });
  });

  const submittedAt = new Date();
  const clicked = await client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const dialogs = [...document.querySelectorAll('.el-message-box__wrapper[role="dialog"]')].filter(visible);
    if (dialogs.length !== 1) return { ok: false, reason: 'dialog is not unique', count: dialogs.length };
    const buttons = [...dialogs[0].querySelectorAll('button')]
      .filter(visible)
      .filter(button => clean(button.innerText) === '确定' && !button.disabled);
    if (buttons.length !== 1) return { ok: false, reason: 'confirm button is not unique', count: buttons.length };
    buttons[0].click();
    return { ok: true };
  })()`);
  if (!clicked.ok) throw new Error(`Final confirmation click failed: ${JSON.stringify(clicked)}`);

  const pageMessages = new Set();
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    const state = await client.evaluate(`(() => {
      const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
      const visible = element => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
      };
      return {
        dialogCount: [...document.querySelectorAll('.el-message-box__wrapper[role="dialog"]')].filter(visible).length,
        messages: [...document.querySelectorAll('.el-message, .el-notification')].filter(visible).map(element => clean(element.innerText)),
      };
    })()`);
    for (const message of state.messages) if (message) pageMessages.add(message);
    if (!state.dialogCount && pageMessages.size) break;
    await sleep(250);
  }
  await sleep(1500);

  const mutationResponses = responses.filter(response =>
    response.url.includes('/api/') && response.method && response.method !== 'GET');
  for (const response of mutationResponses) {
    response.bodySample = await client.send('Network.getResponseBody', { requestId: response.requestId })
      .then(result => String(result.body || '').slice(0, 2000))
      .catch(error => `[unavailable: ${error.message}]`);
  }
  const messages = [...pageMessages];
  const hasPositiveMessage = messages.some(message => /一键维护.*(提交|成功)|任务已提交|操作成功/.test(message));
  const hasNegativeMessage = messages.some(message => /失败|错误|异常/.test(message));
  const networkAccepted = mutationResponses.length === 1 && mutationResponses[0].status === 200;
  const submissionSuccess = networkAccepted && hasPositiveMessage && !hasNegativeMessage;
  const commandRecordVerification = submissionSuccess
    ? await verifyLatestAtCommand(client, {
        action: 'maintenance',
        submittedAt: submittedAt.toISOString(),
        selectedDeviceNames: maintenance.deviceNames,
      })
    : null;
  const success = submissionSuccess && commandRecordVerification?.success === true;
  return {
    submittedAt: submittedAt.toISOString(),
    submittedAtBeijing: beijingTimestamp(submittedAt),
    completedAt: new Date().toISOString(),
    finalConfirmClicked: true,
    submissionMayHaveOccurred: true,
    mutationResponses,
    pageMessages: messages,
    networkAccepted,
    hasPositiveMessage,
    hasNegativeMessage,
    submissionSuccess,
    commandRecordVerification,
    success,
    confirmationText: validation.confirmationText,
    selectedDeviceNames: maintenance.deviceNames,
  };
}

async function readJsonIfExists(filePath) {
  try {
    return JSON.parse(await fs.readFile(filePath, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

async function writeExecutionReceipt(result, context) {
  await fs.mkdir(SNAPSHOT_DIR, { recursive: true });
  const receiptPath = path.join(SNAPSHOT_DIR, `opensms_device_maintenance_${receiptStamp()}.json`);
  const receipt = {
    stage: '阶段 1：设备监控一键维护',
    forcedOutsideWindow: true,
    dateBeijing: beijingDate(),
    batchCount: 1,
    batchSizes: [result.selectedDeviceNames.length],
    totalDeviceCount: result.selectedDeviceNames.length,
    candidateCount: result.selectedDeviceNames.length,
    backendValidCount: result.selectedDeviceNames.length,
    actualWriteDeviceCount: result.finalConfirmClicked ? result.selectedDeviceNames.length : 0,
    successCount: result.success ? result.selectedDeviceNames.length : 0,
    failureCount: result.success ? 0 : result.selectedDeviceNames.length,
    pageSize: 200,
    profileId: PROFILE_ID,
    workbenchTitle: context.workbenchTitle,
    cdpPort: context.port,
    url: TARGET_URL,
    confirmationMatched: true,
    selectedCountMatched: true,
    specialDevices201To241Complete: true,
    receiptPath,
    ...result,
  };
  await fs.writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { flag: 'wx' });
  if (receipt.success) {
    await fs.writeFile(LAST_RUN_PATH, `${JSON.stringify(receipt, null, 2)}\n`);
  }
  return receipt;
}

async function mainWithSharedBrowser(shared, background) {
  if (!['probe', 'prepare', 'preflight', 'confirm-probe', 'audit', 'execute'].includes(MODE)) throw new Error(`Unsupported mode: ${MODE}`);
  await fs.mkdir(SNAPSHOT_DIR, { recursive: true });
  let lockHandle = null;
  if (MODE === 'execute') {
    const previous = await readJsonIfExists(LAST_RUN_PATH);
    if (previous?.success && previous.dateBeijing === beijingDate()) {
      throw new Error(`Stage 1 already completed today: ${previous.receiptPath || LAST_RUN_PATH}`);
    }
    lockHandle = await fs.open(LOCK_PATH, 'wx').catch(error => {
      if (error.code === 'EEXIST') throw new Error(`Stage 1 is already running: ${LOCK_PATH}`);
      throw error;
    });
    await lockHandle.writeFile(`${JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() })}\n`);
  }
  const port = shared.session.port;
  const workbench = [{title:SHARED_BROWSER_NAME}];
  const target = background.target;

  const client = new CdpClient(target.webSocketDebuggerUrl);
  try {
    await client.connect();
    await client.send('Page.enable');
    await client.send('Runtime.enable');
    if (!target.url.endsWith('/machineconfig')) {
      await client.send('Page.navigate', { url: TARGET_URL }, 60000);
    }
    await waitForReady(client);
    await sleep(2000);
    let preparation = null;
    if (MODE === 'prepare' || MODE === 'preflight' || MODE === 'confirm-probe' || MODE === 'audit' || MODE === 'execute') {
      preparation = await setPageSize200(client);
      await sleep(2000);
    }
    const inspection = await inspectPage(client);
    const selectionStructure = MODE === 'preflight' || MODE === 'execute'
      ? await inspectSelectionStructure(client)
      : null;
    const maintenance = MODE === 'confirm-probe' || MODE === 'execute'
      ? await selectAllAndOpenMaintenance(client)
      : null;
    let executionReceipt = null;
    if (MODE === 'execute') {
      const validation = validateConfirmation(maintenance);
      const execution = await submitMaintenance(client, maintenance, validation);
      executionReceipt = await writeExecutionReceipt(execution, {
        port,
        workbenchTitle: workbench[0].title,
      });
    }
    const availableCardsAudit = MODE === 'audit' ? await auditAvailableCards(client) : null;
    console.log(JSON.stringify({
      mode: MODE,
      port,
      profileId: PROFILE_ID,
      browser: 'cloakbrowser',
      browserSessionId: shared.session.sessionId,
      sharedLogin: true,
      workbenchTitle: workbench[0].title,
      preparation: MODE === 'execute' ? { pageSize: preparation?.pageSize } : preparation,
      selectionStructure: MODE === 'execute' ? {
        selectedCount: maintenance?.selectedCount,
        deviceCount: maintenance?.deviceNames.length,
      } : selectionStructure,
      maintenance: MODE === 'execute' ? {
        selectedCount: maintenance?.selectedCount,
        confirmationText: maintenance?.confirmation.dialogs[0]?.text,
      } : maintenance,
      executionReceipt,
      availableCardsAudit,
      inspection: MODE === 'preflight' || MODE === 'audit' || MODE === 'execute' ? {
        href: inspection.href,
        title: inspection.title,
        rowCount: inspection.rowCount,
        dialogs: inspection.dialogs,
      } : inspection,
    }, null, 2));
    if (MODE === 'execute' && !executionReceipt.success) process.exitCode = 2;
  } finally {
    client.close();
    if (lockHandle) {
      await lockHandle.close().catch(() => {});
      await fs.unlink(LOCK_PATH).catch(() => {});
    }
  }
}

async function main() {
  if (!['probe', 'prepare', 'preflight', 'confirm-probe', 'audit', 'execute'].includes(MODE)) throw new Error(`Unsupported mode: ${MODE}`);
  const shared = await acquireSharedOpenSmsSession();
  let background;
  try {
    background = await createRawBackgroundPage({cdp:shared.session.cdp,url:TARGET_URL,authenticatedTargetId:shared.target.id});
    await mainWithSharedBrowser(shared,background);
  } finally {
    try {await background?.close();} finally {shared.release();}
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
