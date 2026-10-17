import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';

import { SNAPSHOT_DIR, ensureArtifactDirs } from '../opensms_artifact_paths.mjs';
import { downloadGoogleWorkbook } from './opensms_google_download_transport.mjs';

const SHEET_ID = '1GiA9uR_2IQ4xntP_aWvCSaWfmC6WVQ7SrnXsy8YyyvM';
const SOURCE_URL = `https://docs.google.com/spreadsheets/d/${SHEET_ID}/export?format=xlsx`;

function stamp() {
  return new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
}

async function main() {
  const startedAt = new Date().toISOString();
  await ensureArtifactDirs();
  const runStamp = stamp();
  const downloaded = await downloadGoogleWorkbook(SOURCE_URL);
  const {bytes} = downloaded;

  const outputPath = path.join(SNAPSHOT_DIR, `opensms_google_card_slots_${runStamp}.xlsx`);
  const sha256 = crypto.createHash('sha256').update(bytes).digest('hex');
  await fs.writeFile(outputPath, bytes, { flag: 'wx' });

  const receipt = {
    runId: process.env.OPENSMS_RUN_ID || null,
    startedAt,
    downloadedAt: new Date().toISOString(),
    silentBrowserMode: true,
    browserForegroundActivationAttempted: false,
    networkOnly: true,
    sourceUrl: SOURCE_URL,
    outputPath,
    size: bytes.length,
    sha256,
    contentType: downloaded.contentType,
    downloadAttempts: downloaded.attempts,
    downloadElapsedMs: downloaded.elapsedMs,
  };
  const receiptPath = path.join(SNAPSHOT_DIR, `opensms_google_card_slots_${runStamp}_receipt.json`);
  receipt.receiptPath = receiptPath;
  await fs.writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { flag: 'wx' });
  console.log(JSON.stringify(receipt, null, 2));
}

main().catch(error => {
  if (error.code === 'OPENSMS_GOOGLE_DOWNLOAD_FAILED' && error.googleDownloadDiagnostic) {
    console.log(JSON.stringify({status:'failed', runId:process.env.OPENSMS_RUN_ID || null,
      code:error.code, businessWrites:0, ...error.googleDownloadDiagnostic}));
  }
  console.error(error.stack || String(error));
  if (error.downloadAttempts) console.error(JSON.stringify({downloadAttempts:error.downloadAttempts}));
  process.exit(1);
});
