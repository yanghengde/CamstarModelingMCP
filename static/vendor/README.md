# Local browser dependencies

Pinned copies of the existing browser dependencies, served from this application
so navigation and Markdown rendering do not require access to an external CDN.

- `marked-15.0.12.min.js`: `marked@15.0.12/marked.min.js`, MIT license in `marked-LICENSE.md`.
- `purify-3.4.16.min.js`: `dompurify@3.4.16/dist/purify.min.js`, license in `DOMPurify-LICENSE`.

Downloaded from the corresponding versioned npm package URLs on cdn.jsdelivr.net.
Both scripts load with `defer`, in order before `workspace.js`.

## Optional chart renderer

`echarts-6.0.0.common.min.js` is Apache ECharts **6.0.0**, Apache-2.0.
Vendored from the Apache repository's pinned `6.0.0` tag:

- https://raw.githubusercontent.com/apache/echarts/6.0.0/dist/echarts.common.min.js
- https://raw.githubusercontent.com/apache/echarts/6.0.0/LICENSE (`ECharts-LICENSE`)
- https://raw.githubusercontent.com/apache/echarts/6.0.0/NOTICE (`ECharts-NOTICE`)

SHA-256 for the bundle (705143 bytes):
`fd958f318b139893918674790290fcd73d19d953b91175d5277250ffaa177d8e`.

The bundle is imported **only by `chart-worker.js`**, on demand. Never add it to
the HTML or load it on the chat UI thread. Fixed templates use SVG SSR with
animation disabled and dispose every instance. The Worker also rasterizes PNG
through OffscreenCanvas for asynchronous image display: SVG image decoding/paint
can otherwise consume the UI thread. SVG is retained for export only and never
inserted into the document DOM. There are no model-supplied options, callbacks,
HTML tooltips, URL resources, or JavaScript execution templates.
