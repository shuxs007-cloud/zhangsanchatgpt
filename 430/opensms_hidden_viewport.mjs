// Target.createTarget(hidden:true) has no visible window. Chromium can give
// its renderer a 0 x 0 viewport; configure only this newly owned hidden target,
// never the user's login tab/window or the browser's screen fingerprint.
export async function configureOwnedHiddenViewport(client, kind = process.env.OPENSMS_BROWSER_KIND) {
  if (kind !== 'cloakbrowser') return null;
  await client.send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
  }, 15000);
  const size = await client.evaluate('({width: window.innerWidth, height: window.innerHeight})', 15000);
  if (!size || !Number.isFinite(size.width) || !Number.isFinite(size.height)
    || Math.abs(size.width - 1440) > 1 || Math.abs(size.height - 900) > 1) {
    throw Object.assign(new Error('OpenSMS 自有隐藏页面布局未确认，停止本轮'), {
      code: 'OPENSMS_HIDDEN_VIEWPORT_UNCONFIRMED',
    });
  }
  return {width: size.width, height: size.height, scope: 'owned-hidden-target-only'};
}
