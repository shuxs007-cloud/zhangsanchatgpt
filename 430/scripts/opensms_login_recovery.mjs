const fixedLoginUrl = 'https://adminsms.smsoug.com/index';
const loginUrlPattern = /^https:\/\/adminsms\.smsoug\.com\/(?:index|login)?(?:[?#]|$)/i;
const loginErrorPattern = /验证码.{0,8}(错误|不正确|失效|过期)|captcha.{0,8}(error|invalid)/i;
const expiredLoginPattern = /登录状态已过期|登录失效|重新登录/;
const captchaServiceBase = 'http://127.0.0.1:2001';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function evaluate(cdp, expression) {
  return cdp.eval(expression);
}

async function retrySnapshot(cdp, attempts = 12) {
  let lastError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await loginSnapshot(cdp);
    } catch (error) {
      lastError = error;
      await sleep(250);
    }
  }
  throw lastError || new Error('无法读取登录页状态');
}

async function loginSnapshot(cdp) {
  return evaluate(cdp, `(() => {
    const visible = (el) => {
      const rect = el?.getBoundingClientRect?.();
      const style = el ? getComputedStyle(el) : null;
      return !!rect && rect.width > 0 && rect.height > 0 && style?.display !== 'none' && style?.visibility !== 'hidden';
    };
    const inputs = [...document.querySelectorAll('input')].filter(visible);
    const captcha = inputs.find((el) => {
      const hint = [el.name, el.id, el.placeholder, el.getAttribute('aria-label')].filter(Boolean).join(' ');
      return /验证码|captcha|verify|code/i.test(hint) && el.type !== 'password';
    }) || inputs.filter((el) => !['password', 'checkbox', 'radio', 'hidden'].includes(el.type)).slice(-1)[0] || null;
    const password = inputs.find((el) => el.type === 'password') || null;
    const account = inputs.find((el) => !['password', 'checkbox', 'radio', 'hidden'].includes(el.type) && el !== captcha) || null;
    const images = [...document.querySelectorAll('img, canvas')].filter((el) => {
      if (!visible(el)) return false;
      const rect = el.getBoundingClientRect();
      const hint = [el.id, el.className, el.getAttribute('alt'), el.getAttribute('src')].filter(Boolean).join(' ');
      return /验证码|captcha|verify|code/i.test(hint) || (rect.width >= 60 && rect.width <= 260 && rect.height >= 20 && rect.height <= 110);
    });
    images.sort((a, b) => {
      if (!captcha) return 0;
      const inputRect = captcha.getBoundingClientRect();
      const aRect = a.getBoundingClientRect();
      const bRect = b.getBoundingClientRect();
      return Math.hypot(aRect.left - inputRect.right, aRect.top - inputRect.top) - Math.hypot(bRect.left - inputRect.right, bRect.top - inputRect.top);
    });
    const image = images[0] || null;
    if (image) image.scrollIntoView({ block: 'center', inline: 'center' });
    const rect = image?.getBoundingClientRect?.();
    const fullBodyText = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
    return {
      href: location.href,
      isLogin: location.pathname === '/login' || (!!password && /登\\s*录/.test(fullBodyText)),
      loginExpired: /登录状态已过期|登录失效|重新登录/.test(fullBodyText),
      captchaError: /验证码.{0,8}(错误|不正确|失效|过期)|captcha.{0,8}(error|invalid)/i.test(fullBodyText),
      accountPresent: !!account?.value,
      passwordPresent: !!password?.value,
      captchaPresent: !!captcha,
      captchaImagePresent: !!image,
      captchaImage: image?.tagName === 'IMG' ? image.src || '' : '',
      captchaRect: rect ? { x: rect.left + scrollX, y: rect.top + scrollY, width: rect.width, height: rect.height } : null,
      bodyText: fullBodyText.slice(0, 1800),
    };
  })()`);
}

