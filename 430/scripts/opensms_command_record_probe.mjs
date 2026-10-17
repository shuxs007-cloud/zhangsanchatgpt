import fs from 'node:fs/promises';
import { RawCdpClient } from '../opensms_protocol_client.mjs';

const PROFILE_ID = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const WORKBENCH_TITLE = '430-_20_20_22-工作台';
const TARGET_URL = 'https://adminsms.smsoug.com/history';
const PORT_FILE = `/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/${PROFILE_ID}/DevToolsActivePort`;

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const port = Number.parseInt(await fs.readFile(PORT_FILE, 'utf8'), 10);
  if (!Number.isInteger(port) || port < 1) throw new Error('BitBrowser 430 CDP port is invalid');
  const endpoint = `http://127.0.0.1:${port}`;
  const targets = await (await fetch(`${endpoint}/json/list`)).json();
  const workbenches = targets.filter(target => target.type === 'page' && target.title === WORKBENCH_TITLE);
  if (workbenches.length !== 1 || !workbenches[0].url.includes(`id=${PROFILE_ID}`)) {
    throw new Error('BitBrowser 430 workbench identity mismatch');
  }
  const opensmsPages = targets.filter(target => {
    try { return target.type === 'page' && new URL(target.url).origin === new URL(TARGET_URL).origin; }
    catch { return false; }
  });
  if (opensmsPages.length !== 1) throw new Error(`OpenSMS page is not unique: ${opensmsPages.length}`);

  const version = await (await fetch(`${endpoint}/json/version`)).json();
  const browser = new RawCdpClient(version.webSocketDebuggerUrl);
  let targetId = null;
  try {
    await browser.connect();
    const protocol = await (await fetch(`${endpoint}/json/protocol`)).json();
    const create = protocol.domains?.find(domain => domain.domain === 'Target')
      ?.commands?.find(command => command.name === 'createTarget');
    if (!create?.parameters?.some(parameter => parameter.name === 'hidden')) {
      throw new Error('Browser does not support hidden targets');
    }
    const created = await browser.send('Target.createTarget', {
      url: TARGET_URL,
      background: true,
      hidden: true,
      forTab: false,
    }, 15000);
    targetId = created.targetId;
    const targetDeadline = Date.now() + 15000;
    let target = null;
    while (Date.now() < targetDeadline) {
      const current = await (await fetch(`${endpoint}/json/list`)).json();
      target = current.find(item => item.id === targetId && item.webSocketDebuggerUrl) || null;
      if (target) break;
      await sleep(250);
    }
    if (!target) throw new Error('Hidden command-record target did not appear');
    const page = new RawCdpClient(target.webSocketDebuggerUrl);
    try {
      await page.connect();
      await page.send('Page.enable');
      await page.send('Runtime.enable');
      const readyDeadline = Date.now() + 60000;
      while (Date.now() < readyDeadline) {
        const ready = await page.evaluate('({href: location.href, readyState: document.readyState})');
        if (ready.href.startsWith(TARGET_URL) && ready.readyState === 'complete') break;
        await sleep(500);
      }
      await sleep(2500);
      const result = await page.evaluate(`(() => {
        const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
        const visible = element => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        };
        const wrappers = [...document.querySelectorAll('.el-table__body-wrapper')];
        const headers = [...document.querySelectorAll('.el-table__header-wrapper th')]
          .filter(visible).map(cell => clean(cell.innerText));
        const populatedWrappers = wrappers.filter(wrapper => wrapper.querySelectorAll('tbody tr').length);
        const rows = populatedWrappers.length === 1
          ? [...populatedWrappers[0].querySelectorAll('tbody tr')].map(row => ({
              text: clean(row.innerText),
              cells: [...row.querySelectorAll('td')].map(cell => clean(cell.innerText)),
            }))
          : [];
        const body = clean(document.body?.innerText);
        return {
          href: location.href,
          title: document.title,
          loginExpired: /登录状态已过期|重新登录/.test(body) || /\\/login(?:$|[?#])/.test(location.href),
          headers,
          rowCount: rows.length,
          rows,
          pageSizes: [...document.querySelectorAll('input.el-input__inner')]
            .map(element => element.value).filter(value => /条\\/页$/.test(value)),
          totalText: body.match(/共\\s*\\d+\\s*条/)?.[0] || '',
          bodyStart: body.slice(0, 5000),
        };
      })()`);
      console.log(JSON.stringify({
        mode: 'command-record-first-page-probe',
        profileId: PROFILE_ID,
        workbenchTitle: WORKBENCH_TITLE,
        ...result,
        businessWrites: 0,
      }, null, 2));
    } finally {
      page.close();
    }
  } finally {
    if (targetId) await browser.send('Target.closeTarget', { targetId }, 10000).catch(() => {});
    browser.close();
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
