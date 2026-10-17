import fs from 'node:fs/promises';

const PROFILE_ID = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const WORKBENCH_TITLE = '430-_20_20_22-工作台';
const ORIGIN = 'https://adminsms.smsoug.com';
const TARGET_URL = `${ORIGIN}/machineconfig`;
const PORT_FILE = `/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/${PROFILE_ID}/DevToolsActivePort`;

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

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
          for (const handler of this.handlers.get(message.method) || []) handler(message.params || {});
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

async function trustedClick(client, point, label) {
  if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) {
    throw new Error(`${label} click point is invalid: ${JSON.stringify(point)}`);
  }
  await client.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1,
  });
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1,
  });
}

async function waitForReady(client, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const state = await client.evaluate('({href: location.href, readyState: document.readyState})');
    if (state.href.startsWith(TARGET_URL) && state.readyState === 'complete') return;
    await sleep(500);
  }
  throw new Error('Device page did not finish loading');
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
    return { ok: true, already: false, point: { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 } };
  })()`);
  if (!opened.ok) throw new Error(JSON.stringify(opened));
  if (!opened.already) {
    await trustedClick(client, opened.point, 'page-size input');
    const deadline = Date.now() + 5000;
    let option = null;
    while (Date.now() < deadline && !option) {
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
      if (!fallback.ok) throw new Error(`200条/页 option is not unique: ${JSON.stringify(fallback)}`);
    }
  }
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const state = await inspect(client);
    if (state.pageSize === '200条/页' && state.total > 0 && state.rowCount === state.total) return state;
    await sleep(500);
  }
  throw new Error('Page did not settle at 200条/页 with all devices visible');
}

async function inspect(client) {
  return client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const body = clean(document.body?.innerText);
    const totalMatch = body.match(/共\\s*(\\d+)\\s*条/);
    const wrappers = [...document.querySelectorAll('.el-table__body-wrapper')];
    const rows = wrappers.length === 1 ? [...wrappers[0].querySelectorAll('tbody tr')] : [];
    const deviceNames = rows.map(row => clean(row.querySelectorAll('td')[1]?.innerText));
    const checkedNames = rows.filter(row => row.querySelector('input[type="checkbox"]')?.checked)
      .map(row => clean(row.querySelectorAll('td')[1]?.innerText));
    const headerInputs = [...document.querySelectorAll('.el-table__header-wrapper input[type="checkbox"]')];
    return {
      href: location.href,
      title: document.title,
      loginExpired: /登录状态已过期|重新登录/.test(body) || /\\/login(?:$|[?#])/.test(location.href),
      pageSize: [...document.querySelectorAll('input.el-input__inner')]
        .map(element => element.value).find(value => /条\\/页$/.test(value)) || '',
      total: totalMatch ? Number(totalMatch[1]) : null,
      wrapperCount: wrappers.length,
      rowCount: rows.length,
      deviceNames,
      checkedNames,
      headerCheckboxCount: headerInputs.length,
      headerChecked: headerInputs.length === 1 ? headerInputs[0].checked : null,
      visibleDialogs: [...document.querySelectorAll('[role="dialog"], .el-dialog, .el-message-box__wrapper')]
        .filter(visible).map(dialog => clean(dialog.innerText)),
    };
  })()`);
}

