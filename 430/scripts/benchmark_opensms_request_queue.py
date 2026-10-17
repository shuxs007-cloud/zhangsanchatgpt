#!/usr/bin/env python3
"""Offline same-workload race/FIFO comparison; never calls a browser or HTTP.

The legacy contender reproduces the old mkdir/100ms-poll lock race. Requests
are private timer stubs with a scaled 20ms gap, NOT production timing estimates.
The worker is OS-denied network and may write only inside its private folder.
"""
from pathlib import Path
import hashlib
import json
import statistics
import subprocess
import tempfile

from opensms_benchmark_local import sandbox_profile
from opensms_verify_fixed_rules import isolated_environment, NODE, ROOT


WORKER = r'''
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {randomUUID, createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
const {createRequestPacer} = await import(pathToFileURL(process.argv[1]));
const directory = process.argv[2];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const gapMs = 20, requestMs = 5;

// Test-only frozen legacy contention algorithm. No production fallback exists.
function legacyPacer(folder) {
  const lock = path.join(folder, 'request.lock');
  const state = path.join(folder, 'next.json');
  let queue = Promise.resolve();
  async function execute(operation) {
    await fs.mkdir(folder, {recursive:true});
    while (true) {
      try { await fs.mkdir(lock); break; }
      catch (error) { if(error.code !== 'EEXIST') throw error; await sleep(100); }
    }
    const owner = `${process.pid}-${randomUUID()}`;
    await fs.writeFile(path.join(lock, 'owner'), owner, {flag:'wx'});
    let next = 0;
    try { next = JSON.parse(await fs.readFile(state, 'utf8')).nextAllowedAt; }
    catch (error) { if(error.code !== 'ENOENT') throw error; }
    while (Date.now() < next) await sleep(Math.min(next - Date.now(), 1000));
    const result = await operation();
    const handle = await fs.open(path.join(lock, 'next.json'), 'wx');
    try { await handle.writeFile(JSON.stringify({nextAllowedAt:Date.now() + gapMs})); await handle.sync(); }
    finally { await handle.close(); }
    await fs.rename(path.join(lock, 'next.json'), state);
    assert.equal(await fs.readFile(path.join(lock, 'owner'), 'utf8'), owner);
    await fs.unlink(path.join(lock, 'owner')); await fs.rmdir(lock);
    return result;
  }
  return operation => {
    const pending = queue.then(() => execute(operation));
    queue = pending.catch(() => {}); return pending;
  };
}

const records = [];
let expected;
for (let iteration = 1; iteration <= 3; iteration++) {
  for (const mode of iteration % 2 ? ['legacy-race', 'fifo'] : ['fifo', 'legacy-race']) {
    const folder = path.join(directory, `${iteration}_${mode}`);
    const pacer = () => mode === 'fifo'
      ? createRequestPacer({directory:folder, gapMs, cooldownMs:30, queueTimeoutMs:10000})
      : legacyPacer(folder);
    const a = pacer(), b = pacer(), exportStatus = pacer();
    const spans = [], values = [];
    let active = 0, maxActive = 0, firstStarted, release;
    const started = new Promise(resolve => {firstStarted = resolve;});
    const hold = new Promise(resolve => {release = resolve;});
    async function operation(id, first = false) {
      maxActive = Math.max(maxActive, ++active);
      const began = Date.now();
      if (first) { firstStarted(); await hold; }
      await sleep(requestMs);
      const value = {id, status:'ok'};
      values.push(value); spans.push({id, start:began, end:Date.now()}); active--;
      return value;
    }
    const began = Date.now();
    const bulkA = (async () => {
      for (let i=0; i<12; i++) await a(() => operation(`statistics-${i}`, i===0));
    })();
    await started;
    const statusQueuedAt = Date.now();
    const status = exportStatus(() => operation('export-status'));
    // Commit the competing registration before releasing the first request.
    await sleep(30);
    const bulkB = (async () => {
      for (let i=0; i<8; i++) await b(() => operation(`retention-${i}`));
    })();
    release();
    await Promise.all([bulkA, bulkB, status]);
    assert.equal(maxActive, 1);
    spans.sort((a,b) => a.start - b.start);
    for(let i=1; i<spans.length; i++) assert.ok(spans[i].start-spans[i-1].end >= gapMs);
    const semantic = values.sort((a,b) => a.id.localeCompare(b.id));
    const hash = createHash('sha256').update(JSON.stringify(semantic)).digest('hex');
    expected ??= hash; assert.equal(hash, expected);
    for(const label of ['statistics', 'retention']) {
      const sequence = spans.filter(s => s.id.startsWith(label+'-')).map(s => Number(s.id.split('-')[1]));
      assert.deepEqual(sequence, Array.from({length:label==='statistics'?12:8}, (_,i)=>i));
    }
    records.push({iteration, mode, totalMs:Date.now()-began,
      exportStatusQueueMs:spans.find(s=>s.id==='export-status').start-statusQueuedAt,
      requestCount:values.length, maxActive, semanticSha256:hash});
  }
}
await fs.writeFile(path.join(directory, 'worker.json'), JSON.stringify({records,
  allResultsEqual:true, perProducerOrderPreserved:true, completionGapPreserved:true,
  workload:{statistics:12, retention:8, exportStatus:1, scaledGapMs:gapMs, fakeRequestMs:requestMs},
  scope:'offline-scaled-contention-not-live-export-speed'}), {flag:'wx'});
'''


def sha(file):
    return hashlib.sha256(file.read_bytes()).hexdigest()


def main():
    directory = Path(tempfile.mkdtemp(prefix='development-request-fifo-',
                                     dir=(ROOT / 'outputs').resolve())).resolve()
    # The production queue is local APFS. External ExFAT delivery folders can
    # create AppleDouble sidecars and must not host request-lock experiments.
    worker_directory = Path(tempfile.mkdtemp(prefix='opensms-fifo-bench-')).resolve()
    source = ROOT / 'opensms_request_pacing.mjs'
    before = sha(source)
    command = ['/usr/bin/sandbox-exec', '-p', sandbox_profile(worker_directory), str(NODE),
               '--input-type=module', '-e', WORKER, str(source), str(worker_directory)]
    with (directory / 'benchmark.log').open('x') as log:
        completed = subprocess.run(command, cwd=ROOT, env=isolated_environment(worker_directory),
                                   stdout=log, stderr=subprocess.STDOUT, timeout=60)
    unchanged = sha(source) == before
    result = json.loads((worker_directory / 'worker.json').read_text()) if completed.returncode == 0 else {}
    medians = {}
    for mode in ('legacy-race', 'fifo'):
        rows = [row for row in result.get('records', []) if row['mode'] == mode]
        if rows:
            medians[mode] = {key: statistics.median(row[key] for row in rows)
                             for key in ('totalMs', 'exportStatusQueueMs')}
    improved = bool(medians) and all(medians['fifo'][key] < medians['legacy-race'][key]
                                   for key in ('totalMs', 'exportStatusQueueMs'))
    result.update(status='passed' if completed.returncode == 0 and unchanged and improved else 'failed',
                  medianMilliseconds=medians, measuredImprovement=improved,
                  networkDeniedByOS=True, writesRestrictedToPrivateDirectoryByOS=True,
                  sourceSha256=before, sourceUnchanged=unchanged,
                  businessSubmitted=False, configurationChanged=False,
                  privateWorkerDirectory=str(worker_directory),
                  receipt=str(directory / 'benchmark.json'))
    (directory / 'benchmark.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
