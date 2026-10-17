import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const [command, name, carriers] = process.argv.slice(2);
if (!['only-users', 'add-users', 'add-groups', 'query-user'].includes(command) || !name || (command !== 'query-user' && !carriers)) throw new Error('Expected binding operation or query-user');
const root = fileURLToPath(new URL('../', import.meta.url));
const env = {...process.env, OPENSMS_REQUEST_INTERVAL_MS: '1000'};
delete env.OPENSMS_SITE_LOCK_TOKEN; delete env.OPENSMS_SITE_LOCK_PID;
delete env.OPENSMS_SITE_JOB_LOCK_TOKEN; delete env.OPENSMS_SITE_JOB_LOCK_PID;
// The canonical operation resolves the one shared browser under its site lock.
// Never read the old independent profile or inherit its long-lived service lock.
const params = command === 'query-user' ? [command, name] : [command, name, carriers, '--apply'];
const result = spawnSync(process.execPath, [path.join(root, 'opensms_ops.mjs'), ...params],
  {cwd: root, env, stdio: 'inherit', timeout: 7200000});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
