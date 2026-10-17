// User-authorized OpenSMS login reuse only; no password or Grab&Go state.
// Secret storage is the local, non-synchronizable macOS Keychain, not a report.
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

export const AUTH_ORIGIN = 'https://adminsms.smsoug.com';
const python = '/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
const helper = fileURLToPath(new URL('./opensms_auth_keychain.py', import.meta.url));

export function authStateFromTargetCookies(cookies) {
  if (!Array.isArray(cookies)) throw new Error('OPENSMS_AUTH_STATE_INVALID');
  // CDP omits sameSite for an unspecified cookie, whose browser default is Lax.
  // Map only storageState fields; never persist unrelated browser metadata.
  return scopedAuthState({cookies:cookies.map(cookie=>cookie && typeof cookie === 'object' ? ({
    name:cookie.name,value:cookie.value,domain:cookie.domain,path:cookie.path,
    expires:cookie.expires,httpOnly:cookie.httpOnly,secure:cookie.secure,
    sameSite:cookie.sameSite ?? 'Lax',
  }) : null),origins:[]});
}

export function scopedAuthState(state) {
  if (!state || !Array.isArray(state.cookies) || !Array.isArray(state.origins)) {
    throw new Error('OPENSMS_AUTH_STATE_INVALID');
  }
  const cookies = state.cookies.filter(cookie =>
    cookie && ['adminsms.smsoug.com', '.adminsms.smsoug.com', 'smsoug.com', '.smsoug.com'].includes(cookie.domain));
  // Persist only the site's authentication material, never remembered passwords
  // or unrelated origins. The current protocol authenticates with Admin-Token.
  const token = cookies.find(cookie => cookie.name === 'Admin-Token' && typeof cookie.value === 'string' && cookie.value);
  if (!token) throw new Error('OPENSMS_MANUAL_LOGIN_REQUIRED');
  const authCookies = cookies.filter(cookie => cookie.name === 'Admin-Token');
  for (const cookie of authCookies) {
    if (typeof cookie.value !== 'string' || !cookie.value || typeof cookie.path !== 'string' || !cookie.path.startsWith('/')
        || typeof cookie.expires !== 'number' || !Number.isFinite(cookie.expires)
        || typeof cookie.httpOnly !== 'boolean' || typeof cookie.secure !== 'boolean'
        || !['Strict','Lax','None'].includes(cookie.sameSite)) throw new Error('OPENSMS_AUTH_STATE_INVALID');
  }
  return {cookies: authCookies, origins: []};
}

async function keychain(action, value) {
  if (process.env.OPENSMS_FIXED_RULES_OFFLINE === '1') throw new Error('OPENSMS_AUTH_OFFLINE_NO_KEYCHAIN');
  return new Promise((resolve, reject) => {
    const child = spawn(python, [helper, action], {stdio:['pipe','pipe','ignore'],
      env: {PATH:'/usr/bin:/bin', LANG:'en_US.UTF-8'}});
    const chunks = [];
    let size = 0;
    const timer = setTimeout(() => {child.kill('SIGTERM'); reject(new Error('OPENSMS_AUTH_KEYCHAIN_UNAVAILABLE'));}, 15000);
    child.stdout.on('data', chunk => { size += chunk.length; if (size <= 1500000) chunks.push(chunk); });
    child.on('error', () => {clearTimeout(timer); reject(new Error('OPENSMS_AUTH_KEYCHAIN_UNAVAILABLE'));});
    child.stdin.on('error', () => {});
    child.on('close', code => {
      clearTimeout(timer);
      try {
        const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if (code !== 0 || size > 1500000 || result.status === 'failed') throw new Error();
        resolve(result);
      } catch {reject(new Error('OPENSMS_AUTH_KEYCHAIN_UNAVAILABLE'));}
    });
    child.stdin.end(value === undefined ? undefined : JSON.stringify(value));
  });
}

export class OpenSmsAuthStore {
  constructor(adapter = keychain) {this.adapter = adapter;}
  async load() {
    const result = await this.adapter('get');
    if (result.status === 'missing') return null;
    try {
      const payload = JSON.parse(Buffer.from(result.data, 'base64').toString('utf8'));
      if (payload.version !== 1 || payload.origin !== AUTH_ORIGIN || typeof payload.expired !== 'boolean') throw new Error();
      if (payload.expired) return null;
      return scopedAuthState(payload.storageState);
    } catch {throw new Error('OPENSMS_AUTH_STATE_INVALID');}
  }
  async save(state) {
    const storageState = scopedAuthState(state);
    await this.adapter('put', {version:1, origin:AUTH_ORIGIN, expired:false,
      verifiedAt:new Date().toISOString(), storageState});
    const readback = await this.load();
    if (JSON.stringify(readback) !== JSON.stringify(storageState)) throw new Error('OPENSMS_AUTH_KEYCHAIN_READBACK_FAILED');
    return {stored:true, store:'local-macOS-Keychain', synchronized:false, readbackPassed:true};
  }
  async invalidate() {
    await this.adapter('put', {version:1, origin:AUTH_ORIGIN, expired:true,
      verifiedAt:new Date().toISOString(), storageState:{cookies:[],origins:[]}});
  }
}

// This private change detector never goes into logs, status, or receipts.
export function authStateFingerprint(state) {
  return createHash('sha256').update(JSON.stringify(scopedAuthState(state))).digest('hex');
}
