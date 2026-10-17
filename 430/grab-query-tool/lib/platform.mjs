import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const APP_ROOT=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
export function settings({platform=process.platform,env=process.env,home=os.homedir(),root=APP_ROOT,config,distribution}={}){
  const p=platform==='win32'?path.win32:path.posix;
  if(config===undefined){const file=path.join(root,'config.json');config=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{};}
  if(distribution===undefined){const file=path.join(root,'distribution.json');distribution=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{};}
  const windows=platform==='win32';
  const standaloneMac=platform==='darwin'&&distribution.mode==='standalone-mac';
  const dataDefault=windows?p.join(env.LOCALAPPDATA||p.join(home,'AppData','Local'),'GrabQueryTool'):standaloneMac?p.join(home,'Library','Application Support','GrabQueryTool'):p.join(home,'Downloads','430','opensms_artifacts','state','grab-query-tool');
  const dataDir=env.GRAB_TOOL_DATA_DIR||config.dataDir||dataDefault;
  const reportsDir=env.GRAB_TOOL_REPORTS_DIR||config.reportsDir||((windows||standaloneMac)?p.join(dataDir,'reports'):p.join(home,'Downloads','430','outputs','grab-query-tool'));
  if(!p.isAbsolute(dataDir)||!p.isAbsolute(reportsDir))throw new Error('dataDir 和 reportsDir 必须是绝对路径');
  const port=Number(env.GRAB_TOOL_PORT||config.port||(standaloneMac?43883:43881));
  if(!Number.isInteger(port)||port<1024||port>65535)throw new Error('端口必须是 1024–65535 的整数');
  return {platform,root,dataDir,reportsDir,port,
    portFile:config.bitBrowserPortFile||((windows||standaloneMac)?'':p.join(home,'Library','Application Support','BitBrowser','BrowserCache','a53ce74e6c9e4fa08aec35f81ce1e2a3','DevToolsActivePort')),
    chromePath:config.browserPath||'',reportEngine:env.GRAB_TOOL_REPORT_ENGINE||((windows||standaloneMac)?'portable':'artifact')};
}
export function browserCandidates(platform=process.platform,env=process.env,home=os.homedir()){
  if(platform==='win32'){
    const bases=[env['ProgramFiles(x86)'],env.ProgramFiles,env.PROGRAMFILES,env.LOCALAPPDATA||path.win32.join(home,'AppData','Local')].filter(Boolean);
    return [...new Set(['Microsoft\\Edge\\Application\\msedge.exe','Google\\Chrome\\Application\\chrome.exe'].flatMap(s=>bases.map(b=>path.win32.join(b,s))))];
  }
  if(platform==='darwin')return ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome','/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',path.posix.join(home,'Applications','Google Chrome.app','Contents','MacOS','Google Chrome'),path.posix.join(home,'Applications','Microsoft Edge.app','Contents','MacOS','Microsoft Edge')];
  return ['/usr/bin/google-chrome','/usr/bin/chromium','/usr/bin/chromium-browser'];
}
export function findBrowser(options=settings()){
  const candidates=options.chromePath?[options.chromePath]:browserCandidates(options.platform);
  const found=candidates.find(f=>fs.existsSync(f));
  if(!found)throw new Error('未找到 Edge / Chrome。请安装其中一个浏览器，或在 config.json 设置 browserPath。');
  return found;
}
export function openCommand(url,platform=process.platform,env=process.env){
  if(!/^http:\/\/127\.0\.0\.1:\d{4,5}\/?$/.test(url))throw new Error('只能打开本地查询页面');
  if(platform==='win32')return [path.win32.join(env.SystemRoot||'C:\\Windows','System32','rundll32.exe'),['url.dll,FileProtocolHandler',url]];
  return platform==='darwin'?['/usr/bin/open',[url]]:['xdg-open',[url]];
}
