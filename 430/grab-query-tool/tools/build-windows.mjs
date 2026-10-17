import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {finalize} from './finalize-windows.mjs';
const exec=promisify(execFile),root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const output=path.resolve(process.argv[2]||path.join(root,'releases')),version='v24.21.0';
const work=await fs.mkdtemp(path.join(os.tmpdir(),'grab-windows-build-')),bundle=path.join(work,'GrabQueryTool-Windows-x64');
await fs.mkdir(bundle);await fs.mkdir(output,{recursive:true});
const name=`node-${version}-win-x64.zip`,base=`https://nodejs.org/dist/${version}/`;
async function fetchBytes(url){const r=await fetch(url,{signal:AbortSignal.timeout(120000)});if(!r.ok)throw new Error(`${url}: ${r.status}`);return Buffer.from(await r.arrayBuffer());}
console.log('Downloading official Node Windows runtime...');
const [archive,checksums]=await Promise.all([fetchBytes(base+name),fetchBytes(base+'SHASUMS256.txt')]);
const expected=checksums.toString().split(/\r?\n/).find(r=>r.endsWith('  '+name))?.split(/\s+/)[0];
assert.match(expected||'',/^[a-f0-9]{64}$/);assert.equal(createHash('sha256').update(archive).digest('hex'),expected);
const archiveFile=path.join(work,name);await fs.writeFile(archiveFile,archive);
await exec('/usr/bin/unzip',['-q',archiveFile,'-d',work]);
const nodeDir=path.join(work,`node-${version}-win-x64`);
for(const item of ['server.mjs','launch.mjs','stop.mjs','selfcheck.mjs','report-portable.mjs','lib','public'])await fs.cp(path.join(root,item),path.join(bundle,item),{recursive:true});
for(const item of ['package.json','README.md','Start.cmd','Stop.cmd','OpenData.cmd','Check.cmd','config.example.json'])await fs.copyFile(path.join(root,'windows',item),path.join(bundle,item));
// Batch files need CRLF for cmd.exe. Source stays portable LF.
for(const file of ['Start.cmd','Stop.cmd','OpenData.cmd','Check.cmd']){const p=path.join(bundle,file);await fs.writeFile(p,(await fs.readFile(p,'utf8')).replace(/\r?\n/g,'\r\n'));}
const sourceLock=path.join(root,'windows','package-lock.json');
let hasLock=false;try{await fs.copyFile(sourceLock,path.join(bundle,'package-lock.json'));hasLock=true;}catch(e){if(e.code!=='ENOENT')throw e;}
const npm=path.join(nodeDir,'node_modules','npm','bin','npm-cli.js');
console.log('Installing pinned public JavaScript dependencies, lifecycle scripts disabled...');
const install=await exec(process.execPath,[npm,hasLock?'ci':'install','--ignore-scripts','--omit=dev','--no-audit','--no-fund','--registry=https://registry.npmjs.org'],{cwd:bundle,env:{...process.env,PATH:path.dirname(process.execPath)+path.delimiter+(process.env.PATH||'')},maxBuffer:2_000_000,timeout:180000});
console.log(install.stdout.trim());if(!hasLock)await fs.copyFile(path.join(bundle,'package-lock.json'),sourceLock);
await fs.mkdir(path.join(bundle,'runtime'));await fs.copyFile(path.join(nodeDir,'node.exe'),path.join(bundle,'runtime','node.exe'));await fs.copyFile(path.join(nodeDir,'LICENSE'),path.join(bundle,'runtime','LICENSE'));
// No credentials, previous user jobs, symlinks, native macOS modules or Codex deps.
let files=0;
async function check(dir){for(const entry of await fs.readdir(dir,{withFileTypes:true})){const p=path.join(dir,entry.name);assert.ok(!entry.isSymbolicLink(),`symlink: ${p}`);if(entry.isDirectory())await check(p);else{files++;assert.ok(!p.endsWith('.node'),`native addon: ${p}`);assert.ok(!p.endsWith('.DS_Store'));}}}
// npm command shims are not needed by the app. Remove only its generated .bin symlinks.
const bin=path.join(bundle,'node_modules','.bin');try{for(const name of await fs.readdir(bin))await fs.unlink(path.join(bin,name));await fs.rmdir(bin);}catch(e){if(e.code!=='ENOENT')throw e;}
await check(bundle);
const manifest={builtAt:new Date().toISOString(),platform:'win32-x64',runtime:version,nodeOfficialUrl:base+name,nodeArchiveSha256:expected,dependencies:JSON.parse(await fs.readFile(path.join(bundle,'package.json'),'utf8')).dependencies,containsUserData:false,files};
await fs.writeFile(path.join(bundle,'BUILD.json'),JSON.stringify(manifest,null,2)+'\n');
await fs.writeFile(path.join(bundle,'THIRD_PARTY_NOTICES.md'),'# Third-party licenses\n\nNode.js: runtime/LICENSE.\n\nPlaywright Core: node_modules/playwright-core/LICENSE, NOTICE and ThirdPartyNotices.txt.\n\nExcelJS: node_modules/exceljs/LICENSE. All npm dependencies retain their distributed license files in node_modules.\n\nSources: https://nodejs.org/ ; https://github.com/microsoft/playwright ; https://github.com/exceljs/exceljs\n');
console.log(JSON.stringify({stage:bundle,output,manifest}));
if(!process.argv.includes('--stage-only')){
  const check=await exec(process.execPath,[path.join(bundle,'selfcheck.mjs')],{cwd:bundle,timeout:90000,maxBuffer:500000});console.log(check.stdout.trim());
  await finalize(bundle,output);
}
