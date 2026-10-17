import fs from 'node:fs/promises';
import path from 'node:path';

const SNAPSHOT_DIR = '/Users/zhangsan/Downloads/430/opensms_artifacts/snapshots';

function bjParts(date = new Date()) {
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Hong_Kong',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(date).reduce((acc, part) => {
    acc[part.type] = part.value;
    return acc;
  }, {});
}

function stamp(date = new Date()) {
  const p = bjParts(date);
  return `${p.year}${p.month}${p.day}${p.hour}${p.minute}${p.second}`;
}

function local(date = new Date()) {
  const p = bjParts(date);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

async function main() {
  await fs.mkdir(SNAPSHOT_DIR, { recursive: true });
  const checkedAt = new Date();
  const receiptPath = path.join(
    SNAPSHOT_DIR,
    `opensms_recover_unlisted_registered_${stamp(checkedAt)}.json`,
  );
  const receipt = {
    checkedAt: checkedAt.toISOString(),
    checkedAtLocal: local(checkedAt),
    status: 'disabled',
    offlineRecoveryEnabled: false,
    silentBrowserMode: true,
    browserConnected: false,
    browserForegroundActivationAttempted: false,
    candidateCount: 0,
    backendValidCount: 0,
    selectedCount: 0,
    actualWrites: 0,
    successes: 0,
    failures: 0,
    retries: 0,
    verification: '未执行',
    reason: '下架恢复功能已永久取消；未连接浏览器、未生成候选、未提交恢复、未重试、未回读',
  };
  await fs.writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`);
  console.log(JSON.stringify({ ...receipt, receiptPath }, null, 2));
}

await main();
