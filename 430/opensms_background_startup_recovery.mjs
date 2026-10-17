// Startup only: this callback must never submit an export or a business write.
// A replacement is permitted only after exact owned-target cleanup is proven.
export async function withBackgroundStartupRecovery(create, {
  onAttempt = event => console.error(JSON.stringify({backgroundStartupRecovery: event})),
} = {}) {
  const attempts = [];
  for (let attempt = 1; attempt <= 2; attempt++) {
    const started = Date.now();
    try {
      const page = await create();
      const event = {attempt, status: 'ready', targetId: page.targetId,
        elapsedMs: Date.now() - started, retry: false};
      attempts.push(event);
      onAttempt(event);
      page.startupRecovery = {status: attempt > 1 ? 'recovered' : 'ready', attempts};
      return page;
    } catch (error) {
      const cleanup = error.backgroundCleanup;
      const transient = error.code === 'BACKGROUND_NAVIGATION_TIMEOUT'
        || (error.code === 'BACKGROUND_PAGE_READ_FAILED'
          && ['CDP_COMMAND_TIMEOUT', 'CDP_CONNECTION_CLOSED'].includes(error.cause?.code));
      const retry = attempt === 1 && transient && !!cleanup?.targetId
        && cleanup.closed === true && cleanup.status === 'confirmed_closed';
      const event = {attempt, status: 'failed', category: error.code || 'non_retryable',
        command: error.cause?.cdpMethod || null, targetId: cleanup?.targetId || null,
        cleanupConfirmed: cleanup?.closed === true, elapsedMs: Date.now() - started, retry};
      attempts.push(event);
      onAttempt(event);
      if (!retry) {
        error.startupRecovery = {status: 'failed', attempts};
        throw error;
      }
    }
  }
}
