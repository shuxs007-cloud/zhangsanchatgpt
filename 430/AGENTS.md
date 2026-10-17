# OpenSMS cross-chat execution

The shared browser's user-facing name is `OpenSMS 共用浏览器`. Task A,
carrier binding and device monitoring/one-click maintenance must use one
recorded CloakBrowser and its one live OpenSMS login. Other workflows resolve
the exact session via `scripts/opensms_shared_browser.mjs`; never use a separate
binding profile, copy CK to another context, or start a second browser/window.
Owned hidden targets in the same authenticated browser context are allowed;
they must be closed after the operation, and never become visible windows.
Task F continues to use BitBrowser 430 Grab&Go, with no shared OpenSMS CK.

All OpenSMS requests using CloakBrowser or BitBrowser 430 must share the session lock, including
read-only reports, queries, refreshes, mutations, and verification. Prefer the
canonical entrypoint `/Users/zhangsan/Documents/数据分析/opensms`.

Before running other site scripts, integrate `acquireSiteLock` from
`/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs`, using browserId
`a53ce74e6c9e4fa08aec35f81ce1e2a3`. Hold the lock through final verification.
Do not bypass it with direct browser tools or unguarded scripts. Existing scripts
without this integration are not automatically protected. Do not run them until
they have been integrated. Do not activate or foreground the browser.

Task A follows `/Users/zhangsan/Downloads/430/scripts/OPENSMS_10DAY_PATROL_POLICY.md`
and its unified export entrypoint. Reuse the same recorded CloakBrowser from
`opensms_artifacts/state/new_browser/status.json` across chats; do not create a
browser per chat or run simultaneous site requests. Verify exact identity and
hold the canonical website and report locks. Confirmed OpenSMS expiry may show
the existing manual-login window only under the explicit user-authorized policy;
403, network, or local cache errors are not expiry. Never close another chat's
active browser or borrow its business page. B/C/D/E require current explicit
authorization; automatic F uses the existing BitBrowser 430 Grab&Go session and
only verified latest-report gaps without valid local dates. F must stay silent.

The 2026-10-13 standing row-data anomaly rule in that policy applies only to
future A-I executions: isolate identifiable missing/malformed records, retain
their exact source and reason, finish other valid work, then report exclusions
for user guidance. Never count exclusions as success or guess missing values.
Conflicting identities, duplicate IDs, incomplete pages, global configuration,
authentication/transport failures and uncertain writes/commits still stop the
affected batch. A later explicit current-user "this time also skip" authorizes
only the policy's guarded, zero-submission D-preparation continuation for run
20261013T202956+0800. Preserve original failure evidence and never replay
completed business stages; task authorization and F rules remain unchanged.

Binding requests remain serial with at least 2 seconds after responses. On 403,
cool down at least 30 seconds and refresh/retry once, then stop if still blocked.
Preserve additive bindings unless the user explicitly requests removal/keep-only.

Task I is the offline document index and historical ICCID/phone statistics tool.
Use `scripts/run_opensms_task_i.sh` and `scripts/OPENSMS_TASK_I.md`. History remains
offline and never uses business or Task F databases. Current positions, only on
explicit user authorization, come from number-management ICCID search/export
through `scripts/opensms_task_i_current_slots.mjs`, with the canonical shared
session/report leases, own hidden page and serial pacing; never substitute A or
Google files for this current search. Keep its index on zs01 and
preserve every original file. Query the index for historical statistics; refresh
incrementally when current document coverage is required.

For Task I historical-report requests, prefer the unified local command
`scripts/run_opensms_task_i.sh run --list <absolute-list-path> --workers 7`.
It incrementally discovers files, queries/deduplicates the index, builds the
generic report and independently reads it back. Use a new output directory.
Do not run the one-batch legacy preparation/verification scripts. Routine
refreshes use registry/new-record checks; `--full-audit` explicitly requests
the more expensive full SQLite integrity audit. Offline requests must not
trigger `current-export` or reuse old search exports as current positions.
When the current user explicitly requests latest positions, the unified opt-in
`run --current-positions --authorization-source current-user` links the fresh
guarded ICCID search/export and full independent readback before report authoring.
Never add that flag to offline-only requests or Task A/F automations.
