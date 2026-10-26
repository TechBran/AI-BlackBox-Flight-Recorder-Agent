# Vendored third-party code (Portal/vendor/)

Everything under `Portal/vendor/` is upstream code copied byte-for-byte and
served by the Orchestrator at `/ui/vendor/...` (noVNC also at `/cu/novnc/`).
Never hand-edit these files: re-download, re-verify, and update this file.
Each vendored package keeps its upstream license file next to it.

Nothing the Portal runs comes from a CDN: a Portal client with no egress (a
LAN-only or offline box, a kiosk, a captive Wi-Fi, a CDN outage, a blocker)
still gets markdown, the sanitizer, code highlighting, the CLI terminal and
the CU live view. Every `<script>`/`<link>` for these files in
`Portal/index.html` carries an `integrity=` (SRI sha384) of the exact bytes.

`tests/web/vendor-pins.test.mjs` fails when any of these disagree: the bytes
on disk, the sha256 rows below (and in `novnc/VENDORED.md`), the exact npm pins
in `tests/web/package.json` (the vendored file must equal the pinned npm
release's file), the `integrity=` attributes, or a Portal page loading a
script or stylesheet from another origin. The jsdom suites in `tests/web/`
load the libraries from the files `index.html` names, so they run the
shipped bytes.

**Currency:** checked upstream on 2026-10-25; each row below was the latest
release of its line that day. Re-check monthly (`npm view <package> version`,
and the noVNC GitHub releases) as part of the release checklist.

| Package | Version | Directory | Source | License | Vendored |
|---|---|---|---|---|---|
| DOMPurify | 3.4.17 | `dompurify/` | npm `dompurify` | MPL-2.0 OR Apache-2.0 | 2026-10-17 |
| marked | 18.1.1 | `marked/` | npm `marked` | MIT | 2026-10-25 |
| marked-highlight | 2.2.4 | `marked-highlight/` | npm `marked-highlight` | MIT | 2026-10-25 |
| highlight.js | 11.12.0 | `highlightjs/` | npm `@highlightjs/cdn-assets` | BSD-3-Clause | 2026-10-25 |
| xterm.js | 6.0.0 | `xterm/` | npm `@xterm/xterm` | MIT | 2026-10-25 |
| xterm.js fit addon | 0.11.0 | `xterm-addon-fit/` | npm `@xterm/addon-fit` | MIT | 2026-10-25 |
| noVNC | 1.7.0 | `novnc/` | GitHub tag `v1.7.0` (= npm `@novnc/novnc`) | MPL-2.0 | 2026-10-25 |

Not vendored: source maps (`*.map`, DevTools only; a DevTools user sees a 404
for the `sourceMappingURL` comment some files end with).

## Updating any package

1. Download the npm tarball named under the package below and check its
   sha512 against `npm view <package>@<v> dist.integrity`.
2. Copy the listed files into the package directory; confirm them against a
   second source (jsDelivr or unpkg, and the project's own release where it
   publishes built files).
3. Update this file (version, URLs, integrity, sha256 rows, date), the
   `integrity=` in `Portal/index.html`, and the exact pin in
   `tests/web/package.json` (then `npm install --save-exact` there to refresh
   `package-lock.json`).
4. `cd tests/web && node --test`: `vendor-pins.test.mjs` fails if any of these
   disagree, and the markdown, sanitizer and terminal suites run against the
   new bytes. A new major also needs its changelog read against the Portal
   code that uses it (see the notes per package).

## DOMPurify 3.4.17

- **Origin:** https://github.com/cure53/DOMPurify (tag `3.4.17`, released 2026-10-12)
- **Source URL:** https://registry.npmjs.org/dompurify/-/dompurify-3.4.17.tgz
  (files `package/dist/purify.min.js`, `package/LICENSE`, `package/LICENSE-MPL`)
- **Tarball integrity (npm):** `sha512-B/pSlYVHQ58kqISD/yDjuJ1yfzI34m1gF4lAod2bwSRhiN4K5Cl3H8Af7hqB2PmTL911tdEudnnXn5FnhRoz0w==`
- **`purify.min.js` SRI (sha384):** `sha384-P0JNneQf81HOl9VwNXK76cQ9VMD6Nfsnepxb0cCOg8+UzEdzgPHAPV4wVQrvYQSx`
- **Cross-checked:** the same sha256 from three independent sources — the npm
  tarball, `https://raw.githubusercontent.com/cure53/DOMPurify/3.4.17/dist/purify.min.js`
  and `https://cdn.jsdelivr.net/npm/dompurify@3.4.17/dist/purify.min.js`.
- **License:** MPL-2.0 OR Apache-2.0 (`dompurify/LICENSE`, `dompurify/LICENSE-MPL`).
- **Why vendored:** it is the only thing standing between model output and
  innerHTML (`Portal/modules/sanitize.js`). Loading it from a CDN meant a
  LAN-only or offline box ran without it, and the 3.0.6 pin had published
  bypasses (CVE-2024-45801, CVE-2024-47875, CVE-2025-26791). Audit ids
  P01-chat-core-web-03, P07-html-css-04, X05-architecture-02.

| File | sha256 |
|---|---|
| `dompurify/purify.min.js` | `cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31` |
| `dompurify/LICENSE` | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| `dompurify/LICENSE-MPL` | `fab3dd6bdab226f1c08630b1dd917e11fcb4ec5e1e020e2c16f83a0a13863e85` |

## marked 18.1.1

- **Origin:** https://github.com/markedjs/marked (release `v18.1.1`, 2026-10-12)
- **Source URL:** https://registry.npmjs.org/marked/-/marked-18.1.1.tgz
  (files `package/lib/marked.umd.js`, `package/LICENSE`)
- **Tarball integrity (npm):** `sha512-zEGgrtUG8crk+TrMacJSVABU4DYDDkS8uqgU3ohrn/2fW9C2AEfUrO3m6IhpIpQpl7YcVpiXXC9cSMWDANT3dg==`
- **`marked.umd.js` SRI (sha384):** `sha384-rATToyt6602Jnq8c7w8HZwmyOUgPL7mzxX01FBWoV48ns/EgkakGozpES+PnaYOI`
- **Cross-checked:** jsDelivr and unpkg serve the same bytes (marked keeps no
  built files in git since v9).
- **License:** MIT (`marked/LICENSE`, which also carries the original
  Markdown.pl notice).
- **Upgrade notes (11.1.1 -> 18.1.1):** v16 removed `marked.min.js`; the UMD
  build is `lib/marked.umd.js` (minified, global `marked`). v12 tracks
  CommonMark 0.31 (`source` is no longer an HTML block tag), v13-v15 moved
  renderers to token objects and HTML escaping into the renderers, v17
  changed list/checkbox tokens, v18 trims trailing blank lines from block
  tokens. The Portal uses no custom renderer; its options are `breaks` and
  `gfm` only (the `highlight`, `headerIds`, `mangle` and `sanitize` keys were
  removed in v8 and are not passed).

| File | sha256 |
|---|---|
| `marked/marked.umd.js` | `b82b9334406d8bdd16b6236c08de1cb65b2a859873ca9503d9cc0b292ca3c12a` |
| `marked/LICENSE` | `8e3a3f82f59a60958f56ca08f445647c32a4733dc7ca6c2c46f6eb898471ab9c` |

## marked-highlight 2.2.4

- **Origin:** https://github.com/markedjs/marked-highlight (release `v2.2.4`,
  2026-04-07; peer range `marked >=4 <19`)
