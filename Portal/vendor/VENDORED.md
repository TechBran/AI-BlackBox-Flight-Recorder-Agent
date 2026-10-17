# Vendored third-party code (Portal/vendor/)

Everything under `Portal/vendor/` is upstream code copied byte-for-byte and
served by the Orchestrator at `/ui/vendor/...`. Never hand-edit these files:
re-download, re-verify, and update the table below. Each vendored package
keeps its upstream license file next to it.

| Package | Version | Files | Vendored |
|---|---|---|---|
| DOMPurify | 3.4.17 | `dompurify/purify.min.js`, `dompurify/LICENSE`, `dompurify/LICENSE-MPL` | 2026-10-17 |
| noVNC | 1.5.0 | see [`novnc/VENDORED.md`](novnc/VENDORED.md) | 2026-07-23 |

## DOMPurify 3.4.17

- **Origin:** https://github.com/cure53/DOMPurify (tag `3.4.17`, released 2026-10-12)
- **Source URL:** https://registry.npmjs.org/dompurify/-/dompurify-3.4.17.tgz
  (file `package/dist/purify.min.js`)
- **Tarball integrity (npm):** `sha512-B/pSlYVHQ58kqISD/yDjuJ1yfzI34m1gF4lAod2bwSRhiN4K5Cl3H8Af7hqB2PmTL911tdEudnnXn5FnhRoz0w==`
- **`purify.min.js` sha256:** `cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31`
- **`purify.min.js` SRI (sha384):** `sha384-P0JNneQf81HOl9VwNXK76cQ9VMD6Nfsnepxb0cCOg8+UzEdzgPHAPV4wVQrvYQSx`
  (the `integrity=` on the `<script>` tag in `Portal/index.html`)
- **Cross-checked:** the same sha256 from three independent sources — the npm
  tarball, `https://raw.githubusercontent.com/cure53/DOMPurify/3.4.17/dist/purify.min.js`
  and `https://cdn.jsdelivr.net/npm/dompurify@3.4.17/dist/purify.min.js`.
- **License:** MPL-2.0 OR Apache-2.0 (`dompurify/LICENSE`, `dompurify/LICENSE-MPL`,
  copied from the tarball).
- **Not vendored:** `purify.min.js.map` (DevTools only).
- **Why vendored:** it is the only thing standing between model output and
  innerHTML (`Portal/modules/sanitize.js`). Loading it from a CDN meant a
  LAN-only or offline box ran without it, and the 3.0.6 pin had published
  bypasses (CVE-2024-45801, CVE-2024-47875, CVE-2025-26791). Audit ids
  P01-chat-core-web-03, P07-html-css-04, X05-architecture-02.

### Updating DOMPurify

1. Download `https://registry.npmjs.org/dompurify/-/dompurify-<v>.tgz` and check
   its sha512 against `npm view dompurify@<v> dist.integrity`.
2. Copy `package/dist/purify.min.js`, `package/LICENSE` and
   `package/LICENSE-MPL` into `dompurify/`; confirm the file matches the GitHub
   tag and jsDelivr copies.
3. Update this file (version, URLs, sha256, sha384, date), the `integrity=` in
   `Portal/index.html`, and the `dompurify` pin in `tests/web/package.json`
   (then `npm install` there to refresh `package-lock.json`).
4. `cd tests/web && node --test` — `vendor-pins.test.mjs` fails if any of the
   four disagree, and the sanitizer suites run against the new bytes.

## CDN libraries pinned by SRI (not vendored yet)

`Portal/index.html` still loads these from public CDNs; each tag carries
`integrity=` + `crossorigin="anonymous"`, so a changed byte is refused by the
browser (and the markdown renderer then fails closed to escaped text).
Vendoring them is a later step (audit rank 73).

| Library | URL | SRI (sha384) |
|---|---|---|
| marked 11.1.1 | `https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js` | `sha384-zbcZAIxlvJtNE3Dp5nxLXdXtXyxwOdnILY1TDPVmKFhl4r4nSUG1r8bcFXGVa4Te` |
| highlight.js 11.9.0 | `https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js` | `sha384-F/bZzf7p3Joyp5psL90p/p89AZJsndkSoGwRpXcZhleCWhd8SnRuoYo4d0yirjJp` |
| highlight.js 11.9.0 theme | `https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css` | `sha384-oaMLBGEzBOJx3UHwac0cVndtX5fxGQIfnAeFZ35RTgqPcYlbprH9o9PUV/F8Le07` |

The marked file is byte-identical to `package/marked.min.js` in the npm tarball
(the `marked` pin in `tests/web/package.json`, which the tests hash); the two
highlight.js files match the sha512 values cdnjs publishes for 11.9.0.

xterm.js (`@xterm/xterm@5.5.0`, `@xterm/addon-fit@0.10.0`) is deliberately NOT
SRI-pinned: those `.min.js`/`.min.css` paths do not exist in the npm packages
and are minified on the fly by jsDelivr, so their bytes are not guaranteed
stable. Vendor them instead (rank 73).
