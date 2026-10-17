import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {main} from './opensms_manual_swap_t_restore.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const [mode,directory,phonesFile,authorization,...extra]=process.argv.slice(2);
if(extra.length>1||(extra.length&&extra[0]!=='--original-carrier-id-authoritative')||!path.isAbsolute(phonesFile||''))throw Error('SWAP_SELECTED_ARGUMENTS_INVALID');
main([mode,directory,authorization],{base:path.join(root,'opensms_artifacts/manual_selected_swap_restore'),
  scopes:['换号区T','换号区C'],phoneSelectionPath:phonesFile,resolveWeekendOrigins:true,
  originalCarrierIdAuthoritative:extra.includes('--original-carrier-id-authoritative'),
  authorizationFlag:'--current-user-authorized-selected-swap-restore'})
  .catch(error=>{console.error(error.message);process.exitCode=1;});
