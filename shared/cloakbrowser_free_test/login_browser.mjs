// Legacy binding login entrypoint: use the same OpenSMS service as task A.
// No separate Chromium process, private profile or independent CK snapshot.
import {spawnSync} from 'node:child_process';
const argv=process.argv.slice(2);
if(argv.some(arg=>arg!=='--login')) throw new Error('Only --login is accepted; independent --serve is disabled');
const entry='/Users/zhangsan/Downloads/430/scripts/opensms_new_browser.mjs';
const env={...process.env};
delete env.OPENSMS_SITE_LOCK_TOKEN; delete env.OPENSMS_SITE_LOCK_PID;
delete env.OPENSMS_SITE_JOB_LOCK_TOKEN; delete env.OPENSMS_SITE_JOB_LOCK_PID;
const result=spawnSync(process.execPath,[entry,argv.includes('--login')?'login':'start'],
  {cwd:'/Users/zhangsan/Downloads/430',env,stdio:'inherit',timeout:105000});
if(result.error) throw result.error;
process.exitCode=result.status??1;
