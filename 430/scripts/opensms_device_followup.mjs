import fs from 'node:fs/promises';
import path from 'node:path';
import {acquireSharedOpenSmsSession, SHARED_BROWSER_NAME} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage} from '../opensms_protocol_client.mjs';
import {verifyLatestAtCommand} from './opensms_device_command_record.mjs';

const PROFILE_ID = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const TARGET_ORIGIN = 'https://adminsms.smsoug.com';
const TARGET_URL = `${TARGET_ORIGIN}/machineconfig`;
const MODE = process.argv[2] || 'probe';
const SNAPSHOT_DIR = path.resolve('opensms_artifacts/snapshots');
const STAGE1_PATH = path.resolve('opensms_device_maintenance_last_run.json');
const LAST_RUN_PATH = path.resolve('opensms_device_followup_last_run.json');
const CUSTOM_RESTART_LAST_RUN_PATH = path.resolve('opensms_device_custom_restart_last_run.json');
const LOCK_PATH = path.join(SNAPSHOT_DIR, '.opensms_device_followup.lock');

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

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
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

async function readJsonIfExists(filePath) {
  try {
    return JSON.parse(await fs.readFile(filePath, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

async function writeJson(filePath, value) {
  await fs.writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`);
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
      point: { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
    };
  })()`);
  if (!opened.ok) throw new Error(JSON.stringify(opened));
  if (!opened.already) {
    let option = null;
    for (let attempt = 0; attempt < 1 && !option; attempt += 1) {
      await trustedClick(client, opened.point, 'page-size input');
      const optionDeadline = Date.now() + 1000;
      while (Date.now() < optionDeadline) {
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
        if (option) break;
        await sleep(250);
      }
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

async function inspect(client) {
  return client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const wrappers = [...document.querySelectorAll('.el-table__body-wrapper')];
    const rows = wrappers.length === 1 ? [...wrappers[0].querySelectorAll('tbody tr')].map(row => ({
      name: clean(row.querySelectorAll('td')[1]?.innerText),
      checked: Boolean(row.querySelector('input[type="checkbox"]')?.checked),
      text: clean(row.innerText),
    })) : [];
    return {
      href: location.href,
      title: document.title,
      bodyStart: clean(document.body?.innerText).slice(0, 4000),
      wrapperCount: wrappers.length,
      rowCount: rows.length,
      deviceNames: rows.map(row => row.name),
      checkedNames: rows.filter(row => row.checked).map(row => row.name),
      buttons: [...document.querySelectorAll('button')].filter(visible).map(button => ({
        text: clean(button.innerText),
        disabled: button.disabled,
        className: clean(button.className),
      })),
      dialogs: [...document.querySelectorAll('[role="dialog"], .el-dialog, .el-message-box__wrapper')]
        .filter(visible).map(dialog => clean(dialog.innerText)),
    };
  })()`);
}

function validateNames(deviceNames) {
  if (deviceNames.length < 41) throw new Error(`Device list is unexpectedly short: ${deviceNames.length}`);
  if (new Set(deviceNames).size !== deviceNames.length) throw new Error('Device names are not unique');
  const missing = [];
  for (let number = 201; number <= 241; number += 1) {
    if (!deviceNames.includes(String(number))) missing.push(String(number));
  }
  if (missing.length) throw new Error(`Missing devices 201-241: ${missing.join(',')}`);
}

async function selectedNames(client) {
  return client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const wrappers = [...document.querySelectorAll('.el-table__body-wrapper')];
    if (wrappers.length !== 1) return { ok: false, reason: 'main table wrapper is not unique', count: wrappers.length };
    const rows = [...wrappers[0].querySelectorAll('tbody tr')];
    return {
      ok: true,
      names: rows.filter(row => row.querySelector('input[type="checkbox"]')?.checked)
        .map(row => clean(row.querySelectorAll('td')[1]?.innerText)),
    };
  })()`);
}

async function selectExact(client, targetNames) {
  const result = await client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const target = new Set(${JSON.stringify(targetNames)});
    const wrappers = [...document.querySelectorAll('.el-table__body-wrapper')];
    if (wrappers.length !== 1) return { ok: false, reason: 'main table wrapper is not unique', count: wrappers.length };
    const rows = [...wrappers[0].querySelectorAll('tbody tr')];
    const available = rows.map(row => clean(row.querySelectorAll('td')[1]?.innerText));
    const missing = [...target].filter(name => !available.includes(name));
    if (missing.length) return { ok: false, reason: 'target devices are missing', missing };
    for (const row of rows) {
      const name = clean(row.querySelectorAll('td')[1]?.innerText);
      const input = row.querySelector('input[type="checkbox"]');
      const clickTarget = row.querySelector('.el-checkbox__inner');
      if (!input || !clickTarget) return { ok: false, reason: 'row checkbox is missing', name };
      const shouldCheck = target.has(name);
      if (Boolean(input.checked) !== shouldCheck) clickTarget.click();
    }
    return { ok: true };
  })()`);
  if (!result.ok) throw new Error(`Exact selection failed: ${JSON.stringify(result)}`);
  const expected = [...targetNames].sort();
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const state = await selectedNames(client);
    if (!state.ok) throw new Error(JSON.stringify(state));
    if (JSON.stringify([...state.names].sort()) === JSON.stringify(expected)) return state.names;
    await sleep(250);
  }
  throw new Error(`Selected device names did not settle exactly: ${JSON.stringify(await selectedNames(client))}`);
}

async function readRestartConfirmation(client) {
  return client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const dialogs = [...document.querySelectorAll('.el-message-box__wrapper[role="dialog"]')].filter(visible);
    return {
      count: dialogs.length,
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

async function openRestartConfirmation(client) {
  const opened = await client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const buttons = [...document.querySelectorAll('button')]
      .filter(visible).filter(button => clean(button.innerText) === '重启');
    if (buttons.length !== 1) return { ok: false, reason: 'restart button is not unique', count: buttons.length };
    if (buttons[0].disabled) return { ok: false, reason: 'restart button is disabled' };
    buttons[0].click();
    return { ok: true };
  })()`);
  if (!opened.ok) throw new Error(`Open restart confirmation failed: ${JSON.stringify(opened)}`);
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const dialog = await readRestartConfirmation(client);
    if (dialog.count) return dialog;
    await sleep(250);
  }
  throw new Error('Restart confirmation did not appear');
}

