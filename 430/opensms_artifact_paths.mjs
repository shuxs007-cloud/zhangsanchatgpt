import path from 'node:path';
import fs from 'node:fs/promises';

export const BASE_DIR = process.env.OPENSMS_BASE_DIR || '/Users/zhangsan/Downloads/430';
export const ARTIFACT_ROOT = process.env.OPENSMS_ARTIFACTS_DIR
  || path.join(BASE_DIR, 'opensms_artifacts');
export const SNAPSHOT_DIR = path.join(ARTIFACT_ROOT, 'snapshots');
export const REPORT_DIR = path.join(ARTIFACT_ROOT, 'reports');
export const EXPORT_ARCHIVE_DIR = path.join(ARTIFACT_ROOT, 'exports');
export const LOG_DIR = path.join(ARTIFACT_ROOT, 'logs');
export const REPLY_RATE_HISTORY_DIR = path.join(SNAPSHOT_DIR, 'reply_rate_history');

export async function ensureArtifactDirs() {
  await Promise.all([
    ARTIFACT_ROOT,
    SNAPSHOT_DIR,
    REPORT_DIR,
    EXPORT_ARCHIVE_DIR,
    LOG_DIR,
    REPLY_RATE_HISTORY_DIR,
  ].map(directory => fs.mkdir(directory, { recursive: true })));
}