- **Source URL:** https://registry.npmjs.org/marked-highlight/-/marked-highlight-2.2.4.tgz
  (files `package/lib/index.umd.js`, `package/LICENSE`)
- **Tarball integrity (npm):** `sha512-PZxisNMJDduSjc0q6uvjsnqqHCXc9s0eyzxDO9sB1eNGJnd/H1/Fu+z6g/liC1dfJdFW4SftMwMlLvsBhUPrqQ==`
- **`index.umd.js` SRI (sha384):** `sha384-BjBE0bY2PIGsLgdqaC1+iwO397c7jxwJdNszWabXYxrbMXBTS9QCPxXJElH8xOE/`
- **Cross-checked:** jsDelivr serves the same bytes.
- **License:** MIT (`marked-highlight/LICENSE`).
- **Use:** global `markedHighlight`. `Portal/modules/markdown-renderer.js`
  installs it on a private marked instance used only for non-streaming
  renders; streamed renders use a plain instance and never run highlight.js.

| File | sha256 |
|---|---|
| `marked-highlight/index.umd.js` | `504b6fcb34e884d6b25e08a0d5f13fa5cfe383098440c20093ef00be0c96a463` |
| `marked-highlight/LICENSE` | `a846b93fda6af8008a89d1394d827afc60d62eebb70c68efbfeeafe0b01928ed` |

## highlight.js 11.12.0

- **Origin:** https://github.com/highlightjs/highlight.js (release `11.12.0`,
  2026-08-12). The browser build is published as `@highlightjs/cdn-assets`
  (the plain `highlight.js` npm package has no browser bundle).
- **Source URL:** https://registry.npmjs.org/@highlightjs/cdn-assets/-/cdn-assets-11.12.0.tgz
  (files `package/highlight.min.js` (common languages), `package/styles/atom-one-dark.min.css`,
  `package/LICENSE`)
