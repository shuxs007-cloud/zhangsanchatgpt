// Explicit current-user restoration only. Not used by Task A or any timer.
import {runManualRestRestore} from './opensms_manual_kini_rest_restore.mjs';
const args=process.argv.slice(2);
const idFlag='--original-carrier-id-authoritative';
const baseLength=args[0]==='--preview'?2:3;
if(!['--preview','--apply'].includes(args[0]) || ![baseLength,baseLength+1].includes(args.length)
  || (args.length===baseLength+1&&args[baseLength]!==idFlag)
  || (args[0]==='--apply'&&args[2]!=='--current-user-authorized-task-g-rest-restore')){
  console.error('REST_CURRENT_USER_AUTHORIZATION_REQUIRED');process.exitCode=1;
}else await runManualRestRestore(args,{selection:'task-g-weekend'}).catch(error=>{
  console.error(error.message);process.exitCode=1;
});
