// Explicit user-authorized login check only: no export, ICCID or database work.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import {acquireSiteLock} from '/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs';
import {checkLogin} from './grabandgo_task_f_lookup.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const python = '/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
let lease;
try {
  if (process.argv.slice(2).join(' ') !== '--authorization-source current-user') {
    throw new Error('GRAB_LOGIN_AUTHORIZATION_MISSING');
  }
  lease = await acquireSiteLock({browserId: 'a53ce74e6c9e4fa08aec35f81ce1e2a3', timeoutMs: 1500});
  const endpoint = execFileSync(python, ['-c',
    'from opensms_bitbrowser_430 import resolve_current_430_cdp; print(resolve_current_430_cdp())'],
    {cwd: path.join(root, 'scripts'), encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore']}).trim();
  process.env.BITBROWSER_CDP = endpoint;
  process.env.OPENSMS_TASK_F_CURRENT_SESSION_ONLY = '1';
  process.env.OPENSMS_TASK_F_POLICY_CATALOG_SYNC = '1';
  const directory = path.join(root, 'opensms_artifacts/manual_grab_login');
  await fs.mkdir(directory, {recursive: true, mode: 0o700});
  await checkLogin(path.join(directory, `existing-430-${Date.now()}-${process.pid}.json`));
} catch (error) {
  const code = /OPENSMS_TASK_F_430_[A-Z_]+/.exec(String(error?.message))?.[0]
    || (/site lock/i.test(String(error?.message)) ? 'GRAB_SHARED_SESSION_BUSY' : 'GRAB_AUTO_LOGIN_NOT_VERIFIED');
  console.log(JSON.stringify({status: 'failed', error: code,
    sensitiveIccidSubmittedCount: 0, credentialsWrittenToReceipt: false,
    browserForegroundActivationAttempted: false, failedJobReplayed: false}));
  process.exitCode = 1;
} finally { lease?.release(); }
// Disconnect this helper process, never close the user's live browser.
await new Promise(resolve => process.stdout.write('', resolve));
process.exit(Number(process.exitCode || 0));
