// Developer read-only layout diagnosis: own hidden page only, no export submit.
import { createRawBackgroundPage } from '../opensms_protocol_client.mjs';

if (process.argv[2] !== '--authorization-source' || process.argv[3] !== 'current-user')
  throw new Error('Explicit current-user diagnosis authorization required');
let page;
try {
  page = await createRawBackgroundPage({ cdp: process.env.BITBROWSER_CDP, url: 'https://adminsms.smsoug.com/export4day' });
  const samples = [];
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const sample = await page.client.evaluate(`(() => {
      const nodes = [];
      for (const element of document.querySelectorAll('body *')) {
        const text = String(element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim();
        const match = text.length <= 80 && text.match(/20\\d{2}[-/.年]\\d{1,2}[-/.月]\\d{1,2}/);
        if (!match) continue;
        const rect = element.getBoundingClientRect(), style = getComputedStyle(element);
        nodes.push({ dateText: match[0], tag: element.tagName, classes: String(element.className || '').slice(0, 120),
          ancestorClasses: [element.parentElement, element.parentElement?.parentElement]
            .map(node => String(node?.className || '').slice(0,120)),
          top: rect.top, left: rect.left, width: rect.width, height: rect.height,
          display: style.display, visibility: style.visibility, opacity: style.opacity });
      }
      return { origin: location.origin, pathname: location.pathname, readyState: document.readyState,
        width: innerWidth, height: innerHeight, scrollY, screenWidth: screen.width, screenHeight: screen.height,
        passwordInput: Boolean(document.querySelector('input[type="password"]')), dateNodes: nodes.slice(0,30) };
    })()`, 5000);
    samples.push(sample);
    if (sample.dateNodes.length || sample.passwordInput) break;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  console.log(JSON.stringify({ scope: 'read-only-owned-hidden-layout', businessRequests: 0,
    exportSubmissions: 0, silentBrowserMode: true, targetId: page.targetId, sample: samples.at(-1), samples: samples.length }));
} finally { if (page) await page.close(); }
