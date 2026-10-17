// Offline validation only: no browser, API, export, or business write calls.
import fs from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { validateFilteredQueries, DATA_ANOMALY_POLICY } from './opensms_task_d_filtered_retention.mjs';

export function verifyPreparationFailure(state, raw) {
  const failure = state.taskDPreparationFailure;
  const require = (ok, message) => { if (!ok) throw Error(`TASK_D_PREPARATION_CONTINUATION_${message}`); };
  require(state.status === 'failed' && failure?.version === 1 && failure.runId === state.runId
    && failure.task === 'D' && failure.stage === 'task_d_prepare'
    && failure.code === 'OPENSMS_TASK_D_ROW_INVALID' && failure.category === 'carrier_identity_missing'
    && failure.businessWrites === 0 && failure.notificationSubmitted === false, 'FAILURE_INVALID');
  require(raw.runId === state.runId && raw.task === 'D' && raw.readOnly === true
    && raw.businessWrites === 0 && raw.proof?.dataAnomalyPolicy === undefined, 'RAW_PROOF_INVALID');
  let originalRejected = false;
  try { validateFilteredQueries(raw.cardNos, raw.proof); }
  catch (error) {
    require(error.code === failure.code && error.category === failure.category
      && error.details?.cardNo === failure.cardNo && error.details?.backendId === failure.backendId
      && error.details?.slot === failure.slot, 'ORIGINAL_FAILURE_MISMATCH');
    originalRejected = true;
  }
  require(originalRejected, 'ORIGINAL_FAILURE_NOT_REPRODUCED');
  const proof = structuredClone(raw.proof);
  proof.dataAnomalyPolicy = DATA_ANOMALY_POLICY;
  const verified = validateFilteredQueries(raw.cardNos, proof);
  require(verified.dataAnomalyRows.length === 1
    && verified.dataAnomalyRows[0].cardNo === failure.cardNo
    && verified.dataAnomalyRows[0].backendId === failure.backendId, 'EXCLUSION_SCOPE_CHANGED');
  return {status: 'passed', preparationOnly: true, businessCompleted: false,
    originalContractStillRejected: true, dataAnomalyPolicy: DATA_ANOMALY_POLICY,
    sourceCount: raw.cardNos.length, matchedCount: verified.matchedRows.length,
    approvedAnomalyRows: verified.dataAnomalyRows, exportReplayed: false,
    completedBusinessStagesReplayed: false, businessWrites: 0};
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [statePath, proofPath] = process.argv.slice(2);
  const state = JSON.parse(await fs.readFile(statePath, 'utf8'));
  const raw = JSON.parse(await fs.readFile(proofPath, 'utf8'));
  console.log(JSON.stringify(verifyPreparationFailure(state, raw)));
}
