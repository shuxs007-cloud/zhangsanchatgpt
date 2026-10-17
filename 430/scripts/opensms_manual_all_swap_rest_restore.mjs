// Explicit current-user all-listing-state recovery. Never called by Task A.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {isDeepStrictEqual} from 'node:util';
import {main} from './opensms_manual_swap_t_restore.mjs';
import {MANUAL_RESTORE_SCOPES,liveIdentity} from './opensms_swap_t_restore_engine.mjs';
import {atomicJson} from './opensms_execute_task_g.mjs';
import {digest} from './opensms_task_g_engine.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
export async function run(argv=process.argv.slice(2)){
  const [mode,directory,authorization,...extra]=argv;
  if(extra.length||!['--preview','--apply'].includes(mode)||!path.isAbsolute(directory||''))
    throw Error('SWAP_ALL_RESTORE_ARGUMENTS_INVALID');
  const planner=async({population,zones,serverDate})=>{
    const identities=population.map(liveIdentity).sort((a,b)=>a.id.localeCompare(b.id));
    const phones=identities.map(r=>r.phone);
    if(new Set(phones).size!==phones.length)throw Error('SWAP_POPULATION_DUPLICATED');
    const input={runId:path.basename(directory),scope:MANUAL_RESTORE_SCOPES,
      onlineOnly:false,serverDate,zones,identities,phones};
    const file=path.join(directory,'all_population_selection.json');
    if(mode==='--preview')await atomicJson(file,input,{exclusive:true});
    const bytes=await fs.readFile(file);
    if(!isDeepStrictEqual(JSON.parse(bytes),input))throw Error('SWAP_ALL_SELECTION_CHANGED');
    return {path:file,sha256:digest(bytes),phones,inputCount:phones.length,duplicateCount:0};
  };
  await main([mode,directory,authorization],{
    base:path.join(root,'opensms_artifacts/manual_swap_rest_restore'),
    scopes:MANUAL_RESTORE_SCOPES,onlineOnlyScopes:[],selectionPlanner:planner,
    resolveWeekendOrigins:true,freezeEntirePopulation:true,
    authorizationFlag:'--current-user-authorized-all-swap-rest-restore'});
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)
  run().catch(error=>{console.error(error.message);process.exitCode=1;});
