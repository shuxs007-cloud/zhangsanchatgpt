// Same canonical browser job lock as the account-binding scripts. No credentials.
import fs from 'node:fs';
import { acquireSiteLock } from '/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs';

export const SITE_BROWSER_ID = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
let leasePromise;

export async function ensureOpenSmsSiteOwnership() {
  leasePromise ??= acquireSiteLock({browserId: SITE_BROWSER_ID}).catch(error => {
    throw Object.assign(new Error('OPENSMS_SITE_LOCK_UNAVAILABLE: 未取得共享网站锁，未联网'), {cause:error});
  });
  const lease = await leasePromise;
  const owner = JSON.parse(fs.readFileSync(lease.file, 'utf8'));
  if (owner.token !== process.env.OPENSMS_SITE_LOCK_TOKEN
      || owner.pid !== Number(process.env.OPENSMS_SITE_LOCK_PID)) {
    throw new Error('OPENSMS_SITE_LOCK_OWNER_CHANGED: 网站任务归属变化，未发送请求');
  }
  try { process.kill(owner.pid, 0); }
  catch { throw new Error('OPENSMS_SITE_LOCK_OWNER_GONE: 主任务已退出，未发送请求'); }
  if (process.env.OPENSMS_BROWSER_KIND === 'cloakbrowser') {
    const identity = JSON.parse(fs.readFileSync('/Users/zhangsan/Downloads/430/opensms_artifacts/state/new_browser/identity.json', 'utf8'));
    if (identity.browserSessionId !== process.env.OPENSMS_BROWSER_SESSION_ID
        || identity.pid !== Number(process.env.OPENSMS_BROWSER_SERVICE_PID)) {
      throw new Error('OPENSMS_NEW_BROWSER_SESSION_CHANGED: 新浏览器归属变化');
    }
    try { process.kill(identity.pid, 0); }
    catch { throw new Error('OPENSMS_NEW_BROWSER_SERVICE_GONE: 新浏览器已关闭'); }
    const file = '/Users/zhangsan/.opensms/locks/opensms-new-browser-report.lock';
    const job = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (!job.token || job.token !== process.env.OPENSMS_SITE_JOB_LOCK_TOKEN
        || job.pid !== Number(process.env.OPENSMS_SITE_JOB_LOCK_PID)) {
      throw new Error('OPENSMS_SITE_JOB_OWNER_CHANGED: 导出任务归属变化，未发送请求');
    }
    try { process.kill(job.pid, 0); }
    catch { throw new Error('OPENSMS_SITE_JOB_OWNER_GONE: 导出主进程已退出，未发送请求'); }
  }
  return lease;
}
