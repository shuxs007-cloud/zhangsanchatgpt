// Local scheduling signal only. It never submits, polls, retries or downloads.
import fs from 'node:fs/promises';
import path from 'node:path';

export function submissionReadyConfig(env = process.env) {
  const output = env.OPENSMS_EXPORT_SUBMISSION_READY_PATH;
  const token = env.OPENSMS_EXPORT_SUBMISSION_READY_TOKEN;
  if (!output && !token) return null; // Standalone exporter remains compatible.
  if (!output || !path.isAbsolute(output) || !/^[a-f0-9]{32}$/.test(token || '')
      || !/^\d{8}T\d{6}[+-]\d{4}$/.test(env.OPENSMS_RUN_ID || '')) {
    throw new Error('导出调度凭据不完整，禁止开始本轮导出');
  }
  return {output, token, runId: env.OPENSMS_RUN_ID};
}

export async function publishSubmissionReady(config, facts) {
  if (!config) return null;
  const date = facts.serverDate;
  if (facts.exportPageClosed !== true || !/^[a-f0-9]{32}$/i.test(facts.taskId || '')
      || !/^\d{4}-\d{2}-\d{2}$/.test(date || '')
      || new Date(`${date}T00:00:00Z`).toISOString().slice(0, 10) !== date) {
    throw new Error('导出尚未确认提交并关闭独立页面，禁止放行统计采集');
  }
  const receipt = {schemaVersion: 1, status: 'submitted', runId: config.runId,
    token: config.token, taskId: facts.taskId, serverDate: date,
    exportPageClosed: true, requestLaneReleased: true, publishedAt: new Date().toISOString()};
  const temporary = `${config.output}.${config.token}.tmp`;
  // Publish the complete document atomically, with exclusive/no-overwrite semantics.
  // This run-private local path is never a shared export-progress checkpoint.
  await fs.writeFile(temporary, JSON.stringify(receipt) + '\n', {flag: 'wx', mode: 0o600});
  try {
    await fs.link(temporary, config.output);
  } finally {
    await fs.unlink(temporary);
  }
  return receipt;
}
