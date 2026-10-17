#!/usr/bin/env node
// Task E read-only card-retention receipt. It never queries phone management
// and never writes carriers or expiry markers.

import fs from 'node:fs/promises';
import path from 'node:path';
import { createRawOpenSmsApiSession } from '../opensms_protocol_client.mjs';
import { collectCardRetention } from './opensms_card_retention_readonly_lib.mjs';
import { createReadOnlyApiLimiter } from './opensms_readonly_concurrency.mjs';

async function writeExclusive(file, payload) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const handle = await fs.open(file, 'wx');
  try {
    await handle.writeFile(`${JSON.stringify(payload, null, 2)}\n`);
  } finally {
    await handle.close();
  }
}

async function main() {
  const [tag, outputArg] = process.argv.slice(2);
  if (!tag || !outputArg) {
    throw new Error('usage: opensms_collect_task_e_card_retention.mjs tag output');
  }
  if (process.env.OPENSMS_ENABLE_TASK_E !== '1' || process.env.OPENSMS_FORCE_TASK_E !== '1') {
    throw new Error('任务E当前真实用户授权或程序门禁未满足，拒绝读取卡保留记录');
  }
  const output = path.resolve(outputArg);
  const session = await createRawOpenSmsApiSession({
    cdp: process.env.BITBROWSER_CDP,
    pathName: '/phoneConfig',
  });
  const readLimiter = createReadOnlyApiLimiter(session.api, {
    allowedRequests: ['GET /phone/cardRetain/list'],
  });
  try {
    const startedAt = Date.now();
    const retention = await collectCardRetention(readLimiter.api);
    await readLimiter.drain();
    const payload = {
      createdAt: new Date().toISOString(),
      task: 'E',
      purpose: 'retention_deadline_alert',
      forceGate: true,
      automationGate: true,
      sourceProvenance: 'phone_card_retain_all_pages',
      endpoint: '/phone/cardRetain/list',
      readOnlyElapsedMs: Date.now() - startedAt,
      readOnlyConcurrency: { ...readLimiter.stats },
      ...retention,
    };
    await writeExclusive(output, payload);
    console.log(JSON.stringify({ ...payload, path: output }, null, 2));
  } finally {
    await readLimiter.drain();
    session.client.close();
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