async function captchaBytes(cdp, state) {
  const match = /^data:image\/(?:gif|jpeg|jpg|png);base64,(.+)$/i.exec(state.captchaImage || '');
  if (match) return Buffer.from(match[1], 'base64');
  if (!state.captchaRect) throw new Error('验证码图片没有可截取区域');
  const capture = await cdp.send('Page.captureScreenshot', {
    format: 'png',
    fromSurface: true,
    captureBeyondViewport: true,
    clip: { ...state.captchaRect, scale: 1 },
  });
  if (!capture?.data) throw new Error('验证码图片截取失败');
  return Buffer.from(capture.data, 'base64');
}

async function fetchJson(url, options = {}, timeoutMs = 30_000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const text = await response.text();
    let body = null;
    try {
      body = JSON.parse(text);
    } catch {
      throw new Error(`本地OCR服务返回非JSON内容（HTTP ${response.status}）`);
    }
    if (!response.ok) throw new Error(`本地OCR服务HTTP ${response.status}：${body?.msg || '请求失败'}`);
    return body;
  } finally {
    clearTimeout(timer);
  }
}

async function captchaServiceHealth() {
  const health = await fetchJson(`${captchaServiceBase}/health`, {}, 5_000);
  if (health?.code !== 0) throw new Error(`本地OCR健康检查失败：${health?.msg || `code=${health?.code}`}`);
  return health;
}

export async function solveCaptchaWithService(bytes) {
  await captchaServiceHealth();
  const form = new FormData();
  form.append('image', new Blob([bytes]), 'captcha.png');
  const response = await fetchJson(`${captchaServiceBase}/ocr`, { method: 'POST', body: form });
  const answer = Number(response?.result);
  if (response?.code !== 0 || !Number.isFinite(answer)) {
    throw new Error(`验证码OCR结果无效：${response?.msg || `code=${response?.code}`}`);
  }
  return {
    expression: response.formula || null,
    answer,
    score: Number.isFinite(Number(response.score)) ? Number(response.score) : null,
    endpoint: `${captchaServiceBase}/ocr`,
    health: `${captchaServiceBase}/health`,
  };
}

async function navigateAndRefreshLogin(cdp, allowAuthenticatedRedirect = false) {
  await cdp.send('Page.navigate', { url: fixedLoginUrl });
  await sleep(1800);
  await cdp.send('Page.reload', { ignoreCache: true });
  await sleep(2500);
  const state = await retrySnapshot(cdp);
  if (allowAuthenticatedRedirect && !state.isLogin && !state.loginExpired
      && /^https:\/\/adminsms\.smsoug\.com\/(?:index|phoneConfig|day)?(?:[?#]|$)/.test(state.href)) {
    return state;
  }
  if (!state.isLogin || !loginUrlPattern.test(state.href)) {
    throw new Error(`刷新固定登录地址后未进入登录页：${state.href}`);
  }
  return state;
}

async function enterLoginFromExpiredPrompt(cdp, initialState) {
  if (initialState.isLogin) return true;
  if (!initialState.loginExpired && !expiredLoginPattern.test(initialState.bodyText)) return false;
  const clicked = await evaluate(cdp, `(() => {
    const visible = (el) => {
      const rect = el?.getBoundingClientRect?.();
      const style = el ? getComputedStyle(el) : null;
      return !!rect && rect.width > 0 && rect.height > 0 && style?.display !== 'none' && style?.visibility !== 'hidden';
    };
    const buttons = [...document.querySelectorAll('button, [role="button"]')]
      .filter(visible)
      .filter((el) => (el.innerText || '').replace(/\\s+/g, '') === '重新登录');
    if (buttons.length !== 1) return { ok: false, count: buttons.length };
    buttons[0].click();
    return { ok: true, count: 1 };
  })()`);
  if (!clicked?.ok) throw new Error(`登录失效页面的“重新登录”按钮数量不唯一：${clicked?.count ?? 0}`);
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    await sleep(500);
    const state = await retrySnapshot(cdp, 3);
    if (state.isLogin || loginUrlPattern.test(state.href)) return true;
  }
  throw new Error('点击“重新登录”后未进入登录页');
}

async function waitForLoginOutcome(cdp, timeoutMs = 12_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await sleep(500);
    const state = await retrySnapshot(cdp, 3);
    if (!state.isLogin) return { success: true, state };
    if (state.captchaError || loginErrorPattern.test(state.bodyText)) return { success: false, captchaError: true, state };
  }
  return { success: false, timeout: true, state: await retrySnapshot(cdp) };
}

