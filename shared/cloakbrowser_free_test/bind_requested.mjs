import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = fileURLToPath(new URL('../', import.meta.url));
// Historical batch: never replay it merely because the file was invoked.
if (!process.argv.slice(2).includes('--apply')) throw new Error('Historical binding batch requires fresh user authorization and explicit --apply; use account_operation.mjs for new requests');
const env={...process.env,OPENSMS_REQUEST_INTERVAL_MS:'1000'};
delete env.OPENSMS_SITE_LOCK_TOKEN; delete env.OPENSMS_SITE_LOCK_PID;
delete env.OPENSMS_SITE_JOB_LOCK_TOKEN; delete env.OPENSMS_SITE_JOB_LOCK_PID;
const results = [];
const tasks = [['小梦精聊888', 'A1,A12,B1,B11,B12,B1V,B11V,B12V'], ['貔貅中英精聊', 'A2,A21']];
for (const [name, carriers] of tasks) {
  const child = spawnSync(process.execPath, [path.join(root, 'opensms_ops.mjs'), 'add-users', name, carriers, '--apply'], {
    cwd: root, encoding: 'utf8', timeout: 600000,
    env
  });
  const result = {account: name, exitCode: child.status, output: child.stdout, error: child.stderr};
  results.push(result);
  console.log(JSON.stringify(result));
  if (child.error || child.status !== 0) {process.exitCode = 1; break;}
}
fs.writeFileSync(new URL('./binding_receipt.json', import.meta.url), JSON.stringify({browser: 'OpenSMS 共用浏览器',
  intervalMs: 1000, finishedAt: new Date().toISOString(), results}, null, 2));
