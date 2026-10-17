import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const exec=promisify(execFile),root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
export async function finalize(bundle,output){
  bundle=path.resolve(bundle);output=path.resolve(output);assert.equal(path.basename(bundle),'GrabQueryTool-Windows-x64');
  for(const file of ['README.md','VALIDATION.md'])await fs.copyFile(path.join(root,'windows',file),path.join(bundle,file));
  const pe=await fs.readFile(path.join(bundle,'runtime','node.exe'));assert.equal(pe.subarray(0,2).toString(),'MZ');const p=pe.readUInt32LE(0x3c);assert.equal(pe.subarray(p,p+4).toString(),'PE\0\0');assert.equal(pe.readUInt16LE(p+4),0x8664);
  const top=await fs.readdir(bundle);const allowed=['server.mjs','launch.mjs','stop.mjs','selfcheck.mjs','report-portable.mjs','lib','public','runtime','node_modules','package.json','package-lock.json','README.md','VALIDATION.md','Start.cmd','Stop.cmd','OpenData.cmd','Check.cmd','config.example.json','BUILD.json','THIRD_PARTY_NOTICES.md','tests'];
  for(const item of top)assert.ok(allowed.includes(item),`unexpected release file: ${item}`);
  for(const name of ['Start.cmd','Stop.cmd','OpenData.cmd','Check.cmd']){const s=await fs.readFile(path.join(bundle,name),'utf8');assert.ok(s.includes('\r\n'));assert.ok(!/(?<!\r)\n/.test(s));}
  const manifest=JSON.parse(await fs.readFile(path.join(bundle,'BUILD.json'),'utf8'));const pkg=JSON.parse(await fs.readFile(path.join(bundle,'package.json'),'utf8'));manifest.overrides=pkg.overrides;manifest.nativeWindowsTested=false;manifest.nodeExecutableSha256=createHash('sha256').update(pe).digest('hex');manifest.packagedAt=new Date().toISOString();
  await fs.writeFile(path.join(bundle,'BUILD.json'),JSON.stringify(manifest,null,2)+'\n');
  await fs.mkdir(output,{recursive:true});const stamp=new Date().toISOString().replace(/[-:]/g,'').slice(0,15);
  const file=path.join(output,`GrabQueryTool-Windows-x64-1.0.0-${stamp}.zip`),folder=path.basename(bundle);
  await exec('/usr/bin/zip',['-q','-r',file,folder,'-x',`${folder}/tests/*`,`${folder}/node_modules/.bin/*`],{cwd:path.dirname(bundle),maxBuffer:100000,timeout:120000});
  await exec('/usr/bin/unzip',['-tq',file],{maxBuffer:100000});
  const bytes=await fs.readFile(file),sha=createHash('sha256').update(bytes).digest('hex');await fs.writeFile(file+'.sha256',`${sha}  ${path.basename(file)}\n`);
  console.log(JSON.stringify({zip:file,bytes:bytes.length,sha256:sha,checksum:file+'.sha256',nativeWindowsTested:false}));return file;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))await finalize(process.argv[2],process.argv[3]||path.join(root,'releases'));
