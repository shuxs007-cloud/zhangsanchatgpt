// Explicit manual keep-200 recovery; reuses the original locked restore engine.
import fs from 'node:fs/promises';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {main} from './opensms_manual_swap_t_restore.mjs';
import {digest} from './opensms_task_g_engine.mjs';
import {rankSwapPopulation,SWAP_SCOPES} from './opensms_swap_keep_worst_engine.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
export async function run(argv=process.argv.slice(2)){
  const [mode,directory,sourceState,authorization,...extra]=argv;
  if(extra.length||!['--preview','--apply'].includes(mode)||!path.isAbsolute(sourceState||''))throw Error('SWAP_KEEP_WORST_ARGUMENTS_INVALID');
  const metrics=JSON.parse(execFileSync('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3',
    [path.join(root,'scripts/opensms_swap_keep_worst_metrics.py'),sourceState],{encoding:'utf8',maxBuffer:32*1024*1024}));
  const planner=async({population,zones,serverDate})=>{
    if(serverDate!==metrics.serverDate)throw Error('SWAP_RANKING_PLATFORM_DATE_CHANGED');
    for(const proof of metrics.sourceProofs)if(digest(await fs.readFile(proof.path))!==proof.sha256)throw Error('SWAP_RANKING_SOURCE_CHANGED');
    const result=rankSwapPopulation(population,metrics,zones,200);
    return {path:metrics.workbook.path,sha256:metrics.workbook.sha256,phones:result.restore.map(r=>r.phone),
      inputCount:result.restore.length,duplicateCount:0,rankingSummary:result.summary,
      ranking:{sourceProofs:metrics.sourceProofs,kept:result.kept,restore:result.restore,unknown:result.unknown}};
  };
  await main([mode,directory,authorization],{base:path.join(root,'opensms_artifacts/manual_swap_keep_worst'),
    scopes:SWAP_SCOPES,onlineOnlyScopes:[],selectionPlanner:planner,freezeEntirePopulation:true,resolveWeekendOrigins:true,
    authorizationFlag:'--current-user-authorized-keep-worst-restore'});
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)
  run().catch(error=>{console.error(error.message);process.exitCode=1;});
