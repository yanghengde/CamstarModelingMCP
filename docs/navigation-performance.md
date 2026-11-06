# Navigation and streaming performance investigation (2026-11-06)

Three independent sources of latency were measured on the local machine:

1. The chat journal committed each streaming event synchronously on the server
   event loop. An isolated 1,000-event burst blocked a 10 ms heartbeat for 23.328 s.
   SQLite connection context managers also did not close their connections.
   The journal now groups up to 100 events / 50 ms, writes in a worker thread,
   and closes connections explicitly. Cancellation waits for the pending write
   before recording terminal state. The equivalent burst persisted all 1,000
   events in a run lasting 0.821 s, with a maximum heartbeat interval of 34 ms.
   These are synthetic stress measurements, not the duration of a user chat.
2. The chat document used parser-blocking scripts from an external CDN (about
   700 ms in the initial browser sample). Pinned, licensed local copies now load
   with `defer`, before the workspace script. Verified zero external requests.
3. Edge waited for the machine's configured PAC script before sending the first
   local request. Chromium NetLog recorded `PAC_FILE_DECIDER_FETCH_PAC_SCRIPT`
   and `PROXY_RESOLUTION_SERVICE_WAITING_FOR_INIT_PAC` at approximately 3.6 s.
   Fresh-browser comparison: system proxy settings gave 3,526 ms before request;
   an isolated `--no-proxy-server` browser gave 11 ms. Both localhost and 127.0.0.1
   can exhibit the PAC wait; changing the hostname alone is not a fix.

`scripts/open-local-workspace.cmd` launches a separate Edge app/profile for the
local workspace with proxy resolution disabled. It does not modify Windows or
the user's normal browser proxy settings. The local server must already be running.

Verification: 18 targeted Python tests passed (journal, cancellation, reconnect,
stream errors, log pagination); seven browser scenarios passed, including a
1,000-fragment reply and intact final output. The restarted live server rendered
chat, records, and log views in 145–363 ms after the initial PAC wait, with no
browser errors. No MES transactions or model requests were submitted for testing.

Run `scripts/measure_navigation.cjs` with `PLAYWRIGHT_MODULE` pointing to an
installed Playwright package to repeat read-only live navigation measurements.
Machine-specific generated timing output is in
`docs/navigation-performance-validation.json` when the script has been run.
