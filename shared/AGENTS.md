# OpenSMS execution coordination

## One named browser and one OpenSMS login

The browser is named `OpenSMS 共用浏览器`. Task A, carrier binding, and device
monitoring/one-click maintenance must reuse the single recorded CloakBrowser at
`/Users/zhangsan/Downloads/430/opensms_artifacts/state/new_browser/status.json`.
Use `/Users/zhangsan/Downloads/430/scripts/opensms_shared_browser.mjs` for exact
service/session/target verification under the canonical site lock. Never launch
another browser, read the old binding profile, or copy/import a second CK.
The old `cloakbrowser_free_test/login_browser.mjs` now delegates to the shared
service. Preserve the existing login, and queue website operations across chats.
Task F remains on the existing BitBrowser 430 Grab&Go session; it is not migrated.

All OpenSMS website tasks in BitBrowser 430 must run serially across chats,
including queries, reports, refreshes, writes, and independent verification.
Use the canonical `./opensms` entrypoint where supported. It owns the shared
session lock in `~/.opensms/locks` for the entire operation.

For other Node scripts, import `acquireSiteLock` from
`/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs` and acquire with
browserId `a53ce74e6c9e4fa08aec35f81ce1e2a3` before any browser/site request.
Keep the lock until all requests and verification finish. Never issue direct
browser-tool or external-script requests while another task holds this lock.
Unsupported workflows must first be integrated with this gate; do not bypass it.

Use serial requests spaced at least 2000 ms after each response. Do not increase
concurrency. On 403, cool down at least 30 seconds and refresh/retry only once;
stop and report if still blocked. Do not claim 403 is necessarily caused by speed.
Keep the browser silent. Preserve existing additive versus keep-only semantics.
