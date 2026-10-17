import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const exec=promisify(execFile),root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
export async function finalizeMac(bundle,output){
  bundle=path.resolve(bundle);output=path.resolve(output);assert.equal(path.basename(bundle),'GrabQueryTool-Mac');
  for(const name of ['README.md','VALIDATION.md'])await fs.copyFile(path.join(root,'macos',name),path.join(bundle,name));
  const allowed=new Set(['server.mjs','launch.mjs','stop.mjs','selfcheck.mjs','report-portable.mjs','lib','public','runtime','node_modules','package.json','package-lock.json','distribution.json','README.md','VALIDATION.md','Start.command','Stop.command','OpenData.command','Check.command','open-data.mjs','config.example.json','BUILD.json','THIRD_PARTY_NOTICES.md','tests']);
  for(const f of await fs.readdir(bundle))assert.ok(allowed.has(f),`unexpected file: ${f}`);
  async function verify(dir){for(const e of await fs.readdir(dir,{withFileTypes:true})){const p=path.join(dir,e.name);assert.ok(!e.isSymbolicLink(),`symlink: ${p}`);if(e.isDirectory())await verify(p);else assert.ok(!/\.(node|xlsx)$/.test(e.name)&&!['server.log','server.lock','.DS_Store','config.json'].includes(e.name),`unexpected private/native file: ${p}`);}}
  await verify(bundle);
  for(const f of ['Start.command','Stop.command','Check.command','OpenData.command','runtime/run-node.sh','runtime/darwin-arm64/node','runtime/darwin-x64/node'])assert.ok((await fs.stat(path.join(bundle,f))).mode&0o111);
  const manifest=JSON.parse(await fs.readFile(path.join(bundle,'BUILD.json'),'utf8'));
  for(const r of manifest.runtimes){const bytes=await fs.readFile(path.join(bundle,'runtime','darwin-'+r.arch,'node'));assert.equal(createHash('sha256').update(bytes).digest('hex'),r.executableSha256);assert.equal(bytes.readUInt32LE(0),0xfeedfacf);assert.equal(bytes.readUInt32LE(4),r.arch==='arm64'?0x0100000c:0x01000007);}
  manifest.packagedAt=new Date().toISOString();await fs.writeFile(path.join(bundle,'BUILD.json'),JSON.stringify(manifest,null,2)+'\n');
  await fs.mkdir(output,{recursive:true});const stamp=new Date().toISOString().replace(/[-:]/g,'').slice(0,15),name=path.basename(bundle);
  const zip=path.join(output,`GrabQueryTool-Mac-Universal-1.0.0-${stamp}.zip`);
  await exec('/usr/bin/zip',['-q','-r',zip,name,'-x',`${name}/tests/*`],{cwd:path.dirname(bundle),maxBuffer:100000,timeout:120000});
  await exec('/usr/bin/unzip',['-tq',zip],{maxBuffer:100000});const bytes=await fs.readFile(zip),sha=createHash('sha256').update(bytes).digest('hex');
  await fs.writeFile(zip+'.sha256',`${sha}  ${path.basename(zip)}\n`);console.log(JSON.stringify({zip,bytes:bytes.length,sha256:sha,checksum:zip+'.sha256'}));
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))await finalizeMac(process.argv[2],process.argv[3]||path.join(root,'releases'));
