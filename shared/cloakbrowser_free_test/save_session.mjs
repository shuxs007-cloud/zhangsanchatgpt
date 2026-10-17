// CK stays in the one existing browser; never export another credential file.
// This compatibility command checks shared-session identity without launching.
import {acquireSharedOpenSmsSession} from '/Users/zhangsan/Downloads/430/scripts/opensms_shared_browser.mjs';
const shared=await acquireSharedOpenSmsSession({timeoutMs:0});
try {
  console.log(JSON.stringify({name:shared.session.displayName,status:'existing-shared-session',
    saved:false,separateCookieFileCreated:false,credentialValuesPrinted:false,
    browserLaunched:false,browserClosed:false}));
} finally {shared.release();}
