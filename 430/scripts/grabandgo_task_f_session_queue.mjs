// One serial WebForms state machine per already-verified isolated session.
// On a fatal local error, stop assigning cards and drain every started worker
// before callers dispose any Cookie container. Per-card transient errors are
// handled by the caller and returned as result rows.
export async function runSessionQueue(items, workers, handleItem) {
  if (!items.length) return [];
  if (!Number.isInteger(workers.length) || workers.length < 1 || workers.length > 8) {
    throw new Error('任务F工作器数量必须为1至8');
  }
  const results = new Array(items.length);
  let cursor = 0;
  let failed = false;
  let firstError;
  function stop(error) {
    if (!failed) firstError = error;
    failed = true;
  }
  await Promise.allSettled(workers.map(async (entry, workerIndex) => {
    while (!failed && cursor < items.length) {
      const index = cursor++;
      try {
        results[index] = await handleItem(entry, items[index], index, workerIndex + 1, stop);
      } catch (error) {
        stop(error);
      }
    }
  }));
  if (failed) throw firstError;
  return results;
}
