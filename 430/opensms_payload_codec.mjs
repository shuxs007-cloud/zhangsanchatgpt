import crypto from 'node:crypto';

export const PAYLOAD_ORIGIN = 'https://adminsms.smsoug.com';

function protocolError(message) {
  return Object.assign(new Error(message), { code: 'OPENSMS_PROTOCOL_ERROR' });
}

// Read the same build already loaded by the authenticated page. Never execute
// downloaded JavaScript or persist key/IV material in logs, receipts or files.
export async function readLoadedPayloadConfig() {
  const origin = 'https://adminsms.smsoug.com';
  if (location.origin !== origin) throw new Error('AES参数页面不同源');
  const scripts = [...new Set([...document.scripts].map(s => s.src).filter(Boolean))]
    .map(s => new URL(s)).filter(u => u.origin === origin
      && /^\/static\/js\/app\.[\w-]+\.js$/.test(u.pathname)
      && !u.username && !u.password && !u.search && !u.hash);
  if (scripts.length !== 1) throw new Error('当前页面应用脚本不唯一或未加载');
  const response = await fetch(scripts[0].href, {
    credentials: 'omit', cache: 'no-cache', redirect: 'error',
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error(`读取当前应用脚本HTTP ${response.status}`);
  const source = await response.text();
  if (source.length > 5_000_000) throw new Error('应用脚本超过解析上限');
  const contentType = String(response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
  const html = /^\s*(?:<!doctype\s+html|<html)/i.test(source);
  if (contentType === 'text/html' || html) {
    // SPA servers can replace a removed build with index.html and HTTP 200.
    // Discover metadata only. Never extract keys from, execute, or silently
    // substitute the replacement asset into this old document.
    const documentCopy = new DOMParser().parseFromString(source, 'text/html');
    const replacements = [...new Set([...documentCopy.scripts]
      .map(s => s.getAttribute('src')).filter(Boolean))]
      .map(src => new URL(src, scripts[0].href)).filter(u => u.origin === origin
        && /^\/static\/js\/app\.[\w-]+\.js$/.test(u.pathname)
        && !u.username && !u.password && !u.search && !u.hash);
    if (html && contentType === 'text/html' && documentCopy.querySelector('#app')
        && replacements.length === 1 && replacements[0].href !== scripts[0].href) {
      return { failureKind: 'stale-app-bundle', origin,
        scriptUrl: scripts[0].href, replacementScriptUrl: replacements[0].href };
    }
    throw new Error('应用脚本返回HTML或验证页，未使用该响应');
  }
  if (!/^(?:text|application)\/(?:javascript|ecmascript|x-javascript)$/.test(contentType)) {
    throw new Error('应用脚本响应类型不是JavaScript');
  }
  const pattern = /\{\s*enabled\s*:\s*(?:!0|true)\s*,\s*key\s*:\s*["']([A-Za-z0-9+/=]+)["']\s*,\s*iv\s*:\s*["']([A-Za-z0-9+/=]+)["']\s*\}/g;
  const matches = [...source.matchAll(pattern)];
  if (matches.length !== 1 || !/AES\.encrypt/.test(source)
      || !/AES\.decrypt/.test(source) || !/mode\.CBC/.test(source)
      || !/pad\.Pkcs7/.test(source)) throw new Error('当前应用AES-CBC配置缺失或不唯一');
  return { origin, scriptUrl: scripts[0].href, key: matches[0][1], iv: matches[0][2] };
}

export function createPayloadCodec(config) {
  const url = new URL(config?.scriptUrl || 'about:blank');
  if (config?.origin !== PAYLOAD_ORIGIN || url.origin !== PAYLOAD_ORIGIN
      || url.username || url.password || url.search || url.hash
      || !/^\/static\/js\/app\.[\w-]+\.js$/.test(url.pathname)) {
    throw protocolError('拒绝不同源或无效的AES参数来源');
  }
  const decode = (value, lengths) => {
    if (typeof value !== 'string' || !/^[A-Za-z0-9+/]+={0,2}$/.test(value)) {
      throw protocolError('AES参数格式无效');
    }
    const bytes = Buffer.from(value, 'base64');
    if (!lengths.includes(bytes.length) || bytes.toString('base64') !== value) {
      throw protocolError('AES参数长度无效');
    }
    return bytes;
  };
  const key = decode(config.key, [16, 24, 32]);
  const iv = decode(config.iv, [16]);
  return Object.freeze({
    metadata: Object.freeze({ source: 'current-page-app-bundle', origin: PAYLOAD_ORIGIN,
      scriptUrl: url.href, algorithm: `aes-${key.length * 8}-cbc` }),
    encrypt(payload) {
      const cipher = crypto.createCipheriv(`aes-${key.length * 8}-cbc`, key, iv);
      return Buffer.concat([cipher.update(payload, 'utf8'), cipher.final()]).toString('base64');
    },
    decrypt(payload) {
      try {
        const decipher = crypto.createDecipheriv(`aes-${key.length * 8}-cbc`, key, iv);
        return Buffer.concat([decipher.update(payload, 'base64'), decipher.final()]).toString('utf8');
      } catch { throw protocolError('当前站点响应解密失败；未将协议错误当作登录失效'); }
    },
    parse(text) {
      try {
        const value = JSON.parse(text);
        return typeof value === 'string' ? JSON.parse(this.decrypt(value)) : value;
      } catch { throw protocolError('当前站点响应解析/解密失败；未将协议错误当作登录失效'); }
    },
  });
}

export async function loadPayloadCodec(evaluate) {
  try {
    const config = await evaluate(`(${readLoadedPayloadConfig.toString()})()`);
    if (config?.failureKind === 'stale-app-bundle') {
      const validAsset = value => {
        try {
          const url = new URL(value);
          return url.origin === PAYLOAD_ORIGIN && !url.username && !url.password
            && !url.search && !url.hash && /^\/static\/js\/app\.[\w-]+\.js$/.test(url.pathname);
        } catch { return false; }
      };
      if (config.origin !== PAYLOAD_ORIGIN || !validAsset(config.scriptUrl)
          || !validAsset(config.replacementScriptUrl) || config.scriptUrl === config.replacementScriptUrl) {
        throw protocolError('网页版本变化证据无效；未使用替代配置');
      }
      throw Object.assign(protocolError('当前页面引用旧版应用脚本，旧资源返回新版HTML；需同会话隐藏页面校验'), {
        payloadFailureKind: 'stale-app-bundle',
        payloadDiagnostic: { origin: PAYLOAD_ORIGIN, scriptUrl: config.scriptUrl,
          replacementScriptUrl: config.replacementScriptUrl },
      });
    }
    return createPayloadCodec(config);
  } catch (error) {
    if (error?.code === 'OPENSMS_PROTOCOL_ERROR') throw error;
    // Allowlisted reasons only. Never copy arbitrary browser exceptions (which
    // may contain tokens, response bodies or key material) to receipts.
    const message = String(error?.message || '');
    const reasons = ['AES参数页面不同源', '当前页面应用脚本不唯一或未加载',
      '应用脚本超过解析上限', '当前应用AES-CBC配置缺失或不唯一',
      '应用脚本返回HTML或验证页，未使用该响应', '应用脚本响应类型不是JavaScript'];
    let reason = reasons.find(value => message.includes(value));
    if (!reason && /读取当前应用脚本HTTP \d{3}(?:\D|$)/.test(message)) reason = message.match(/读取当前应用脚本HTTP \d{3}/)[0];
    if (!reason && /Failed to fetch|fetch failed/.test(message)) reason = '当前应用脚本网络读取失败';
    if (!reason && /TimeoutError|timed out|command timeout/i.test(message)) reason = '当前应用脚本读取超时';
    throw protocolError(`无法从当前页面校验AES参数${reason ? `：${reason}` : ''}；已停止，未回退旧参数`);
  }
}

const pageCodecs = new WeakMap();
export function loadPagePayloadCodec(page) {
  if (!pageCodecs.has(page)) {
    const pending = loadPayloadCodec(expression => page.evaluate(expression));
    pageCodecs.set(page, pending);
    pending.catch(() => pageCodecs.delete(page));
  }
  return pageCodecs.get(page);
}
