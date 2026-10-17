import fs from 'node:fs/promises';
import {spawn} from 'node:child_process';
import {settings} from './lib/platform.mjs';
const {dataDir}=settings();await fs.mkdir(dataDir,{recursive:true,mode:0o700});
const child=spawn('/usr/bin/open',[dataDir],{stdio:'ignore'});child.on('error',()=>console.error(`请在 Finder 中打开：${dataDir}`));child.unref();