async function cancelRestartConfirmation(client) {
  const cancelled = await client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const dialogs = [...document.querySelectorAll('.el-message-box__wrapper[role="dialog"]')].filter(visible);
    if (dialogs.length !== 1) return { ok: false, reason: 'dialog is not unique', count: dialogs.length };
    const buttons = [...dialogs[0].querySelectorAll('button')]
      .filter(visible).filter(button => clean(button.innerText) === '取消' && !button.disabled);
    if (buttons.length !== 1) return { ok: false, reason: 'cancel button is not unique', count: buttons.length };
    buttons[0].click();
    return { ok: true };
  })()`);
  if (!cancelled.ok) throw new Error(`Cancel restart confirmation failed: ${JSON.stringify(cancelled)}`);
}

function validateRestartConfirmation(confirmation, selected) {
  if (confirmation.count !== 1) throw new Error(`Restart confirmation is not unique: ${JSON.stringify(confirmation)}`);
  const dialog = confirmation.dialogs[0];
  const match = dialog.text.match(/确认要重启设备\s*"([^"]+)"\s*吗？/);
  if (!match) throw new Error('Restart confirmation does not contain an explicit device list');
  const modalNames = match[1].split('、').map(value => value.trim()).filter(Boolean);
  if (JSON.stringify(modalNames) !== JSON.stringify(selected)) {
    throw new Error(`Restart confirmation device list mismatch: ${JSON.stringify({ selected, modalNames })}`);
  }
  const required = ['确认要重启设备', '设备将先保存配置', '约1分钟后重启'];
  const missing = required.filter(phrase => !dialog.text.includes(phrase));
  if (missing.length) throw new Error(`Restart confirmation is missing required phrases: ${missing.join(',')}`);
  const confirms = dialog.buttons.filter(button => button.text === '确定' && !button.disabled);
  const cancels = dialog.buttons.filter(button => button.text === '取消' && !button.disabled);
  if (confirms.length !== 1 || cancels.length !== 1) throw new Error('Restart confirmation buttons are not unique');
  return { confirmationText: dialog.text, modalNames };
}

function createNetworkRecorder(client) {
  const requests = new Map();
  const responses = [];
  client.on('Network.requestWillBeSent', params => {
    requests.set(params.requestId, {
      url: params.request?.url || '',
      method: params.request?.method || '',
      postData: params.request?.postData || '',
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
  return { responses };
}

async function clickRestartConfirm(client) {
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
      .filter(visible).filter(button => clean(button.innerText) === '确定' && !button.disabled);
    if (buttons.length !== 1) return { ok: false, reason: 'confirm button is not unique', count: buttons.length };
    buttons[0].click();
    return { ok: true };
  })()`);
  if (!clicked.ok) throw new Error(`Restart confirmation click failed: ${JSON.stringify(clicked)}`);
}

async function collectRestartResult(client, recorder, responseStart) {
  const pageMessages = new Set();
  const deadline = Date.now() + 15000;
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
        messages: [...document.querySelectorAll('.el-message, .el-notification')]
          .filter(visible).map(element => clean(element.innerText)),
      };
    })()`);
    for (const message of state.messages) if (message) pageMessages.add(message);
    if (!state.dialogCount && pageMessages.size) break;
    await sleep(250);
  }
  await sleep(1500);
  const mutationResponses = recorder.responses.slice(responseStart)
    .filter(response => response.url.includes('/api/') && response.method && response.method !== 'GET');
  for (const response of mutationResponses) {
    response.bodySample = await client.send('Network.getResponseBody', { requestId: response.requestId })
      .then(result => String(result.body || '').slice(0, 2000))
      .catch(error => `[unavailable: ${error.message}]`);
  }
  const messages = [...pageMessages];
  const hasPositiveMessage = messages.some(message => /重启.*(成功|提交|下发|发送)|任务已提交|操作成功/.test(message));
  const hasNegativeMessage = messages.some(message => /失败|错误|异常/.test(message));
  const networkAccepted = mutationResponses.length === 1 && mutationResponses[0].status === 200;
  return {
    mutationResponses,
    pageMessages: messages,
    hasPositiveMessage,
    hasNegativeMessage,
    networkAccepted,
    success: networkAccepted && hasPositiveMessage && !hasNegativeMessage,
  };
}

async function readMmsUi(client) {
  return client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const describe = dialog => ({
      text: clean(dialog.innerText),
      html: String(dialog.innerHTML || '').slice(0, 20000),
      buttons: [...dialog.querySelectorAll('button')].filter(visible).map(button => ({
        text: clean(button.innerText),
        disabled: button.disabled,
        className: clean(button.className),
      })),
      labels: [...dialog.querySelectorAll('label')].filter(visible).map(label => ({
        text: clean(label.innerText),
        className: clean(label.className),
        checked: Boolean(label.querySelector('input')?.checked),
      })),
    });
    return {
      elementDialogs: [...document.querySelectorAll('.el-dialog')].filter(visible).map(describe),
      messageBoxes: [...document.querySelectorAll('.el-message-box__wrapper[role="dialog"]')]
        .filter(visible).map(describe),
    };
  })()`);
}

