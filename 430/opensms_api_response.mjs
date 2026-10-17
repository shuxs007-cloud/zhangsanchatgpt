// Validate inside the shared pacing/retry boundary, not after releasing it.
// HTTP denial is not evidence of an expired login or a safe business replay.
import { readRetryAfterMs } from './opensms_read_retry.mjs';

export function validateOpenSmsApiResponse(response, parse, { method, apiPath } = {}) {
  const status = response?.status;
  if (!Number.isSafeInteger(status) || status < 100 || status > 599
      || typeof response?.ok !== 'boolean' || typeof response?.text !== 'string') {
    throw Object.assign(new Error('OpenSMS浏览器响应结构无效'), {code:'OPENSMS_RESPONSE_INVALID'});
  }
  if (!response.ok || status < 200 || status >= 300) {
    // Never log a body that may contain cookies, credentials, or customer data.
    const bodyKind = response.text.trim() === 'nil' ? '，响应内容为nil' : '';
    throw Object.assign(new Error(`${method} ${apiPath} HTTP ${status}${bodyKind}；未将拒绝响应当作登录失效`), {
      status, retryAfterMs:readRetryAfterMs(response),
    });
  }
  try { return parse(response.text); }
  catch {
    throw Object.assign(new Error(`${method} ${apiPath} 响应解析/解密失败；未将协议错误当作登录失效`), {
      status, code:'OPENSMS_RESPONSE_PARSE_FAILED',
    });
  }
}
