# Local browser dependencies

Pinned copies of the existing browser dependencies, served from this application
so navigation and Markdown rendering do not require access to an external CDN.

- `marked-15.0.12.min.js`: `marked@15.0.12/marked.min.js`, MIT license in `marked-LICENSE.md`.
- `purify-3.4.16.min.js`: `dompurify@3.4.16/dist/purify.min.js`, license in `DOMPurify-LICENSE`.

Downloaded from the corresponding versioned npm package URLs on cdn.jsdelivr.net.
Both scripts load with `defer`, in order before `workspace.js`.
