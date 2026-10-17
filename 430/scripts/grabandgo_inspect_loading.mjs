#!/usr/bin/env node

import fs from 'node:fs/promises';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const profileId = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const portFile = `/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/${profileId}/DevToolsActivePort`;
const port = (await fs.readFile(portFile, 'utf8')).split(/\r?\n/)[0].trim();
const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, { timeout: 120000 });
const pages = browser.contexts()[0]?.pages() || [];
const page = [...pages].reverse().find(candidate => /Retailer_CustomerSIMCards/i.test(candidate.url()));
if (!page) throw new Error('未找到Customer SIM Cards页面');
if (process.argv.includes('--trigger-extend')) {
  const requestPromise = page.waitForRequest(request => (
    /Retailer_CustomerSIMCards/i.test(request.url())
    && request.method() === 'POST'
    && String(request.postData() || '').includes('btnExtendBuffer')
  ), { timeout: 30000 });
  await page.evaluate(() => { window.__simExtending = false; });
  await page.locator('#btnLoadMoreSims').click({ timeout: 30000, noWaitAfter: true });
  const request = await requestPromise;
  const form = new URLSearchParams(request.postData() || '');
  const selected = {};
  for (const [key, value] of form.entries()) {
    if (
      key === '__EVENTTARGET'
      || key === '__EVENTARGUMENT'
      || key === 'ctl00$ctl09'
      || key.endsWith('$hfTotal')
      || key.endsWith('$hfRendered')
      || key.endsWith('$hfResetView')
      || key.endsWith('$btnExtendBuffer')
    ) selected[key] = value;
  }
  console.log(JSON.stringify({
    method: request.method(),
    resourceType: request.resourceType(),
    url: request.url(),
    postFieldNames: [...new Set(form.keys())],
    postControlValues: selected,
  }, null, 2));
  await page.close({ runBeforeUnload: false }).catch(() => {});
  await browser.close();
  process.exit(0);
}
const result = await page.evaluate(() => {
  const button = document.getElementById('GagPRetailerHolder_btnExtendBuffer');
  const form = document.querySelector('form');
  return {
    href: location.href,
    readyState: document.readyState,
    total: document.getElementById('hfTotal')?.value || '',
    renderedHidden: document.getElementById('hfRendered')?.value || '',
    renderedRows: document.querySelectorAll('.scrollableDataList .sim-row').length,
    visible: typeof window.__simVisible === 'number' ? window.__simVisible : null,
    extending: Boolean(window.__simExtending),
    showAllPresent: Boolean(document.getElementById('GagPRetailerHolder_btnShowAllSimCards')),
    extendButton: button ? {
      tagName: button.tagName,
      type: button.getAttribute('type'),
      name: button.getAttribute('name'),
      value: button.getAttribute('value'),
      onclick: button.getAttribute('onclick'),
    } : null,
    formMethod: form?.method || '',
    formAction: form?.action || '',
    loadMoreSource: typeof window.loadMoreSims === 'function' ? String(window.loadMoreSims) : '',
    applyVisibilitySource: typeof window.applySimVisibility === 'function' ? String(window.applySimVisibility) : '',
    doPostBackType: typeof window.__doPostBack,
    doPostBackSource: typeof window.__doPostBack === 'function' ? String(window.__doPostBack) : '',
    webFormDoPostBackWithOptionsType: typeof window.WebForm_DoPostBackWithOptions,
    formOnsubmit: form?.getAttribute('onsubmit') || '',
  };
});
result.cookieMetadata = (await browser.contexts()[0].cookies('https://www.grabandgoplus.com/'))
  .map(cookie => ({ name: cookie.name, domain: cookie.domain, path: cookie.path, sameSite: cookie.sameSite }));
console.log(JSON.stringify(result, null, 2));
await browser.close();
