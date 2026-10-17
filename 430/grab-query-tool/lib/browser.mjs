import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium,request as httpRequest} from 'playwright-core';
import {LoginRequired,SOURCE_URL,now} from './model.mjs';
import {settings,findBrowser} from './platform.mjs';
const ORIGIN=new URL(SOURCE_URL).origin;

class Cdp {
  constructor(socket){this.socket=socket;this.sequence=0;this.pending=new Map();
    socket.addEventListener('message',event=>{const m=JSON.parse(String(event.data)),p=this.pending.get(m.id);if(!p)return;this.pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(new Error(m.error.message)):p.resolve(m.result);});
    socket.addEventListener('close',()=>{for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(new Error('浏览器连接已断开'));}this.pending.clear();});
  }
  static async connect(url){
    const socket=new WebSocket(url);
    await new Promise((resolve,reject)=>{const t=setTimeout(()=>{socket.close();reject(new Error('连接浏览器超时'));},10000);socket.addEventListener('open',()=>{clearTimeout(t);resolve();},{once:true});socket.addEventListener('error',()=>{clearTimeout(t);reject(new Error('浏览器不可用'));},{once:true});});
    return new Cdp(socket);
  }
  send(method,params={}){const id=++this.sequence;return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error('浏览器请求超时'));},65000);this.pending.set(id,{resolve,reject,timer});this.socket.send(JSON.stringify({id,method,params}));});}
  async evaluate(expression){const r=await this.send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:false});if(r.exceptionDetails)throw new Error(r.result?.description||'浏览器读取失败');return r.result?.value;}
  close(){this.socket.close();}
}

// Exported to test HTML parsing against offline fixtures in a headless browser.
export function parsePageDocument(html){
  const doc=new DOMParser().parseFromString(html,'text/html');
  const authenticated=!!doc.querySelector('#GagPRetailerHolder_btnFilter')&&!doc.querySelector('input[type=password]');
  if(!authenticated)return {authenticated:false};
  const form=doc.querySelector('form');if(!form)throw new Error('缺少查询表单');
  const fields={};
  for(const input of form.querySelectorAll('input[name]'))if(input.type==='hidden'&&!input.disabled)fields[input.name]=input.value;
  const byIndex=new Map();
  for(const element of doc.querySelectorAll('[id^=GagPRetailerHolder_dlCustomerSimCards_]')){
    const m=element.id.match(/^GagPRetailerHolder_dlCustomerSimCards_(.+)_(\d+)$/);if(!m)continue;
    if(!byIndex.has(m[2]))byIndex.set(m[2],{});
    byIndex.get(m[2])[m[1]]=(element.textContent||'').replace(/\s+/g,' ').trim();
  }
  const rows=[...byIndex.values()].filter(f=>Object.hasOwn(f,'lblSimCardNumber')).map(f=>({queryIccid:f.lblSimCardNumber.replace(/\s+/g,'').replace(/[Ff]$/,''),fields:f}));
  const totalRaw=doc.querySelector('#hfTotal')?.value,renderedRaw=doc.querySelector('#hfRendered')?.value;
  if(!/^\d+$/.test(totalRaw||'')||!/^\d+$/.test(renderedRaw||''))throw new Error('网站缺少结果总数，拒绝推断不存在');
  const total=Number(totalRaw),rendered=Number(renderedRaw);
  if(rows.length!==rendered||total<rendered||new Set(rows.map(r=>r.queryIccid)).size!==rows.length)throw new Error('网站行数或 ICCID 唯一性校验失败');
  return {authenticated:true,fields,total,rendered,rows,searchValue:doc.querySelector('#GagPRetailerHolder_txtSearchCriteria')?.value||'',showAllPresent:!!doc.querySelector('#GagPRetailerHolder_btnShowAllSimCards')};
}

