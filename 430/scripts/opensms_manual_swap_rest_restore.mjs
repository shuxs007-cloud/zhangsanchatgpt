// Current-user manual recovery only. No export, periodic rules, or F changes.
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {main} from './opensms_manual_swap_t_restore.mjs';
import {MANUAL_RESTORE_SCOPES} from './opensms_swap_t_restore_engine.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
export const options={base:path.join(root,'opensms_artifacts/manual_swap_rest_restore'),
  scopes:MANUAL_RESTORE_SCOPES,onlineOnlyScopes:['换号区T','换号区C'],
  authorizationFlag:'--current-user-authorized-swap-rest-restore'};
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)
  main(process.argv.slice(2),options).catch(error=>{console.error(error.message);process.exitCode=1;});
