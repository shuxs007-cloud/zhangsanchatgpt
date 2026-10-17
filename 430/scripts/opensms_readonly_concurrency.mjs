// Bounded concurrency for independent reads only. Never pass a write operation.

function workerLimit(value) {
  const limit = Number(value);
  if (!Number.isInteger(limit) || limit < 1 || limit > 32) {
    throw new Error(`只读并发数必须为1至32的整数: ${value}`);
  }
  return limit;
}

export async function mapReadOnlyBounded(items, mapper, { concurrency = 8 } = {}) {
  const limit = workerLimit(concurrency);
  const results = new Array(items.length);
  let next = 0;
  let firstError;
  let failed = false;
  async function worker() {
    while (!failed && next < items.length) {
      const index = next++;
      try {
        results[index] = await mapper(items[index], index);
      } catch (error) {
        if (!failed) firstError = error;
        failed = true;
      }
    }
  }
  // A rejected read must not leave sibling reads running after the caller closes
  // its session or enters another stage. Queued reads are never started on error.
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  if (failed) throw firstError;
  return results;
}

export function createReadOnlyApiLimiter(api, { concurrency = 8, allowedRequests = [] } = {}) {
  const limit = workerLimit(concurrency);
  const allowed = new Set(allowedRequests);
  const queue = [];
  const idleWaiters = [];
  let active = 0;
  let firstError;
  let failed = false;
  const stats = {
    configuredConcurrency: limit,
    maxActive: 0,
    submitted: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
  };
  function notifyIdle() {
    if (!active && !queue.length) {
      for (const resolve of idleWaiters.splice(0)) resolve();
    }
  }
  function pump() {
    while (!failed && active < limit && queue.length) {
      const task = queue.shift();
      active += 1;
      stats.maxActive = Math.max(stats.maxActive, active);
      Promise.resolve().then(() => api(...task.args)).then(value => {
        stats.completed += 1;
        task.resolve(value);
      }, error => {
        stats.failed += 1;
        if (!failed) firstError = error;
        failed = true;
        task.reject(error);
        for (const pending of queue.splice(0)) {
          stats.cancelled += 1;
          pending.reject(firstError);
        }
      }).finally(() => {
        active -= 1;
        pump();
        notifyIdle();
      });
    }
    notifyIdle();
  }
  return {
    stats,
    api(...args) {
      const operation = `${String(args[0]).toUpperCase()} ${args[1]}`;
      if (!allowed.has(operation)) {
        return Promise.reject(new Error(`只读并发器拒绝非白名单请求: ${operation}`));
      }
      if (failed) return Promise.reject(firstError);
      stats.submitted += 1;
      return new Promise((resolve, reject) => {
        queue.push({ args, resolve, reject });
        pump();
      });
    },
    drain() {
      if (!active && !queue.length) return Promise.resolve();
      return new Promise(resolve => idleWaiters.push(resolve));
    },
  };
}