async function openMmsConfigDialog(client) {
  const opened = await client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const buttons = [...document.querySelectorAll('button')]
      .filter(visible).filter(button => clean(button.innerText) === '彩信上网配置');
    if (buttons.length !== 1) return { ok: false, reason: 'mms config button is not unique', count: buttons.length };
    if (buttons[0].disabled) return { ok: false, reason: 'mms config button is disabled' };
    buttons[0].click();
    return { ok: true };
  })()`);
  if (!opened.ok) throw new Error(`Open MMS config dialog failed: ${JSON.stringify(opened)}`);
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const ui = await readMmsUi(client);
    if (ui.elementDialogs.length) return ui;
    await sleep(250);
  }
  throw new Error('MMS config dialog did not appear');
}

async function chooseWholeDevice(client, selected) {
  const chosen = await client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const dialogs = [...document.querySelectorAll('.el-dialog')].filter(visible)
      .filter(dialog => clean(dialog.innerText).includes('彩信上网配置'));
    if (dialogs.length !== 1) return { ok: false, reason: 'mms config dialog is not unique', count: dialogs.length };
    const tabs = [...dialogs[0].querySelectorAll('[role="tab"]')]
      .filter(visible).filter(tab => clean(tab.innerText) === '按设备整机');
    if (tabs.length !== 1) return { ok: false, reason: 'whole-device tab is not unique', count: tabs.length };
    tabs[0].click();
    return { ok: true };
  })()`);
  if (!chosen.ok) throw new Error(`Choose whole-device MMS config failed: ${JSON.stringify(chosen)}`);
  const readyDeadline = Date.now() + 10000;
  while (Date.now() < readyDeadline) {
    const state = await client.evaluate(`(() => {
      const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
      const compact = value => clean(value).replace(/\\s+/g, '');
      const visible = element => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
      };
      const dialogs = [...document.querySelectorAll('.el-dialog')].filter(visible)
        .filter(dialog => clean(dialog.innerText).includes('彩信上网配置'));
      if (dialogs.length !== 1) return { ok: false, reason: 'mms config dialog is not unique', count: dialogs.length };
      const tabs = [...dialogs[0].querySelectorAll('[role="tab"]')]
        .filter(visible).filter(tab => clean(tab.innerText) === '按设备整机' && tab.getAttribute('aria-selected') === 'true');
      const panes = [...dialogs[0].querySelectorAll('[role="tabpanel"]')].filter(visible);
      const submits = [...dialogs[0].querySelectorAll('button')]
        .filter(visible).filter(button => compact(button.innerText) === '提交' && !button.disabled);
      if (tabs.length !== 1 || panes.length !== 1 || submits.length !== 1) {
        return {
          ok: false,
          pending: true,
          tabCount: tabs.length,
          paneCount: panes.length,
          submitCount: submits.length,
        };
      }
      const selected = ${JSON.stringify(selected)};
      const selectedSet = new Set(selected);
      const displayedNames = [...new Set(
        [...panes[0].querySelectorAll('*')]
          .filter(visible)
          .filter(element => element.children.length === 0)
          .map(element => clean(element.innerText))
          .filter(text => selectedSet.has(text))
      )];
      const rect = submits[0].getBoundingClientRect();
      return {
        ok: true,
        paneText: clean(panes[0].innerText),
        displayedNames,
        submitPoint: { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
      };
    })()`);
    if (state.ok) {
      if (!new RegExp(`已选设备\\s*${selected.length}\\s*台`).test(state.paneText)) {
        throw new Error(`MMS whole-device pane does not show the expected selected count: ${state.paneText}`);
      }
      if (JSON.stringify(state.displayedNames) !== JSON.stringify(selected)) {
        throw new Error(`MMS whole-device pane list mismatch: ${JSON.stringify({
          selected,
          displayedNames: state.displayedNames,
        })}`);
      }
      return state;
    }
    if (!state.pending) throw new Error(`MMS config submit failed: ${JSON.stringify(state)}`);
    await sleep(250);
  }
  throw new Error('MMS whole-device pane did not become ready');
}

async function submitWholeDevice(client, wholeDeviceSelection) {
  await trustedClick(client, wholeDeviceSelection.submitPoint, 'MMS whole-device submit');
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const ui = await readMmsUi(client);
    if (ui.messageBoxes.length) return ui;
    await sleep(250);
  }
  throw new Error('MMS final confirmation did not appear');
}

async function cancelMmsFinalConfirmation(client) {
  const cancelled = await client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const dialogs = [...document.querySelectorAll('.el-message-box__wrapper[role="dialog"]')].filter(visible);
    if (dialogs.length !== 1) return { ok: false, reason: 'mms final confirmation is not unique', count: dialogs.length };
    const buttons = [...dialogs[0].querySelectorAll('button')]
      .filter(visible).filter(button => clean(button.innerText) === '取消' && !button.disabled);
    if (buttons.length !== 1) return { ok: false, reason: 'mms final cancel button is not unique', count: buttons.length };
    buttons[0].click();
    return { ok: true };
  })()`);
  if (!cancelled.ok) throw new Error(`Cancel MMS final confirmation failed: ${JSON.stringify(cancelled)}`);
}

function validateMmsFinalConfirmation(finalConfirmation, selected) {
  if (finalConfirmation.messageBoxes.length !== 1) {
    throw new Error(`MMS final confirmation is not unique: ${JSON.stringify(finalConfirmation)}`);
  }
  const dialog = finalConfirmation.messageBoxes[0];
  const requiredPatterns = [
    ['彩信上网配置', /彩信上网配置/],
    ['按设备整机', /整机/],
    ['QICSGP', /QICSGP/i],
    ['cfun', /cfun/i],
    ['本模式不含 reset', /本模式不含\s*reset/i],
  ];
  const missing = requiredPatterns.filter(([, pattern]) => !pattern.test(dialog.text)).map(([label]) => label);
  if (missing.length) throw new Error(`MMS confirmation is missing required phrases: ${missing.join(',')}`);

  const countMatch = dialog.text.match(/所选\s*(\d+)\s*台设备/);
  if (!countMatch || Number(countMatch[1]) !== selected.length) {
    throw new Error(`MMS confirmation selected count mismatch: ${JSON.stringify({
      expected: selected.length,
      confirmationText: dialog.text,
    })}`);
  }
  const confirms = dialog.buttons.filter(button => button.text === '确定' && !button.disabled);
  const cancels = dialog.buttons.filter(button => button.text === '取消' && !button.disabled);
  if (confirms.length !== 1 || cancels.length !== 1) throw new Error('MMS confirmation buttons are not unique');
  return { confirmationText: dialog.text, confirmedDeviceCount: Number(countMatch[1]) };
}

async function clickMmsFinalConfirm(client) {
  const clicked = await client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const dialogs = [...document.querySelectorAll('.el-message-box__wrapper[role="dialog"]')].filter(visible);
    if (dialogs.length !== 1) return { ok: false, reason: 'mms final confirmation is not unique', count: dialogs.length };
    const buttons = [...dialogs[0].querySelectorAll('button')]
      .filter(visible).filter(button => clean(button.innerText) === '确定' && !button.disabled);
    if (buttons.length !== 1) return { ok: false, reason: 'mms final confirm button is not unique', count: buttons.length };
    buttons[0].click();
    return { ok: true };
  })()`);
  if (!clicked.ok) throw new Error(`MMS final confirmation click failed: ${JSON.stringify(clicked)}`);
}

