import { createHash } from 'node:crypto';
import { readRetryAfterMs } from '../opensms_read_retry.mjs';

// Only an explicit rate-limit response may extend the existing GET retry rule.
// Unknown JSON, authentication errors and damaged ZIPs remain terminal.
const RATE_MESSAGES = new Set([
  '请求过于频繁，请稍后再试', '请求过于频繁，请稍候再试',
  '操作过于频繁，请稍后再试', '操作过于频繁，请稍候再试',
]);

export function validateExportDownloadResponse(response) {
  const payload = typeof response?.base64 === 'string'
    ? Buffer.from(response.base64, 'base64') : Buffer.from(String(response?.text ?? ''));
  const httpStatus = Number.isInteger(response?.status) ? response.status : null;
  const zipHeader = payload[0] === 0x50 && payload[1] === 0x4b;
  const contentType = String(response?.contentType || '').split(';', 1)[0].toLowerCase();
  const diagnostic = {
    httpStatus, byteLength: payload.length, zipHeader,
    contentType: /^[a-z0-9.+-]+\/[a-z0-9.+-]+$/.test(contentType) ? contentType : null,
    bodySha256: createHash('sha256').update(payload).digest('hex'),
    businessCode: null, category: 'invalid_xlsx',
  };
  let body;
  if (!zipHeader && payload.length <= 16384) {
    try { body = JSON.parse(payload.toString('utf8')); } catch {}
  }
  if (body && !Array.isArray(body) && typeof body === 'object') {
    diagnostic.businessCode = Number.isSafeInteger(body.code) ? body.code : null;
    diagnostic.category = 'business_error';
  }
  const messages = [body?.msg, body?.message].filter(value => typeof value === 'string');
  // Preserve a bounded service explanation (including short delay numbers),
  // never raw JSON/data, credentials, phone numbers, URLs or long identifiers.
  const genericMessage = messages.length === 1 && messages[0].length <= 100
    && !/private|secret|token|cookie|password|authorization|bearer|https?:\/\/|@|\d{6,}|[a-zA-Z0-9_+\/=-]{24,}|[\x00-\x08\x0b-\x1f]/i.test(messages[0])
    ? messages[0] : null;
  if (genericMessage) diagnostic.businessMessage = genericMessage;
  if (body && !Array.isArray(body) && typeof body === 'object') {
    diagnostic.businessFields = Object.keys(body).filter(key => /^[A-Za-z_]{1,40}$/.test(key));
  }
  // Observed production response (HTTP 200 / code 500):
  // "导出过于频繁，请 1 分钟后再试". Honor its explicit delay; never
  // treat an arbitrary prose instruction or a generic business error as retryable.
  const minuteLimit = messages.length === 1
    ? /^导出过于频繁，请 ([1-9]\d{0,2}) 分钟后再试$/.exec(messages[0]) : null;
  const minuteDelayMs = minuteLimit ? Number(minuteLimit[1]) * 60000 : 0;
  const rateLimited = httpStatus === 429 || (httpStatus === 200
    && (diagnostic.businessCode === 429 || (diagnostic.businessCode === 500
      && messages.length === 1 && (RATE_MESSAGES.has(messages[0]) || minuteLimit !== null))));
  const authenticatedError = [401, 403].includes(httpStatus)
    || (httpStatus === 200 && [401, 403].includes(diagnostic.businessCode));
  if (rateLimited) diagnostic.category = 'rate_limited';
  else if (authenticatedError) diagnostic.category = 'authentication_or_permission';
  else if (httpStatus !== null && (httpStatus < 200 || httpStatus >= 300)) diagnostic.category = 'http_error';

  if (response?.ok === true && httpStatus >= 200 && httpStatus < 300
      && zipHeader && payload.length >= 100000) {
    // Full ZIP/CRC, schema, source freshness and SHA checks remain in the runner.
    return { payload, diagnostic: { ...diagnostic, category: 'xlsx_candidate' } };
  }
  const reason = rateLimited ? '下载接口明确限流'
    : authenticatedError ? '下载认证或权限失败'
      : diagnostic.category === 'business_error' ? '下载接口返回业务错误，不是Excel'
        : response?.ok !== true ? `下载本轮导出失败: HTTP ${httpStatus}`
          : '本轮导出不是完整XLSX';
  throw Object.assign(new Error(`${reason}: size=${payload.length}, HTTP ${httpStatus}, code=${diagnostic.businessCode ?? '-'}, category=${diagnostic.category}`), {
    status: rateLimited ? 429 : authenticatedError ? (httpStatus === 200 ? diagnostic.businessCode : httpStatus) : httpStatus,
    retryAfterMs: Math.max(minuteDelayMs, readRetryAfterMs({ retryAfter: response?.retryAfter,
      text: body ? JSON.stringify({ retry_after: body.retry_after }) : '' }) || 0),
    downloadDiagnostic: diagnostic,
  });
}