export async function recoverOpenSmsLogin(cdp, {
  allowCaptchaSubmit = process.env.OPEN_SMS_CAPTCHA_AUTO_SUBMIT !== '0',
  allowAuthenticatedRedirect = false,
} = {}) {
  const initialState = await retrySnapshot(cdp);
  const loginRequired = await enterLoginFromExpiredPrompt(cdp, initialState);
  if (!initialState.isLogin && !loginRequired) return { recovered: false, loginRequired: false };

  let lastCaptcha = null;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const state = await navigateAndRefreshLogin(cdp, allowAuthenticatedRedirect);
    if (!state.isLogin && allowAuthenticatedRedirect) {
      // The CLI must still prove authentication through an independent API
      // read before recording success. No captcha is submitted in this path.
      return { recovered: true, loginRequired: true, attempts: attempt - 1,
        existingSessionRestored: true, finalUrl: state.href };
    }
    if (!state.accountPresent || !state.passwordPresent) {
      throw new Error('430 登录页未保留账号或密码，需要人工登录');
    }
    if (!state.captchaPresent || !state.captchaImagePresent) {
      throw new Error('登录页缺少验证码输入框或验证码图片');
    }

    const solved = await solveCaptchaWithService(await captchaBytes(cdp, state));
    lastCaptcha = solved;
    if (!allowCaptchaSubmit) {
      const error = new Error(`验证码待确认：${solved.expression || ''}${solved.answer}`);
      error.code = 'CAPTCHA_CONFIRMATION_REQUIRED';
      error.captcha = solved;
      throw error;
    }

    const submitted = await evaluate(cdp, `(() => {
      const visible = (el) => {
        const rect = el?.getBoundingClientRect?.();
        return !!rect && rect.width > 0 && rect.height > 0;
      };
      const inputs = [...document.querySelectorAll('input')].filter(visible);
      const captcha = inputs.find((el) => {
        const hint = [el.name, el.id, el.placeholder, el.getAttribute('aria-label')].filter(Boolean).join(' ');
        return /验证码|captcha|verify|code/i.test(hint) && el.type !== 'password';
      }) || inputs.filter((el) => !['password', 'checkbox', 'radio', 'hidden'].includes(el.type)).slice(-1)[0];
      const buttons = [...document.querySelectorAll('button')].filter(visible).filter(el => el.innerText.replace(/\\s/g, '') === '登录');
      if (!captcha || buttons.length !== 1) return { ok: false, buttonCount: buttons.length, captchaPresent: !!captcha };
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(captcha, ${JSON.stringify(solved.answer)});
      captcha.dispatchEvent(new Event('input', { bubbles: true }));
      captcha.dispatchEvent(new Event('change', { bubbles: true }));
      buttons[0].click();
      return { ok: true, buttonCount: 1, captchaPresent: true };
    })()`);
    if (!submitted?.ok) {
      throw new Error(`登录页验证码输入框或登录按钮不唯一（验证码=${submitted?.captchaPresent ? 1 : 0}，按钮=${submitted?.buttonCount ?? 0}）`);
    }

    const outcome = await waitForLoginOutcome(cdp, 30_000);
    if (outcome.success) {
      return {
        recovered: true,
        loginRequired: true,
        attempts: attempt,
        captcha: solved,
        captchaService: { health: solved.health, endpoint: solved.endpoint },
        preSubmitRefresh: true,
        refreshUrl: fixedLoginUrl,
        finalUrl: outcome.state.href,
      };
    }
    if (!outcome.captchaError) {
      throw new Error('登录提交后未进入后台，且页面未明确提示验证码错误');
    }
    if (attempt === 2) {
      const error = new Error('验证码连续两次错误，已停止登录恢复');
      error.code = 'CAPTCHA_RETRY_LIMIT_REACHED';
      error.captcha = solved;
      throw error;
    }
  }
  const error = new Error('登录恢复未完成');
  error.captcha = lastCaptcha;
  throw error;
}

export function isOpenSmsLoginUrl(url) {
  return loginUrlPattern.test(url);
}