async function collectMmsResult(client, recorder, responseStart) {
  const pageMessages = new Set();
  const deadline = Date.now() + 20000;
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
        messages: [...document.querySelectorAll('.el-message, .el-notification')]
          .filter(visible).map(element => clean(element.innerText)),
      };
    })()`);
    for (const message of state.messages) if (message) pageMessages.add(message);
    if (!state.dialogCount && pageMessages.size) break;
    await sleep(250);
  }
  await sleep(1500);
  const mutationResponses = recorder.responses.slice(responseStart)
    .filter(response => response.url.includes('/api/') && response.method && response.method !== 'GET');
  for (const response of mutationResponses) {
    response.bodySample = await client.send('Network.getResponseBody', { requestId: response.requestId })
      .then(result => String(result.body || '').slice(0, 2000))
      .catch(error => `[unavailable: ${error.message}]`);
  }
  const messages = [...pageMessages];
  const hasPositiveMessage = messages.some(message => (
    /彩信.*(成功|提交|下发|发送)|配置.*(成功|提交|下发)|任务已提交|操作成功/.test(message)
  ));
  const hasNegativeMessage = messages.some(message => /失败|错误|异常/.test(message));
  const networkAccepted = mutationResponses.length === 1 && mutationResponses[0].status === 200;
  return {
    mutationResponses,
    pageMessages: messages,
    hasPositiveMessage,
    hasNegativeMessage,
    networkAccepted,
    success: networkAccepted && hasPositiveMessage && !hasNegativeMessage,
  };
}

async function readStage3Prerequisite({ forced = false, allowRepeatSuccess = false } = {}) {
  let previous = await readJsonIfExists(LAST_RUN_PATH);
  if (!previous || previous.dateBeijing !== beijingDate()) {
    throw new Error('Stage 2 successful receipt for today is missing');
  }
  let priorTerminalReceiptPath = null;
  let repeatAfterSuccess = false;
  if (previous.stage3?.success) {
    if (!allowRepeatSuccess) {
      throw new Error(`Stage 3 already reached a terminal state today: ${previous.receiptPath || LAST_RUN_PATH}`);
    }
    priorTerminalReceiptPath = previous.receiptPath || LAST_RUN_PATH;
    repeatAfterSuccess = true;
    previous = await readJsonIfExists(previous.stage2ReceiptPath);
    if (!previous || previous.dateBeijing !== beijingDate()) {
      throw new Error('Stage 2 receipt referenced by the successful Stage 3 attempt is missing');
    }
  } else if (previous.stage3?.blocked) {
    if (!forced) {
      throw new Error(`Stage 3 already reached a terminal state today: ${previous.receiptPath || LAST_RUN_PATH}`);
    }
    if (previous.stage3.submissionMayHaveOccurred !== false) {
      throw new Error('Forced Stage 3 retry is forbidden because the prior attempt may have submitted');
    }
    priorTerminalReceiptPath = previous.receiptPath || LAST_RUN_PATH;
    previous = await readJsonIfExists(previous.stage2ReceiptPath);
    if (!previous || previous.dateBeijing !== beijingDate()) {
      throw new Error('Stage 2 receipt referenced by the blocked Stage 3 attempt is missing');
    }
  }
  if (!previous.stage2?.success || !previous.stage2?.lastBatchSubmittedAt) {
    throw new Error('Stage 2 successful receipt for today is incomplete');
  }
  const dueAt = new Date(previous.stage2.lastBatchSubmittedAt).getTime() + 50 * 60 * 1000;
  if (Date.now() < dueAt) throw new Error(`Stage 3 is not due until ${new Date(dueAt).toISOString()}`);
  return { previous, dueAt, forced, priorTerminalReceiptPath, repeatAfterSuccess };
}

async function clearMmsUiByReload(client) {
  await client.send('Page.reload', { ignoreCache: false }, 60000);
  await waitForReady(client);
  await sleep(1500);
  const inspection = await inspect(client);
  return {
    href: inspection.href,
    checkedNames: inspection.checkedNames,
    dialogs: inspection.dialogs,
    clean: inspection.checkedNames.length === 0 && inspection.dialogs.length === 0,
  };
}

async function clearRestartUiByReload(client) {
  await client.send('Page.reload', { ignoreCache: false }, 60000);
  await waitForReady(client);
  await sleep(1500);
  const pageSize = await setPageSize200(client);
  await sleep(500);
  const inspection = await inspect(client);
  const ui = await readMmsUi(client);
  return {
    href: inspection.href,
    pageSize: pageSize.pageSize,
    totalDeviceCount: pageSize.total,
    rowCount: inspection.rowCount,
    checkedNames: inspection.checkedNames,
    checkedCount: inspection.checkedNames.length,
    elementDialogs: ui.elementDialogs,
    messageBoxes: ui.messageBoxes,
    clean: pageSize.pageSize === '200条/页'
      && inspection.rowCount === pageSize.total
      && inspection.checkedNames.length === 0
      && ui.elementDialogs.length === 0
      && ui.messageBoxes.length === 0,
  };
}

async function executeStage3(client, context, inspection, prerequisite) {
  const receiptPath = path.join(SNAPSHOT_DIR, `opensms_device_followup_stage3_${receiptStamp()}.json`);
  const receipt = {
    stage: '阶段 3：设备监控彩信上网配置',
    dateBeijing: beijingDate(),
    profileId: PROFILE_ID,
    workbenchTitle: context.workbenchTitle,
    cdpPort: context.port,
    url: TARGET_URL,
    pageSize: 200,
    totalDeviceCount: inspection.deviceNames.length,
    candidateCount: inspection.deviceNames.length,
    backendValidCount: inspection.deviceNames.length,
    actualWriteDeviceCount: 0,
    successCount: 0,
    failureCount: 0,
    specialDevices201To241Complete: true,
    batchCount: 1,
    batchSizes: [inspection.deviceNames.length],
    receiptPath,
    stage2ReceiptPath: prerequisite.previous.receiptPath,
    stage2LastBatchSubmittedAt: prerequisite.previous.stage2.lastBatchSubmittedAt,
    stage2LastBatchSubmittedAtBeijing: prerequisite.previous.stage2.lastBatchSubmittedAtBeijing,
    forced: prerequisite.forced,
    priorTerminalReceiptPath: prerequisite.priorTerminalReceiptPath,
    repeatAfterSuccess: prerequisite.repeatAfterSuccess,
    stage3: {
      success: false,
      blocked: false,
      submissionMayHaveOccurred: false,
    },
    startedAt: new Date().toISOString(),
    startedAtBeijing: beijingTimestamp(),
  };
  const persist = async () => {
    await writeJson(receiptPath, receipt);
    await writeJson(LAST_RUN_PATH, receipt);
  };
  await persist();

  const selected = await selectExact(client, inspection.deviceNames);
  receipt.stage3.selectedNames = selected;
  receipt.stage3.selectedCount = selected.length;
  await persist();
  receipt.stage3.configDialog = await openMmsConfigDialog(client);
  const wholeDeviceSelection = await chooseWholeDevice(client, selected);
  receipt.stage3.wholeDeviceSelection = {
    paneText: wholeDeviceSelection.paneText,
    displayedNames: wholeDeviceSelection.displayedNames,
    selectedCountMatched: true,
    completeDeviceListMatched: true,
  };
  await persist();
  const finalConfirmation = await submitWholeDevice(client, wholeDeviceSelection);
  receipt.stage3.finalConfirmation = finalConfirmation;
  await persist();

  let validation;
  try {
    validation = validateMmsFinalConfirmation(finalConfirmation, selected);
  } catch (error) {
    receipt.stage3.blocked = true;
    receipt.stage3.blockReason = error.message;
    receipt.stage3.submissionMayHaveOccurred = false;
    receipt.blocked = true;
    receipt.success = false;
    try {
      await cancelMmsFinalConfirmation(client);
      receipt.stage3.cancelled = true;
    } catch (cancelError) {
      receipt.stage3.cancelled = false;
      receipt.stage3.cancelError = cancelError.message;
    }
    receipt.stage3.cleanup = await clearMmsUiByReload(client);
    receipt.completedAt = new Date().toISOString();
    receipt.completedAtBeijing = beijingTimestamp();
    await persist();
    return receipt;
  }

  receipt.stage3.confirmationMatched = true;
  receipt.stage3.confirmationText = validation.confirmationText;
  receipt.stage3.confirmedDeviceCount = validation.confirmedDeviceCount;
  await persist();
  await client.send('Network.enable');
  const recorder = createNetworkRecorder(client);
  const responseStart = recorder.responses.length;
  const submittedAt = new Date();
  receipt.stage3.submissionMayHaveOccurred = true;
  receipt.actualWriteDeviceCount = selected.length;
  await persist();
  await clickMmsFinalConfirm(client);
  const result = await collectMmsResult(client, recorder, responseStart);
  Object.assign(receipt.stage3, {
    submittedAt: submittedAt.toISOString(),
    submittedAtBeijing: beijingTimestamp(submittedAt),
    ...result,
  });
  if (!result.success) {
    receipt.stage3.uncertain = true;
    receipt.error = 'Stage 3 did not produce an unambiguous success receipt';
    await persist();
    throw new Error(receipt.error);
  }
  receipt.stage3.cleanup = await clearMmsUiByReload(client);
  if (!receipt.stage3.cleanup.clean) {
    receipt.stage3.uncertain = true;
    receipt.error = 'Stage 3 independent cleanup verification did not reach a clean state';
    receipt.failureCount = inspection.deviceNames.length;
    await persist();
    throw new Error(receipt.error);
  }
  receipt.stage3.commandRecordVerification = await verifyLatestAtCommand(client, {
    action: 'mms',
    submittedAt: receipt.stage3.submittedAt,
    selectedDeviceNames: selected,
  });
  if (!receipt.stage3.commandRecordVerification.success) {
    receipt.stage3.uncertain = true;
    receipt.error = 'Command record first page did not confirm a fresh executed AT MMS command';
    receipt.failureCount = inspection.deviceNames.length;
    await persist();
    throw new Error(receipt.error);
  }
  receipt.stage3.success = true;
  receipt.stage3.blocked = false;
  receipt.stage3.uncertain = false;
  receipt.success = true;
  receipt.successCount = inspection.deviceNames.length;
  receipt.failureCount = 0;
  receipt.completedAt = new Date().toISOString();
  receipt.completedAtBeijing = beijingTimestamp();
  await persist();
  return receipt;
}

function makeStage2Batches(deviceNames) {
  return [{ label: '全部设备', targetNames: [...deviceNames] }];
}

function readCustomRestartPlan() {
  const raw = String(process.env.OPENSMS_CUSTOM_RESTART_DEVICES || '').trim();
  if (!raw) throw new Error('OPENSMS_CUSTOM_RESTART_DEVICES is required for restart-custom');
  const rawInputNames = raw.split(/[\s,;、，；]+/).map(value => value.trim()).filter(Boolean);
  if (!rawInputNames.length) throw new Error('Custom restart device list is empty');
  const invalidNames = rawInputNames.filter(name => !/^[A-Za-z0-9_-]+$/.test(name));
  if (invalidNames.length) throw new Error(`Custom restart device names are invalid: ${invalidNames.join(',')}`);

  const seen = new Set();
  const targetNames = [];
  const duplicateNames = [];
  for (const name of rawInputNames) {
    if (seen.has(name)) {
      if (!duplicateNames.includes(name)) duplicateNames.push(name);
      continue;
    }
    seen.add(name);
    targetNames.push(name);
  }

  const batchSizeText = String(process.env.OPENSMS_CUSTOM_RESTART_BATCH_SIZE || '10').trim();
  const batchSize = Number(batchSizeText);
  if (!Number.isInteger(batchSize) || batchSize < 1 || batchSize > 50) {
    throw new Error(`OPENSMS_CUSTOM_RESTART_BATCH_SIZE must be an integer from 1 to 50: ${batchSizeText}`);
  }
  const intervalSecondsText = String(process.env.OPENSMS_CUSTOM_RESTART_INTERVAL_SECONDS || '120').trim();
  const intervalSeconds = Number(intervalSecondsText);
  if (!Number.isInteger(intervalSeconds) || intervalSeconds < 120 || intervalSeconds > 3600) {
    throw new Error(`OPENSMS_CUSTOM_RESTART_INTERVAL_SECONDS must be an integer from 120 to 3600: ${intervalSecondsText}`);
  }
  const batches = [];
  for (let index = 0; index < targetNames.length; index += batchSize) {
    batches.push({
      label: `自定义批次${batches.length + 1}`,
      targetNames: targetNames.slice(index, index + batchSize),
    });
  }
  return {
    rawInputNames,
    targetNames,
    duplicateNames,
    batchSize,
    intervalSeconds,
    intervalMs: intervalSeconds * 1000,
    batches,
  };
}

async function executeCustomRestart(client, context, inspection, plan) {
  const missingNames = plan.targetNames.filter(name => !inspection.deviceNames.includes(name));
  if (missingNames.length) throw new Error(`Custom restart devices are missing from the live page: ${missingNames.join(',')}`);

  const previous = await readJsonIfExists(CUSTOM_RESTART_LAST_RUN_PATH);
  const sameTargets = previous?.dateBeijing === beijingDate()
    && JSON.stringify(previous.targetNames || []) === JSON.stringify(plan.targetNames);
  if (sameTargets && previous.success) {
    throw new Error(`The same custom restart plan already completed today: ${previous.receiptPath}`);
  }
  if (sameTargets && (
    previous.restart?.submissionMayHaveOccurred
    || previous.restart?.currentBatch?.submissionMayHaveOccurred
    || (previous.restart?.completedBatches || []).length
  )) {
    throw new Error(`The same custom restart plan has a prior submitted or uncertain result: ${previous.receiptPath || CUSTOM_RESTART_LAST_RUN_PATH}`);
  }

  const receiptPath = path.join(SNAPSHOT_DIR, `opensms_device_custom_restart_${receiptStamp()}.json`);
  const receipt = {
    stage: '自定义设备分批重启',
    dateBeijing: beijingDate(),
    profileId: PROFILE_ID,
    workbenchTitle: context.workbenchTitle,
    cdpPort: context.port,
    url: TARGET_URL,
    pageSize: 200,
    totalDeviceCount: inspection.deviceNames.length,
    specialDevices201To241Complete: true,
    rawInputCount: plan.rawInputNames.length,
    rawInputNames: plan.rawInputNames,
    uniqueTargetCount: plan.targetNames.length,
    targetNames: plan.targetNames,
    duplicateNames: plan.duplicateNames,
    batchSize: plan.batchSize,
    batchIntervalSeconds: plan.intervalSeconds,
    batchCount: plan.batches.length,
    batchSizes: plan.batches.map(batch => batch.targetNames.length),
    receiptPath,
    startedAt: new Date().toISOString(),
    startedAtBeijing: beijingTimestamp(),
    success: false,
    restart: {
      success: false,
      submissionMayHaveOccurred: false,
      completedBatches: [],
      currentBatch: null,
    },
  };
  const persist = async () => {
    await writeJson(receiptPath, receipt);
    await writeJson(CUSTOM_RESTART_LAST_RUN_PATH, receipt);
  };
  await persist();

  await client.send('Network.enable');
  const recorder = createNetworkRecorder(client);
  for (let index = 0; index < plan.batches.length; index += 1) {
    const batch = plan.batches[index];
    const previousSuccess = receipt.restart.completedBatches.at(-1);
    if (previousSuccess) {
      const waitMs = new Date(previousSuccess.submittedAt).getTime() + plan.intervalMs - Date.now();
      if (waitMs > 0) await sleep(waitMs);
    }

    receipt.restart.currentBatch = {
      index: index + 1,
      label: batch.label,
      targetNames: batch.targetNames,
      attemptStartedAt: new Date().toISOString(),
      attemptStartedAtBeijing: beijingTimestamp(),
      submissionMayHaveOccurred: false,
    };
    await persist();

    await setPageSize200(client);
    const current = await inspect(client);
    validateNames(current.deviceNames);
    if (JSON.stringify([...current.deviceNames].sort()) !== JSON.stringify([...inspection.deviceNames].sort())) {
      throw new Error(`Current-page device list changed before custom batch ${index + 1}`);
    }
    const selected = await selectExact(client, batch.targetNames);
    const confirmation = await openRestartConfirmation(client);
    const validation = validateRestartConfirmation(confirmation, selected);
    Object.assign(receipt.restart.currentBatch, {
      selectedNames: selected,
      selectedCount: selected.length,
      confirmationText: validation.confirmationText,
      confirmationNames: validation.modalNames,
      confirmationMatched: true,
      finalConfirmClicked: false,
    });
    await persist();

    receipt.restart.currentBatch.submissionMayHaveOccurred = true;
    receipt.restart.submissionMayHaveOccurred = true;
    await persist();
    const submittedAt = new Date();
    const responseStart = recorder.responses.length;
    await clickRestartConfirm(client);
    receipt.restart.currentBatch.finalConfirmClicked = true;
    await persist();
    const result = await collectRestartResult(client, recorder, responseStart);
    Object.assign(receipt.restart.currentBatch, {
      submittedAt: submittedAt.toISOString(),
      submittedAtBeijing: beijingTimestamp(submittedAt),
      ...result,
    });
    if (!result.success) {
      receipt.restart.uncertain = true;
      receipt.restart.error = `Custom batch ${index + 1} did not produce an unambiguous success receipt`;
      await persist();
      throw new Error(receipt.restart.error);
    }
    receipt.restart.completedBatches.push(receipt.restart.currentBatch);
    receipt.restart.currentBatch = null;
    await persist();
    await selectExact(client, []);
  }

  const completedAt = new Date();
  receipt.restart.success = true;
  receipt.restart.uncertain = false;
  receipt.restart.completedAt = completedAt.toISOString();
  receipt.restart.completedAtBeijing = beijingTimestamp(completedAt);
  receipt.restart.lastBatchSubmittedAt = receipt.restart.completedBatches.at(-1).submittedAt;
  receipt.restart.lastBatchSubmittedAtBeijing = receipt.restart.completedBatches.at(-1).submittedAtBeijing;
  receipt.restart.cleanup = await clearRestartUiByReload(client);
  if (!receipt.restart.cleanup.clean) {
    receipt.restart.uncertain = true;
    receipt.restart.error = 'Independent cleanup verification did not reach a clean state';
    await persist();
    throw new Error(receipt.restart.error);
  }
  receipt.success = true;
  receipt.completedAt = completedAt.toISOString();
  receipt.completedAtBeijing = beijingTimestamp(completedAt);
  await persist();
  return receipt;
}

async function executeStage2(client, context, inspection, { forced = false } = {}) {
  const stage1 = await readJsonIfExists(STAGE1_PATH);
  if (!forced) {
    if (!stage1?.success || stage1.dateBeijing !== beijingDate() || !stage1.submittedAt) {
      throw new Error('Stage 1 successful receipt for today is missing');
    }
    const dueAt = new Date(stage1.submittedAt).getTime() + 40 * 60 * 1000;
    if (Date.now() < dueAt) throw new Error(`Stage 2 is not due until ${new Date(dueAt).toISOString()}`);
  }

  const previous = await readJsonIfExists(LAST_RUN_PATH);
  if (previous?.dateBeijing === beijingDate()) {
    if (previous.stage2?.success) throw new Error(`Stage 2 already completed today: ${previous.receiptPath}`);
    if (previous.stage2?.submissionMayHaveOccurred || previous.stage2?.currentBatch?.submissionMayHaveOccurred) {
      throw new Error(`Stage 2 previous result is uncertain and must not be retried: ${previous.receiptPath || LAST_RUN_PATH}`);
    }
    if ((previous.stage2?.completedBatches || []).length) {
      throw new Error(`Stage 2 has partial successful batches; automatic reconstruction is disabled: ${previous.receiptPath || LAST_RUN_PATH}`);
    }
  }

  const batches = makeStage2Batches(inspection.deviceNames);
  if (!batches[0].targetNames.length) throw new Error('All-device restart selection is empty');
  const receiptPath = path.join(SNAPSHOT_DIR, `opensms_device_followup_stage2_${receiptStamp()}.json`);
  const receipt = {
    stage: '阶段 2：设备监控全选一次重启',
    dateBeijing: beijingDate(),
    profileId: PROFILE_ID,
    workbenchTitle: context.workbenchTitle,
    cdpPort: context.port,
    url: TARGET_URL,
    pageSize: 200,
    totalDeviceCount: inspection.deviceNames.length,
    candidateCount: inspection.deviceNames.length,
    backendValidCount: inspection.deviceNames.length,
    actualWriteDeviceCount: 0,
    successCount: 0,
    failureCount: 0,
    specialDevices201To241Complete: true,
    batchCount: batches.length,
    batchSizes: batches.map(batch => batch.targetNames.length),
    receiptPath,
    startedAt: new Date().toISOString(),
    startedAtBeijing: beijingTimestamp(),
    forced,
    stage1SubmittedAt: stage1?.dateBeijing === beijingDate() ? stage1.submittedAt : null,
    stage1SubmittedAtBeijing: stage1?.dateBeijing === beijingDate() ? stage1.submittedAtBeijing : null,
    stage2: {
      success: false,
      submissionMayHaveOccurred: false,
      completedBatches: [],
      currentBatch: null,
    },
  };
  const persist = async () => {
    await writeJson(receiptPath, receipt);
    await writeJson(LAST_RUN_PATH, receipt);
  };
  await persist();

  await client.send('Network.enable');
  const recorder = createNetworkRecorder(client);
  for (let index = 0; index < batches.length; index += 1) {
    const batch = batches[index];
    receipt.stage2.currentBatch = {
      index: index + 1,
      label: batch.label,
      targetNames: batch.targetNames,
      attemptStartedAt: new Date().toISOString(),
      attemptStartedAtBeijing: beijingTimestamp(),
      submissionMayHaveOccurred: false,
    };
    await persist();

    await setPageSize200(client);
    const current = await inspect(client);
    validateNames(current.deviceNames);
    if (JSON.stringify([...current.deviceNames].sort()) !== JSON.stringify([...inspection.deviceNames].sort())) {
      throw new Error(`Current-page device list changed before batch ${index + 1}`);
    }
    const selected = await selectExact(client, batch.targetNames);
    const confirmation = await openRestartConfirmation(client);
    const validation = validateRestartConfirmation(confirmation, selected);
    Object.assign(receipt.stage2.currentBatch, {
      selectedNames: selected,
      selectedCount: selected.length,
      confirmationText: validation.confirmationText,
      confirmationMatched: true,
    });
    await persist();

    receipt.stage2.currentBatch.submissionMayHaveOccurred = true;
    receipt.stage2.submissionMayHaveOccurred = true;
    await persist();
    const submittedAt = new Date();
    const responseStart = recorder.responses.length;
    await clickRestartConfirm(client);
    receipt.stage2.currentBatch.finalConfirmClicked = true;
    receipt.actualWriteDeviceCount = selected.length;
    await persist();
    const result = await collectRestartResult(client, recorder, responseStart);
    Object.assign(receipt.stage2.currentBatch, {
      submittedAt: submittedAt.toISOString(),
      submittedAtBeijing: beijingTimestamp(submittedAt),
      ...result,
    });
    if (!result.success) {
      receipt.stage2.uncertain = true;
      receipt.stage2.error = `Batch ${index + 1} did not produce an unambiguous success receipt`;
      await persist();
      throw new Error(receipt.stage2.error);
    }
    receipt.stage2.completedBatches.push(receipt.stage2.currentBatch);
    receipt.stage2.currentBatch = null;
    await persist();
    await selectExact(client, []);
  }

  receipt.stage2.cleanup = await clearRestartUiByReload(client);
  if (!receipt.stage2.cleanup.clean) {
    receipt.stage2.uncertain = true;
    receipt.stage2.error = 'Independent cleanup verification did not reach a clean state';
    receipt.failureCount = inspection.deviceNames.length;
    await persist();
    throw new Error(receipt.stage2.error);
  }
  receipt.stage2.commandRecordVerification = await verifyLatestAtCommand(client, {
    action: 'restart',
    submittedAt: receipt.stage2.completedBatches[0].submittedAt,
    selectedDeviceNames: inspection.deviceNames,
  });
  if (!receipt.stage2.commandRecordVerification.success) {
    receipt.stage2.uncertain = true;
    receipt.stage2.error = 'Command record first page did not confirm a fresh executed AT restart command';
    receipt.failureCount = inspection.deviceNames.length;
    await persist();
    throw new Error(receipt.stage2.error);
  }
  const completedAt = new Date();
  receipt.stage2.success = true;
  receipt.stage2.uncertain = false;
  receipt.stage2.completedAt = completedAt.toISOString();
  receipt.stage2.completedAtBeijing = beijingTimestamp(completedAt);
  receipt.stage2.lastBatchSubmittedAt = receipt.stage2.completedBatches.at(-1).submittedAt;
  receipt.stage2.lastBatchSubmittedAtBeijing = receipt.stage2.completedBatches.at(-1).submittedAtBeijing;
  receipt.successCount = inspection.deviceNames.length;
  receipt.failureCount = 0;
  receipt.success = true;
  receipt.completedAt = completedAt.toISOString();
  await persist();
  return receipt;
}

async function mainWithSharedBrowser(shared, background) {
  if (!['probe', 'cleanup-reload', 'preflight-restart', 'preflight-mms', 'restart-custom', 'stage2', 'stage2-force', 'stage3', 'stage3-force', 'stage3-force-repeat'].includes(MODE)) {
    throw new Error(`Unsupported mode: ${MODE}`);
  }
  await fs.mkdir(SNAPSHOT_DIR, { recursive: true });
  const customRestartPlan = MODE === 'restart-custom' ? readCustomRestartPlan() : null;
  const stage3Prerequisite = MODE === 'stage3' || MODE === 'stage3-force' || MODE === 'stage3-force-repeat'
    ? await readStage3Prerequisite({
      forced: MODE === 'stage3-force' || MODE === 'stage3-force-repeat',
      allowRepeatSuccess: MODE === 'stage3-force-repeat',
    })
    : null;
  let lockHandle = null;
  if (MODE === 'restart-custom' || MODE === 'stage2' || MODE === 'stage2-force' || MODE === 'stage3' || MODE === 'stage3-force' || MODE === 'stage3-force-repeat') {
    lockHandle = await fs.open(LOCK_PATH, 'wx').catch(error => {
      if (error.code === 'EEXIST') throw new Error(`Follow-up stage is already running: ${LOCK_PATH}`);
      throw error;
    });
    await lockHandle.writeFile(`${JSON.stringify({ pid: process.pid, mode: MODE, startedAt: new Date().toISOString() })}\n`);
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
    const loginState = await client.evaluate(`(() => ({
      href: location.href,
      body: String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 2000),
    }))()`);
    if (/登录状态已过期|重新登录/.test(loginState.body) || /\/login(?:$|[?#])/.test(loginState.href)) {
      throw new Error(`LOGIN_EXPIRED: ${JSON.stringify(loginState)}`);
    }
    if (MODE === 'cleanup-reload') {
      await client.send('Page.reload', { ignoreCache: false }, 60000);
      await waitForReady(client);
      await sleep(1500);
    }
    const pageSize = await setPageSize200(client);
    await sleep(1500);
    const inspection = await inspect(client);
    validateNames(inspection.deviceNames);
    let preflight = null;
    if (MODE === 'preflight-restart') {
      const special = new Set(Array.from({ length: 41 }, (_, index) => String(201 + index)));
      const ordinary = inspection.deviceNames.filter(name => !special.has(name));
      const selected = await selectExact(client, ordinary);
      const confirmation = await openRestartConfirmation(client);
      preflight = { ordinary, selected, confirmation };
      await cancelRestartConfirmation(client);
      await sleep(500);
      await selectExact(client, []);
    }
    if (MODE === 'preflight-mms') {
      await client.send('Page.reload', { ignoreCache: false }, 60000);
      await waitForReady(client);
      await sleep(1500);
      await setPageSize200(client);
      const freshInspection = await inspect(client);
      validateNames(freshInspection.deviceNames);
      const selected = await selectExact(client, freshInspection.deviceNames);
      const configDialog = await openMmsConfigDialog(client);
      const wholeDeviceSelection = await chooseWholeDevice(client, selected);
      const finalConfirmation = await submitWholeDevice(client, wholeDeviceSelection);
      preflight = { selected, configDialog, wholeDeviceSelection, finalConfirmation };
      await cancelMmsFinalConfirmation(client);
      await client.send('Page.reload', { ignoreCache: false }, 60000);
      await waitForReady(client);
      await sleep(1500);
    }
    let executionReceipt = null;
    if (MODE === 'restart-custom') {
      executionReceipt = await executeCustomRestart(
        client,
        { port, workbenchTitle: workbench[0].title },
        inspection,
        customRestartPlan,
      );
    } else if (MODE === 'stage2' || MODE === 'stage2-force') {
      executionReceipt = await executeStage2(
        client,
        { port, workbenchTitle: workbench[0].title },
        inspection,
        { forced: MODE === 'stage2-force' },
      );
    } else if (MODE === 'stage3' || MODE === 'stage3-force' || MODE === 'stage3-force-repeat') {
      executionReceipt = await executeStage3(
        client,
        { port, workbenchTitle: workbench[0].title },
        inspection,
        stage3Prerequisite,
      );
    }
    const mmsUi = await readMmsUi(client);
    console.log(JSON.stringify({
      mode: MODE,
      port,
      workbenchTitle: workbench[0].title,
      pageSize,
      browser: 'cloakbrowser',
      browserSessionId: shared.session.sessionId,
      sharedLogin: true,
      inspection: { href: inspection.href, rowCount: inspection.rowCount, checkedNames: inspection.checkedNames },
      preflight,
      mmsUi,
      executionReceipt,
    }, null, 2));
  } finally {
    client.close();
    if (lockHandle) {
      await lockHandle.close().catch(() => {});
      await fs.unlink(LOCK_PATH).catch(() => {});
    }
  }
}

async function main() {
  if (!['probe', 'cleanup-reload', 'preflight-restart', 'preflight-mms', 'restart-custom', 'stage2', 'stage2-force', 'stage3', 'stage3-force', 'stage3-force-repeat'].includes(MODE)) throw new Error(`Unsupported mode: ${MODE}`);
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