async function selectAll(client, expectedNames) {
  const result = await client.evaluate(`(() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const expected = ${JSON.stringify(expectedNames)};
    const wrappers = [...document.querySelectorAll('.el-table__body-wrapper')];
    if (wrappers.length !== 1) return { ok: false, reason: 'main table is not unique', count: wrappers.length };
    const rows = [...wrappers[0].querySelectorAll('tbody tr')];
    const actual = rows.map(row => clean(row.querySelectorAll('td')[1]?.innerText));
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      return { ok: false, reason: 'device list changed before selection', actual };
    }
    for (const row of rows) {
      const input = row.querySelector('input[type="checkbox"]');
      const clickTarget = row.querySelector('.el-checkbox__inner');
      if (!input || !clickTarget) return { ok: false, reason: 'row checkbox is missing' };
      if (!input.checked) clickTarget.click();
    }
    return { ok: true };
  })()`);
  if (!result.ok) throw new Error(`Select all failed: ${JSON.stringify(result)}`);
  const expected = [...expectedNames].sort();
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const state = await inspect(client);
    if (JSON.stringify([...state.checkedNames].sort()) === JSON.stringify(expected) && state.headerChecked) return state;
    await sleep(250);
  }
  throw new Error(`Selection did not settle exactly: ${JSON.stringify(await inspect(client))}`);
}

async function main() {
  const [portLine] = (await fs.readFile(PORT_FILE, 'utf8')).trim().split(/\r?\n/);
  const port = Number(portLine);
  if (!Number.isInteger(port) || port < 1) throw new Error('BitBrowser 430 CDP port is invalid');
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const workbenches = targets.filter(target => target.type === 'page' && target.title === WORKBENCH_TITLE);
  if (workbenches.length !== 1 || !workbenches[0].url.includes(`id=${PROFILE_ID}`)) {
    throw new Error(`BitBrowser 430 workbench identity mismatch: ${JSON.stringify(workbenches)}`);
  }
  const pages = targets.filter(target => {
    if (target.type !== 'page') return false;
    try { return new URL(target.url).origin === ORIGIN; } catch { return false; }
  });
  if (pages.length !== 1) throw new Error(`OpenSMS page is not unique in BitBrowser 430: ${pages.length}`);
  const client = new CdpClient(pages[0].webSocketDebuggerUrl);
  const commandRequests = [];
  try {
    await client.connect();
    await client.send('Page.enable');
    await client.send('Runtime.enable');
    await client.send('Network.enable');
    client.on('Network.requestWillBeSent', params => {
      const request = params.request || {};
      if (/\/api\/command\//.test(request.url || '')) {
        commandRequests.push({ method: request.method, url: request.url, postData: request.postData || '' });
      }
    });
    if (!pages[0].url.startsWith(TARGET_URL)) await client.send('Page.navigate', { url: TARGET_URL }, 60000);
    await waitForReady(client);
    await sleep(2000);
    const prepared = await setPageSize200(client);
    if (prepared.loginExpired) throw new Error('LOGIN_EXPIRED');
    if (prepared.wrapperCount !== 1 || prepared.rowCount !== prepared.total) {
      throw new Error(`Incomplete device table: ${JSON.stringify(prepared)}`);
    }
    if (new Set(prepared.deviceNames).size !== prepared.deviceNames.length) {
      throw new Error('Device names are not unique');
    }
    const selected = await selectAll(client, prepared.deviceNames);
    await sleep(1000);
    if (commandRequests.length) {
      throw new Error(`Unexpected business command request during simulation: ${JSON.stringify(commandRequests)}`);
    }
    if (selected.visibleDialogs.length) {
      throw new Error(`Unexpected dialog during simulation: ${JSON.stringify(selected.visibleDialogs)}`);
    }
    await client.send('Page.bringToFront');
    console.log(JSON.stringify({
      mode: 'simulate-select-all',
      profileId: PROFILE_ID,
      workbenchTitle: WORKBENCH_TITLE,
      url: selected.href,
      pageSize: selected.pageSize,
      totalDeviceCount: selected.total,
      rowCount: selected.rowCount,
      selectedCount: selected.checkedNames.length,
      headerChecked: selected.headerChecked,
      deviceNames: selected.deviceNames,
      selectedNames: selected.checkedNames,
      commandRequests,
      businessButtonsClicked: [],
      finalConfirmClicked: false,
      visibleDialogs: selected.visibleDialogs,
    }, null, 2));
  } finally {
    client.close();
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
