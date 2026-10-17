import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {finalizeMac} from './finalize-macos.mjs';
const exec=promisify(execFile),root=path.dirname(path.dirname(fileURLToPath(import.meta.url))),version='v24.21.0';
const output=path.resolve(process.argv.find((x,i)=>i>1&&!x.startsWith('--'))||path.join(root,'releases'));
const work=await fs.mkdtemp(path.join(os.tmpdir(),'grab-macos-build-')),bundle=path.join(work,'GrabQueryTool-Mac');
await fs.mkdir(bundle);await fs.mkdir(path.join(bundle,'runtime'));
async function download(url){const r=await fetch(url,{signal:AbortSignal.timeout(120000)});if(!r.ok)throw new Error(`${url}: HTTP ${r.status}`);return Buffer.from(await r.arrayBuffer());}
const base=`https://nodejs.org/dist/${version}/`,hashes=(await download(base+'SHASUMS256.txt')).toString();
console.log('Downloading and verifying official macOS arm64 + x64 runtimes...');
const runtimeInfo=await Promise.all(['arm64','x64'].map(async arch=>{
  const name=`node-${version}-darwin-${arch}.tar.gz`,bytes=await download(base+name);
  const sha256=hashes.split(/\r?\n/).find(line=>line.endsWith('  '+name))?.split(/\s+/)[0];assert.match(sha256||'',/^[a-f0-9]{64}$/);assert.equal(createHash('sha256').update(bytes).digest('hex'),sha256);
  const archive=path.join(work,name);await fs.writeFile(archive,bytes);await exec('/usr/bin/tar',['-xzf',archive,'-C',work]);
  const extracted=path.join(work,`node-${version}-darwin-${arch}`),target=path.join(bundle,'runtime',`darwin-${arch}`);await fs.mkdir(target);
  await fs.copyFile(path.join(extracted,'bin','node'),path.join(target,'node'));await fs.chmod(path.join(target,'node'),0o755);await fs.copyFile(path.join(extracted,'LICENSE'),path.join(target,'LICENSE'));
  return {arch,url:base+name,archiveSha256:sha256,executableSha256:createHash('sha256').update(await fs.readFile(path.join(target,'node'))).digest('hex'),extracted};
}));
for(const item of ['server.mjs','launch.mjs','stop.mjs','selfcheck.mjs','report-portable.mjs','lib','public'])await fs.cp(path.join(root,item),path.join(bundle,item),{recursive:true});
for(const item of ['package.json','distribution.json','README.md','config.example.json','Start.command','Stop.command','Check.command','OpenData.command','open-data.mjs'])await fs.copyFile(path.join(root,'macos',item),path.join(bundle,item));
await fs.copyFile(path.join(root,'macos','run-node.sh'),path.join(bundle,'runtime','run-node.sh'));
for(const item of ['Start.command','Stop.command','Check.command','OpenData.command','runtime/run-node.sh'])await fs.chmod(path.join(bundle,item),0o755);
const native=runtimeInfo.find(r=>r.arch===process.arch);assert.ok(native,'Build host must be arm64 or x64 Mac');
const npm=path.join(native.extracted,'lib','node_modules','npm','bin','npm-cli.js');
const sourceLock=path.join(root,'macos','package-lock.json');let hasLock=false;
try{await fs.copyFile(sourceLock,path.join(bundle,'package-lock.json'));hasLock=true;}catch(e){if(e.code!=='ENOENT')throw e;await fs.copyFile(path.join(root,'windows','package-lock.json'),path.join(bundle,'package-lock.json'));}
console.log('Installing pinned public dependencies with lifecycle scripts disabled...');
const options={cwd:bundle,env:{...process.env,PATH:path.dirname(process.execPath)+path.delimiter+(process.env.PATH||'')},maxBuffer:1000000,timeout:180000};
if(!hasLock){await exec(process.execPath,[npm,'install','--package-lock-only','--ignore-scripts','--no-audit','--no-fund'],options);await fs.copyFile(path.join(bundle,'package-lock.json'),sourceLock);}
console.log((await exec(process.execPath,[npm,'ci','--ignore-scripts','--omit=dev','--no-audit','--no-fund'],options)).stdout.trim());
const auditRun=await exec(process.execPath,[npm,'audit','--omit=dev','--json'],options);const audit=JSON.parse(auditRun.stdout);assert.equal(audit.metadata.vulnerabilities.total,0);
// npm's generated CLI symlinks are unnecessary; no user data is present here.
const bins=path.join(bundle,'node_modules','.bin');try{for(const f of await fs.readdir(bins))await fs.unlink(path.join(bins,f));await fs.rmdir(bins);}catch(e){if(e.code!=='ENOENT')throw e;}
const manifest={builtAt:new Date().toISOString(),platform:'darwin-arm64+x64',runtime:version,runtimes:runtimeInfo.map(({extracted,...r})=>r),dependencies:JSON.parse(await fs.readFile(path.join(bundle,'package.json'),'utf8')).dependencies,overrides:{exceljs:{uuid:'11.1.1'}},auditKnownVulnerabilities:0,containsUserData:false,nativeIntelTested:false};
await fs.writeFile(path.join(bundle,'BUILD.json'),JSON.stringify(manifest,null,2)+'\n');
await fs.writeFile(path.join(bundle,'THIRD_PARTY_NOTICES.md'),'# Third-party licenses\n\nNode.js: runtime/darwin-arm64/LICENSE and runtime/darwin-x64/LICENSE.\n\nPlaywright Core: node_modules/playwright-core/LICENSE, NOTICE and ThirdPartyNotices.txt.\n\nExcelJS: node_modules/exceljs/LICENSE. All npm dependencies retain their licenses.\n\nSources: https://nodejs.org/ ; https://github.com/microsoft/playwright ; https://github.com/exceljs/exceljs\n');
console.log(JSON.stringify({stage:bundle,output,runtimes:manifest.runtimes.map(r=>r.arch),auditKnownVulnerabilities:0}));
if(!process.argv.includes('--stage-only')){
  console.log((await exec(path.join(bundle,'Check.command'),[],{cwd:bundle,maxBuffer:100000,timeout:90000})).stdout);
  await finalizeMac(bundle,output);
}
