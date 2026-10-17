import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import vm from 'node:vm';
import fs from 'node:fs/promises';
import { PAYLOAD_ORIGIN, createPayloadCodec, readLoadedPayloadConfig,
  loadPayloadCodec, loadPagePayloadCodec } from '../opensms_payload_codec.mjs';
import { probeExistingSession } from '../scripts/opensms_login_recovery_cli.mjs';
import { encryptPayload, decryptPayload, parseResponseText } from '../opensms_protocol_client.mjs';

function config(seed = 1) {
  return { origin: PAYLOAD_ORIGIN, scriptUrl: `${PAYLOAD_ORIGIN}/static/js/app.fixture.js`,
    key: Buffer.alloc(32, seed).toString('base64'), iv: Buffer.alloc(16, seed).toString('base64') };
}
function wire(value, cfg) {
  const cipher = crypto.createCipheriv('aes-256-cbc', Buffer.from(cfg.key, 'base64'), Buffer.from(cfg.iv, 'base64'));
  return Buffer.concat([cipher.update(JSON.stringify(value)), cipher.final()]).toString('base64');
}
function bundle(cfg = config()) {
  return `var c={enabled:!0,key:"${cfg.key}",iv:"${cfg.iv}"};AES.encrypt;AES.decrypt;mode.CBC;pad.Pkcs7;`;
}
async function readFixture(source, { origin = PAYLOAD_ORIGIN, scripts = [config().scriptUrl], ok = true,
  contentType = 'text/javascript; charset=utf-8' } = {}) {
  let calls = 0;
  class FixtureDOMParser {
    parseFromString(text) {
      return {scripts: [...text.matchAll(/<script\s+src=["']([^"']+)["']/gi)]
        .map(match => ({getAttribute: () => match[1]})),
      querySelector: selector => selector === '#app' && /id=["']app["']/.test(text) ? {} : null};
    }
  }
  const context = vm.createContext({ URL, Set, AbortSignal, DOMParser: FixtureDOMParser,
    location: { origin }, document: { scripts: scripts.map(src => ({ src })) },
    fetch: async (url, options) => {
      calls++;
      assert.equal(new URL(url).origin, PAYLOAD_ORIGIN);
      assert.equal(options.credentials, 'omit');
      assert.equal(options.redirect, 'error');
      return { ok, status: ok ? 200 : 403, headers: {get: () => contentType}, text: async () => source };
    },
  });
  const result = await vm.runInContext(`(${readLoadedPayloadConfig.toString()})()`, context);
  return { result, calls };
}

test('current-bundle codec parses quoted ciphertext and plain JSON, and matches independently encrypted requests', () => {
  const cfg = config(), codec = createPayloadCodec(cfg), value = { code: 200, rows: [], total: 0 };
  assert.deepEqual(parseResponseText(JSON.stringify(wire(value, cfg)), codec), value);
  assert.deepEqual(codec.parse(JSON.stringify(value)), value);
  assert.equal(encryptPayload(JSON.stringify(value), codec), wire(value, cfg));
  assert.equal(decryptPayload(wire(value, cfg), codec), JSON.stringify(value));
});
test('concurrent sessions retain separate codecs across a key rotation', async () => {
  const values = await Promise.all([1, 2, 3].map(async n => {
    const cfg = config(n), codec = await loadPayloadCodec(async () => cfg);
    return codec.parse(JSON.stringify(wire({ n }, cfg)));
  }));
  assert.deepEqual(values, [{ n: 1 }, { n: 2 }, { n: 3 }]);
  assert.throws(() => createPayloadCodec(config(2)).parse(JSON.stringify(wire({ n: 1 }, config(1)))), /解密失败/);
});
test('malformed response and missing codecs fail closed, without old-key fallback', () => {
  const codec = createPayloadCodec(config());
  for (const text of ['<html>bad gateway</html>', '"invalid ciphertext"']) {
    assert.throws(() => codec.parse(text), e => e.code === 'OPENSMS_PROTOCOL_ERROR');
  }
  assert.throws(() => encryptPayload('{}'), /禁止回退旧参数/);
  assert.throws(() => parseResponseText('{}'), /禁止回退旧参数/);
});
test('AES configuration rejects foreign sources, malformed base64 and invalid sizes', () => {
  for (const patch of [
    { origin: 'https://other.example' }, { scriptUrl: 'https://other.example/static/js/app.fixture.js' },
    { scriptUrl: `${PAYLOAD_ORIGIN}.evil.test/static/js/app.fixture.js` },
    { scriptUrl: `${PAYLOAD_ORIGIN}/static/js/app.fixture.js?key=bad` },
    { key: 'not base64' }, { key: Buffer.alloc(7).toString('base64') }, { iv: Buffer.alloc(8).toString('base64') },
  ]) assert.throws(() => createPayloadCodec({ ...config(), ...patch }), e => e.code === 'OPENSMS_PROTOCOL_ERROR');
});
test('metadata and serialized codec never contain AES material', () => {
  const cfg = config(), codec = createPayloadCodec(cfg);
  assert.ok(!JSON.stringify(codec).includes(cfg.key));
  assert.ok(!JSON.stringify(codec).includes(cfg.iv));
});
test('browser reader uses a unique same-origin loaded app asset and never executes it', async () => {
  const { result, calls } = await readFixture(bundle() + 'throw new Error("must not execute");');
  assert.equal(result.key, config().key);
  assert.equal(calls, 1);
});
test('browser reader rejects missing, ambiguous, foreign, disabled or failed configurations', async () => {
  await assert.rejects(readFixture(bundle(), { origin: 'https://other.example' }), /不同源/);
  await assert.rejects(readFixture(bundle(), { scripts: [] }), /不唯一或未加载/);
  await assert.rejects(readFixture(bundle(), { scripts: ['https://other.example/static/js/app.fixture.js'] }), /不唯一或未加载/);
  await assert.rejects(readFixture(bundle(), { scripts: [config().scriptUrl, `${PAYLOAD_ORIGIN}/static/js/app.second.js`] }), /不唯一或未加载/);
  for (const source of ['', bundle() + bundle(), bundle().replace('enabled:!0', 'enabled:!1'), bundle().replace('mode.CBC', 'mode.ECB')]) {
    await assert.rejects(readFixture(source), /配置缺失或不唯一/);
  }
  await assert.rejects(readFixture(bundle(), { ok: false }), /HTTP 403/);
});
test('HTTP-200 removed build returning new SPA HTML is classified, never executed or followed', async () => {
  const replacement = `${PAYLOAD_ORIGIN}/static/js/app.newbuild.js`;
  const {result, calls} = await readFixture(`<!doctype html><html><div id="app"></div><script src="${replacement}"></script></html>`,
    {contentType:'text/html; charset=utf-8'});
  assert.equal(calls,1);
  assert.equal(result.failureKind,'stale-app-bundle');
  assert.equal(result.replacementScriptUrl,replacement);
  assert.equal(result.key,undefined);
  await assert.rejects(loadPayloadCodec(async()=>result),e=>e.code==='OPENSMS_PROTOCOL_ERROR'
    && e.payloadFailureKind==='stale-app-bundle' && e.payloadDiagnostic.replacementScriptUrl===replacement);
});
test('challenge HTML, same build, ambiguous or foreign replacement cannot authorize recovery', async () => {
  for (const html of [
    '<!doctype html><html>Just a moment</html>',
    `<!doctype html><html><div id="app"></div><script src="${config().scriptUrl}"></script></html>`,
    '<!doctype html><html><div id="app"></div><script src="https://evil.invalid/static/js/app.newbuild.js"></script></html>',
    '<!doctype html><html><div id="app"></div><script src="/static/js/app.one.js"></script><script src="/static/js/app.two.js"></script></html>',
    '<!doctype html><html><div id="app"></div><script src="/static/js/app.newbuild.js?secret=x"></script></html>',
  ]) await assert.rejects(readFixture(html,{contentType:'text/html'}),/返回HTML或验证页/);
});
test('JavaScript content type and non-HTML body are mandatory even when AES markers exist', async () => {
  await assert.rejects(readFixture(bundle(),{contentType:'text/plain'}),/响应类型不是JavaScript/);
  await assert.rejects(readFixture(`<html>${bundle()}</html>`),/返回HTML或验证页/);
  await assert.rejects(readFixture(bundle(),{scripts:[`${config().scriptUrl}?secret=x`]}),/不唯一或未加载/);
});
test('forged stale-build evidence is rejected without exposing foreign URL or keys', async () => {
  const invalid = {failureKind:'stale-app-bundle',origin:PAYLOAD_ORIGIN,scriptUrl:config().scriptUrl,
    replacementScriptUrl:'https://evil.invalid/static/js/app.newbuild.js?secret=x'};
  await assert.rejects(loadPayloadCodec(async()=>invalid),e=>e.code==='OPENSMS_PROTOCOL_ERROR'
    && !e.payloadFailureKind && !JSON.stringify(e).includes('secret'));
});
test('same-page concurrent reads share one load; different pages do not share keys', async () => {
  let count = 0;
  const page = { evaluate: async () => { count++; return config(); } };
  const codecs = await Promise.all(Array.from({ length: 8 }, () => loadPagePayloadCodec(page)));
  assert.equal(count, 1);
  assert.ok(codecs.every(c => c === codecs[0]));
  const other = await loadPagePayloadCodec({ evaluate: async () => config(2) });
  assert.notEqual(other.encrypt('{}'), codecs[0].encrypt('{}'));
});
test('failed loads clear the cache, redact data and do not silently reuse stale keys', async () => {
  let count = 0;
  const page = { evaluate: async () => { count++; if (count === 1) throw new Error('secret-marker'); return config(); } };
  await assert.rejects(loadPagePayloadCodec(page), e => e.code === 'OPENSMS_PROTOCOL_ERROR' && !e.message.includes('secret-marker'));
  await loadPagePayloadCodec(page);
  assert.equal(count, 2);
});
test('known configuration failures retain a safe actionable reason only',async()=>{
  for(const reason of ['当前页面应用脚本不唯一或未加载','读取当前应用脚本HTTP 503','Failed to fetch']) {
    await assert.rejects(loadPayloadCodec(async()=>{throw new Error(`${reason} secret-marker`);}),
      e=>e.code==='OPENSMS_PROTOCOL_ERROR'&&!e.message.includes('secret-marker')
        &&e.message.includes(reason==='Failed to fetch'?'网络读取失败':reason));
  }
});
test('login preflight preserves protocol failure and closes connection instead of attempting login', async () => {
  let closed = 0;
  await assert.rejects(probeExistingSession(async () => ({
    api: async () => { throw Object.assign(new Error('解密失败'), { code: 'OPENSMS_PROTOCOL_ERROR' }); },
    browser: { close: async () => { closed++; } },
  })), e => e.code === 'OPENSMS_PROTOCOL_ERROR');
  assert.equal(closed, 1);
});
test('all three production API consumers use dynamic codecs without hard-coded key/IV', async () => {
  for (const file of ['opensms_protocol_client.mjs', 'scripts/opensms_balance_only.mjs', 'scripts/opensms_reply_rate_today.mjs']) {
    const source = await fs.readFile(new URL(`../${file}`, import.meta.url), 'utf8');
    assert.ok(source.includes('opensms_payload_codec.mjs'), file);
    assert.ok(!/const (?:KEY|IV) = Buffer\.from/.test(source), file);
  }
});
