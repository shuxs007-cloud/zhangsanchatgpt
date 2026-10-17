import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

// Use one lock across script copies and domains for the same browser session.
export async function acquireSiteLock({ browserId, timeoutMs = 7200000, directory = path.join(os.homedir(), '.opensms', 'locks') }) {
  if (!/^[a-zA-Z0-9_-]+$/.test(browserId)) throw new Error('Invalid browser lock key');
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const file = path.join(directory, `${browserId}.lock`);
  const read = () => { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; } };
  const inherited = read();
  if (inherited?.token && inherited.token === process.env.OPENSMS_SITE_LOCK_TOKEN && inherited.pid === Number(process.env.OPENSMS_SITE_LOCK_PID)) {
    process.kill(inherited.pid, 0);
    return { release() {}, inherited: true, file };
  }
  const started = Date.now();
  let announced = false;
  for (;;) {
    let fd;
    try { fd = fs.openSync(file, 'wx', 0o600); }
    catch (error) {
      if (error.code !== 'EEXIST') throw error;
      const owner = read();
      if (owner?.pid) {
        try { process.kill(owner.pid, 0); }
        catch (error) { if (error.code === 'ESRCH') throw new Error(`Abandoned site lock; inspect before removing: ${file}`); }
      }
      if (Date.now() - started >= timeoutMs) throw new Error(`Timed out waiting for site lock: ${file}`);
      if (!announced) { console.error('OpenSMS: another task owns this browser session; waiting.'); announced = true; }
      await new Promise(resolve => setTimeout(resolve, 250));
      continue;
    }
    const token = randomUUID();
    try { fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, token, startedAt: new Date().toISOString() })); }
    catch (error) { fs.closeSync(fd); fs.unlinkSync(file); throw error; }
    fs.closeSync(fd);
    process.env.OPENSMS_SITE_LOCK_TOKEN = token;
    process.env.OPENSMS_SITE_LOCK_PID = String(process.pid);
    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      if (read()?.token === token) fs.unlinkSync(file);
      delete process.env.OPENSMS_SITE_LOCK_TOKEN;
      delete process.env.OPENSMS_SITE_LOCK_PID;
    };
    process.once('exit', release);
    return { release, inherited: false, file };
  }
}