export class BrowserProvider {
  constructor(dataDir,options=settings()){this.dataDir=dataDir;this.options=options;this.state={status:'unchecked',message:'开始查询时自动检查登录',transport:null};this.form=null;this.cookieApi=null;this.source=null;this.loginWindowOpen=false;}
  async endpoint(){if(!this.options.portFile)throw new Error('使用独立登录浏览器');const port=(await fs.readFile(this.options.portFile,'utf8')).split(/\r?\n/)[0].trim();if(!/^\d+$/.test(port)||Number(port)<1||Number(port)>65535)throw new Error('比特浏览器端口文件无效');const base=`http://127.0.0.1:${port}`;const targets=await(await fetch(base+'/json/list',{signal:AbortSignal.timeout(5000)})).json();return {base,targets};}
  async connectExisting(){
    if(this.context){this.page=this.context.pages().find(p=>p.url().startsWith(ORIGIN))||this.page;if(this.page&&!this.page.isClosed())return;}
    if(this.cdp&&this.cdp.socket.readyState===WebSocket.OPEN)return;
    try{
      const {targets}=await this.endpoint();const target=targets.find(t=>t.type==='page'&&t.url.startsWith(SOURCE_URL))||targets.find(t=>t.type==='page'&&t.url.startsWith(ORIGIN+'/'));
      if(!target)throw new Error('430 中没有 Grab 页面');
      this.cdp=await Cdp.connect(target.webSocketDebuggerUrl);this.source='BitBrowser 430';
    }catch{throw new LoginRequired('未找到可用的 Grab 登录会话，需要手工登录');}
  }
  async evaluate(fn,arg){
    const expression=`(${fn.toString()})(${JSON.stringify(arg)})`;
    if(this.context)return this.page.evaluate(expression);
    if(!this.cdp)throw new LoginRequired();
    return this.cdp.evaluate(expression);
  }
  async captureCookies(){
    const cookies=this.context?await this.context.cookies([SOURCE_URL]):(await this.cdp.send('Network.getCookies',{urls:[SOURCE_URL]})).cookies;
    const filtered=cookies.filter(c=>['grabandgoplus.com','www.grabandgoplus.com'].includes(c.domain.replace(/^\./,''))).map(c=>({name:c.name,value:c.value,domain:c.domain,path:c.path,expires:c.expires>0?c.expires:-1,httpOnly:!!c.httpOnly,secure:!!c.secure,sameSite:['Strict','Lax','None'].includes(c.sameSite)?c.sameSite:'Lax'}));
    const userAgent=await this.evaluate(()=>navigator.userAgent,null);
    await this.cookieApi?.dispose();
    this.cookieApi=await httpRequest.newContext({storageState:{cookies:filtered,origins:[]},userAgent,timeout:60000,ignoreHTTPSErrors:false});
    this.state.cookieCapturedAt=now(); // never expose cookie values to the UI, logs or files.
    this.preferBrowser=false;
  }
  async fetchBrowser(data){
    return this.evaluate(async input=>{
      if(location.origin!==input.origin)throw new Error('请先完成 Grab 网页登录');
      const res=await fetch(input.url,{method:input.body===null?'GET':'POST',credentials:'include',cache:'no-store',signal:AbortSignal.timeout(60000),
        ...(input.body===null?{}:{headers:{'content-type':'application/x-www-form-urlencoded'},body:input.body})});
      return {status:res.status,url:res.url,html:await res.text()};
    },{origin:ORIGIN,url:SOURCE_URL,body:data});
  }
  async fetchPage(data=null){
    let response;
    if(!this.preferBrowser){
      try{
        const r=await this.cookieApi.fetch(SOURCE_URL,{method:data===null?'GET':'POST',maxRedirects:0,failOnStatusCode:false,
          ...(data===null?{}:{headers:{'content-type':'application/x-www-form-urlencoded'},data})});
        response={status:r.status(),url:r.url(),html:await r.text()};await r.dispose();
        if([301,302,303,307,308,401,403].includes(response.status)){this.preferBrowser=true;response=null;}
      }catch{this.preferBrowser=true;}
    }
    if(!response)response=await this.fetchBrowser(data);
    const parsedUrl=new URL(response.url);
    if(parsedUrl.origin!==ORIGIN||!/^\/Retailer_CustomerSIMCards\/?$/.test(parsedUrl.pathname))throw new LoginRequired();
    if([401,403].includes(response.status))throw new LoginRequired('登录或访问权限已失效，请重新手工登录');
    if(response.status!==200)throw new Error(`Grab 返回 HTTP ${response.status}，未把错误当作不存在`);
    const parsed=await this.evaluate(parsePageDocument,response.html);
    if(!parsed.authenticated)throw new LoginRequired();
    this.form=parsed.fields;this.state.transport=this.preferBrowser?'浏览器 Cookie 后台请求':'本机 Cookie HTTP 请求';
    return parsed;
  }
  async ensureAuthenticated(){
    try{
      await this.connectExisting();await this.captureCookies();
      await this.fetchPage();
      this.state={...this.state,status:'authenticated',source:this.source,checkedAt:now(),message:'已登录，Cookie 仅保存在进程内存'};this.loginWindowOpen=false;
      return this.state;
    }catch(error){if(error.code==='LOGIN_REQUIRED')this.state={...this.state,status:'login_required',message:error.message,checkedAt:now()};throw error;}
  }
  async search(term){
    if(!/^\d{15,22}$/.test(term))throw new Error('无效查询范围');
    if(!this.form)throw new LoginRequired();
    const params=new URLSearchParams(this.form);params.set('__EVENTTARGET','');params.set('__EVENTARGUMENT','');
    params.set('ctl00$GagPRetailerHolder$txtSearchCriteria',term);params.set('ctl00$GagPRetailerHolder$btnFilter','Go');
    try{return await this.fetchPage(params.toString());}
    catch(e){if(e.code==='LOGIN_REQUIRED'){this.form=null;this.state.status='login_required';this.state.message=e.message;this.loginWindowOpen=false;}throw e;}
  }
  async openLogin(){
    if(this.loginWindowOpen)return this.state;
    if(this.context){
      this.page=this.context.pages().find(p=>p.url().startsWith(ORIGIN))||await this.context.newPage();
      // Foregrounding is permitted ONLY here, because the user requested a manual login popup.
      await this.page.goto(SOURCE_URL,{waitUntil:'domcontentloaded',timeout:60000});await this.page.bringToFront();
    }else{
      let endpoint;
      try{endpoint=await this.endpoint();}catch{}
      if(endpoint){
        const meta=await(await fetch(endpoint.base+'/json/version',{signal:AbortSignal.timeout(5000)})).json();
        const browser=await Cdp.connect(meta.webSocketDebuggerUrl);
        try{const {targetId}=await browser.send('Target.createTarget',{url:SOURCE_URL,newWindow:true,background:false});
          this.cdp?.close();const targets=await(await fetch(endpoint.base+'/json/list')).json();const target=targets.find(t=>t.id===targetId);
          if(!target)throw new Error('登录窗口创建后未找到连接');this.cdp=await Cdp.connect(target.webSocketDebuggerUrl);this.source='BitBrowser 430';
        }finally{browser.close();}
      }else{
        await fs.mkdir(path.join(this.dataDir,'login-browser'),{recursive:true,mode:0o700});
        this.context=await chromium.launchPersistentContext(path.join(this.dataDir,'login-browser'),{executablePath:findBrowser(this.options),headless:false,viewport:null});
        this.page=this.context.pages()[0]||await this.context.newPage();await this.page.goto(SOURCE_URL,{waitUntil:'domcontentloaded',timeout:60000});
        this.source='独立浏览器登录窗口';
        this.context.on('close',()=>{this.context=null;this.page=null;this.loginWindowOpen=false;this.form=null;});
      }
    }
    this.loginWindowOpen=true;this.state={...this.state,status:'login_required',source:this.source,message:'请在登录窗口中手工登录；本程序不读取或填写密码'};return this.state;
  }
  async close(){await this.cookieApi?.dispose();this.cdp?.close();await this.context?.close();}
}