- **Tarball integrity (npm):** `sha512-KvOKXODaiFmId9xaq3xc5xCL66wVLUuOngDbO9B/kewbFTqdGbn2nJxNhN3H5R1cgDTVj6R8vH0zgiNDEGjpDw==`
- **SRI (sha384):** `highlight.min.js` `sha384-wjfDDhOPPdjtva8vWBhWeVprSpmxisEu5aYT3q1JyACqXpdKpo3PWZTMVq24MBix`;
  `atom-one-dark.min.css` `sha384-oaMLBGEzBOJx3UHwac0cVndtX5fxGQIfnAeFZ35RTgqPcYlbprH9o9PUV/F8Le07`
  (the theme is unchanged since 11.9.0)
- **Cross-checked:** byte-identical to `https://raw.githubusercontent.com/highlightjs/cdn-release/11.12.0/build/`
  (the project's own release repo) and to the cdnjs copies.
- **License:** BSD-3-Clause (`highlightjs/LICENSE`).

| File | sha256 |
|---|---|
| `highlightjs/highlight.min.js` | `8ab71eb09c51f501e5e25157d9cff100e46cc29bcbfc744d0b746d451fca7f53` |
| `highlightjs/styles/atom-one-dark.min.css` | `4237ffca7ce6aadb438c457e0a675b125c534bbdda5b87f41f3a1495603bcc9b` |
| `highlightjs/LICENSE` | `6c081431591d9df696c82dc598fe1423765b8a299b200ed00b281afd0f64c490` |

## xterm.js 6.0.0 and the fit addon 0.11.0

- **Origin:** https://github.com/xtermjs/xterm.js (release `6.0.0`, 2025-12-22;
  `@xterm/addon-fit` 0.11.0 is its matching addon release)
- **Source URLs:** https://registry.npmjs.org/@xterm/xterm/-/xterm-6.0.0.tgz
  (files `package/lib/xterm.js`, `package/css/xterm.css`, `package/LICENSE`) and
  https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.11.0.tgz
  (files `package/lib/addon-fit.js`, `package/LICENSE`)
- **Tarball integrity (npm):** xterm `sha512-TQwDdQGtwwDt+2cgKDLn0IRaSxYu1tSUjgKarSDkUM0ZNiSRXFpjxEsvc/Zgc5kq5omJ+V0a8/kIM2WD3sMOYg==`;
  addon-fit `sha512-jYcgT6xtVYhnhgxh3QgYDnnNMYTcf8ElbxxFzX0IZo+vabQqSPAjC3c1wJrKB5E19VwQei89QCiZZP86DCPF7g==`
- **SRI (sha384):** `xterm.js` `sha384-f/1U6Z9wM4D71a5eRXEZnyOTMOvjqxr2XLwh+Go1OvIl3L3tOcvUrzudnhbECwl4`;
  `xterm.css` `sha384-n2n7twoohnW+d3myBKaUgl7DSiwidw6MkQy9oesGzkPpMjejKRR3XlnD+5yCdtBD`;
  `addon-fit.js` `sha384-txoiwu4RR2GD3qySbaj+BbzibkLbSJRcfqGYMu6z1EqHil4A2dyBiBW5dlacG6OR`
- **Cross-checked:** jsDelivr and unpkg serve the same bytes. (The old
  `lib/xterm.min.js` / `css/xterm.min.css` CDN paths never existed in the npm
  package: jsDelivr minified them on the fly, so they could not be SRI-pinned.
  `lib/xterm.js` is already minified by the xterm build.)
- **License:** MIT (`xterm/LICENSE`, `xterm-addon-fit/LICENSE`).
- **Use:** UMD globals `window.Terminal` and `window.FitAddon.FitAddon`
  (`Portal/modules/cli-agents-modal.js`, the tmux backend's terminal). The fit
  addon reaches into xterm internals, so the two always move together.
  `tests/web/cli-terminal.test.mjs` drives the modal end to end (create, fit,
  ANSI write, keystrokes, resize) on these bytes.

| File | sha256 |
|---|---|
| `xterm/xterm.js` | `14903579ff54664cd72f8e8699e6961a6272c21863ec1c3b118cdc8af5d4a972` |
| `xterm/xterm.css` | `854a7c0fb70e8b1a083c16797ab827299fb18744f5ad34f227b48337e33293c6` |
| `xterm/LICENSE` | `b569f629d00f2626a8100df2a1798210535621e42164dfd426a6fe5aac7b0ccd` |
| `xterm-addon-fit/addon-fit.js` | `ba3ea256ce0620a0992a197d6c9baea64823fc93d8da07a9e366ca9943c18527` |
| `xterm-addon-fit/LICENSE` | `e256f01188af527e4d06d21d06fbf785ae9c50d4b328bf03cbe0ba7f0aa4228f` |

## noVNC 1.7.0

See [`novnc/VENDORED.md`](novnc/VENDORED.md): source, the private
`_sendMouse` seam the CU viewer depends on, and a sha256 row for every file.
