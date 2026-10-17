import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {chromium} from 'playwright-core';
import {findBrowser} from '../lib/platform.mjs';
import {parsePageDocument} from '../lib/browser.mjs';
import {createApp} from '../server.mjs';
const A='8912230200277885949';
const record=key=>({queryIccid:key,fields:{lblPurchaseDate:'2026-09-11',lblActivationRequestDate:'',lblActivateDate:'',lblSimCardNumber:key,lblSKU:'TEST-SKU'}});
class FixtureProvider{
  constructor(){this.state={status:'authenticated'};this.calls=0;this.delayMs=200;}
  async ensureAuthenticated(){}
  async search(term){this.calls++;await new Promise(r=>setTimeout(r,this.delayMs));const rows=[record(term)];return {searchValue:term,total:1,rendered:1,rows};}
  async openLogin(){}
  async close(){}
}
test('HTML 明确日期字段、空记录与登录页解析',async()=>{
  const browser=await chromium.launch({executablePath:findBrowser(),headless:true});
  try{const page=await browser.newPage();
    const html=`<form><input id="GagPRetailerHolder_btnFilter"><input id="GagPRetailerHolder_txtSearchCriteria" value="${A}"><input id="hfTotal" value="1"><input id="hfRendered" value="1"><input name="__VIEWSTATE" type="hidden" value="PRIVATE"><span id="GagPRetailerHolder_dlCustomerSimCards_lblSimCardNumber_0">${A}</span><span id="GagPRetailerHolder_dlCustomerSimCards_lblActivateDate_0"></span><span id="GagPRetailerHolder_dlCustomerSimCards_lblPurchaseDate_0">2026-09-11</span></form>`;
    const r=await page.evaluate(parsePageDocument,html);assert.equal(r.authenticated,true);assert.equal(r.rows[0].fields.lblActivateDate,'');assert.equal(r.rows[0].queryIccid,A);
    assert.equal((await page.evaluate(parsePageDocument,'<input type="password">')).authenticated,false);
    await assert.rejects(page.evaluate(parsePageDocument,html.replace('id="hfTotal"','id="missing"')));
  }finally{await browser.close();}
});
test('页面输入、实时进度、历史查看、导出下载与文本精度',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'grab-ui-test-')),provider=new FixtureProvider();const app=await createApp({port:0,dataDir:dir,reportsDir:path.join(dir,'outputs'),provider,delay:0});
  const browser=await chromium.launch({executablePath:findBrowser(),headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(app.origin);await page.locator('#input').fill(A+'F\n'+A+'f');await page.getByRole('button',{name:'开始新查询',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#taskStatus').textContent==='已完成');assert.equal(await page.locator('#completed').textContent(),'1');assert.equal(await page.locator('#originalCount').textContent(),'2 / 1');
    assert.equal(await page.locator('#results tr').count(),1);assert.ok((await page.locator('#results').textContent()).includes(A));
    const downloadEvent=page.waitForEvent('download');await page.locator('#export').click();const download=await downloadEvent;const file=path.join(dir,'download.xlsx');await download.saveAs(file);const bytes=await fs.readFile(file);assert.equal(bytes.subarray(0,2).toString(),'PK');
    await page.reload();await page.waitForFunction(()=>document.querySelector('#completed').textContent==='1');assert.equal(provider.calls,1);
    await page.locator('#reuse').click();await page.getByRole('button',{name:'开始新查询',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.history-item').length===2&&document.querySelector('#taskStatus').textContent==='已完成');assert.equal(provider.calls,2);
    provider.delayMs=1000;await page.locator('#input').fill(A+'\n8912230200278118233');await page.locator('#start').click();
    await page.waitForFunction(()=>document.querySelector('#taskStatus').textContent==='查询中');await page.locator('#pause').click();
    await page.waitForFunction(()=>document.querySelector('#taskStatus').textContent==='已暂停');await page.locator('#resume').click();
    await page.waitForFunction(()=>document.querySelector('#taskStatus').textContent==='已完成');assert.equal(await page.locator('#completed').textContent(),'2');
    await page.locator('#start').click();await page.waitForFunction(()=>document.querySelector('#taskStatus').textContent==='查询中');page.once('dialog',d=>d.accept());await page.locator('#stop').click();await page.waitForFunction(()=>document.querySelector('#taskStatus').textContent==='已停止');
    await page.locator('.history-item').nth(1).click();await page.waitForFunction(()=>document.querySelector('#taskStatus').textContent==='已完成');
    assert.deepEqual(errors,[]);await page.screenshot({path:path.join(dir,'ui-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=393));await page.screenshot({path:path.join(dir,'ui-mobile.png'),fullPage:true});
    console.log(JSON.stringify({testArtifacts:dir,download:file}));
  }finally{await browser.close();await app.close();}
});
