# Vendored noVNC (pinned)

- **Origin:** https://github.com/novnc/noVNC
- **Tag:** `v1.7.0` (pinned; tag commit `63107bd06d9e1f6136ff21aeda8cd62cbf0d433e`,
  released 2026-04-28)
- **Source URL:** https://github.com/novnc/noVNC/archive/refs/tags/v1.7.0.tar.gz
  (`core/`, `vendor/pako/`, `LICENSE.txt`)
- **Cross-checked:** byte-identical to the same paths in the npm release
  `@novnc/novnc@1.7.0` (`https://registry.npmjs.org/@novnc/novnc/-/novnc-1.7.0.tgz`,
  integrity `sha512-ucEJOx4T2avIRCleodk7YobZj5O2Ga2AeLfQ69A/yjG9HHba2+PDgwSkN3FttrmG+70ZGx21sElNFouK13RzyA==`),
  which `tests/web/package.json` pins; `tests/web/vendor-pins.test.mjs` compares
  every file and the checksums below.
- **Vendored:** 2026-07-23 at v1.5.0 (CU live-view revamp, design doc
  `docs/plans/2026-07-23-cu-live-view-splashtop-design.md` §10-D3); re-vendored
  2026-10-25 at v1.7.0 (audit P07-html-css-16, rank 73).
- **Checked upstream on:** 2026-10-25 (v1.7.0 is the latest release).
- **License:** MPL-2.0 (see `LICENSE.txt`, copied from the upstream repo);
  `vendor/pako/` is MIT (`vendor/pako/LICENSE`).

## What is vendored (and why only this)

Only the RFB client module and its single dependency closure:

- `core/` — the ES-module RFB client (`core/rfb.js` is the entry point the
  CU viewer imports as `/cu/novnc/core/rfb.js`)
- `vendor/pako/` — the only `core/` dependency outside `core/` itself
  (`core/inflator.js` / `core/deflator.js` import
  `../vendor/pako/lib/zlib/*.js`)

The upstream app shell (`app/`, `vnc.html`, tests, docs, po, snap, etc.) is
deliberately NOT vendored — the BlackBox viewer page constructs `RFB`
directly.

## Why vendored at all (D3)

Fresh-box robustness: apt `novnc` is SHOULD_HAVE and demonstrably missing on
dev boxes, and apt ships 1.3.0 whose RFB module lacks the 1.5+ touch/gesture
affordances. The Orchestrator mounts this directory at `/cu/novnc`
(`Orchestrator/app.py`), falling back to `/usr/share/novnc` only when this
copy is absent.

## The private seam the viewer uses

noVNC has no public "send this pointer event" call, and the viewer drives the
remote pointer from its own gesture layer, so it calls the internal
`RFB#_sendMouse(x, y, mask)`. Every use goes through
`Portal/cu-view/rfb-input.js`, and `tests/web/novnc-rfb-seam.test.mjs` checks
the method against these exact bytes (a real RFB handshake over a fake
socket, then the PointerEvent bytes on the wire). A future re-vendor that
renames or changes it fails that test instead of leaving the viewer
input-dead.

What changed for the viewer between 1.5.0 and 1.7.0: `_sendMouse` keeps its
signature and gating (connected, not viewOnly). 1.6 added the back/forward
mouse buttons, so the standard PointerEvent's button byte is now masked to 7
bits, buttons 8+ go out as ExtendedPointerEvent, and bit 15 throws; the viewer
only sends buttons 1-7 (masks up to 64). 1.6 also added the H.264 (WebCodecs,
used only when the VNC server offers it and the browser can decode;
`VideoDecoder` exists only in secure contexts, so plain-http LAN viewers skip
it) and Zlib encodings. `core/util/browser.js` probes H.264 with a top-level
await (every supported browser and the Android WebView have it). 1.7 removed
the app's deprecated dot-cursor SETTING; the library's `showDotCursor`
property the viewer sets is unchanged.

## Updating

1. Download `https://github.com/novnc/noVNC/archive/refs/tags/v<x>.tar.gz` and
   the npm tarball `@novnc/novnc@<x>`; confirm `core/` and `vendor/pako/` are
   identical in both.
2. Replace `core/`, `vendor/pako/` and `LICENSE.txt` here with the tag's copy
   (delete the old trees first, so removed upstream files go too).
3. Update the tag, commit, dates and the checksum table below, and the
   `@novnc/novnc` pin in `tests/web/package.json` (then `npm install` there).
4. Re-read `Portal/cu-view/rfb-input.js` against the new `core/rfb.js`
   (`_sendMouse`, `RFB.messages.pointerEvent`), then
   `cd tests/web && node --test` and the `Portal/cu-view/*.test.mjs` suites.
   Never hand-edit files in this tree.

## Checksums

Every file in this directory except this one (`tests/web/vendor-pins.test.mjs`
fails when a row is missing, stale or wrong).

| File | sha256 |
|---|---|
| `LICENSE.txt` | `3ccd2242d7f5b6c5ae831a486f63cce1f18d8cd11c511f6a7a7251673763b2d1` |
| `core/base64.js` | `76d6b7ec73aed8cbc72b49b8b865bcb334acc59a6b0e96b19bc39c9e8e053ae3` |
| `core/crypto/aes.js` | `00b9a69185f913b882270c4cc7a076b4dd3a083981edb91c1e55db22f8ffcef3` |
| `core/crypto/bigint.js` | `e7880d27af8bf439579f6fc40d7b81fd379a9babb1d13c83999effd8c64823ab` |
| `core/crypto/crypto.js` | `e6006697ce1d195c951ef1e986172d17174e533909df19e07be583ac75dd344b` |
| `core/crypto/des.js` | `91d8c8ed596d2beda7da9dd68c9ae1d325c673822e3ccfc8a4091e121c99a316` |
| `core/crypto/dh.js` | `9102454282ba646ee4a4ce34c5db5d2a9979179f9d4606737fc6bb7a669e5ebe` |
| `core/crypto/md5.js` | `364c0f0d44d3f8fd608c18a0e1253e5a77e9b8e4c48e3c6503a0f45c9bb2ff8d` |
| `core/crypto/rsa.js` | `83875db9e5d978ea0eb2a1e2af8afbb7dedfb0f18eca2bab75c2756285037476` |
| `core/decoders/copyrect.js` | `da3b571e10984438ea49559738e11034b5d0c85154a68eac1a6bf9ef10e5aa02` |
| `core/decoders/h264.js` | `96d47901c5e26dc02256b152aa642490d936bbcc395f59ffd580356118edff84` |
| `core/decoders/hextile.js` | `63eb86247bea83119af75f3d145a4515a2411ff28a72492d2d7b925268d50163` |
| `core/decoders/jpeg.js` | `6747ac900d09a5899c3a6fff897e58e88607e69b39ff4a6bddddee2e9e6bc6b4` |
| `core/decoders/raw.js` | `bc7d4517213dc1df8626e8198eadf32b2c1946f75228b31387d3916963af0727` |
| `core/decoders/rre.js` | `b3380dbc75310769ef24e956e1fa1f13d8217f8b1d7d6f5c9d08db9a336a4c0d` |
| `core/decoders/tight.js` | `a7f01df99922be812ea21c5e46597b76379a5d8267f99ec8bea9a202bec34130` |
| `core/decoders/tightpng.js` | `38bd2fd200f5c8e9b0905eca2389fb8e6ad2151de886e4fa9a9c59cc1c7696f0` |
| `core/decoders/zlib.js` | `1d1c05cdca4840ff46082103ab35aa7696b41504bec91ebd74fae9e0e5f61380` |
| `core/decoders/zrle.js` | `6853c115f97a1d4de39ca71ab8b519a05916bb8c173aa8112ce4881cbb612cbd` |
| `core/deflator.js` | `ceb6763eb3fcc53f34d08534a8d1df9b63af3a95c58f8d96d238ef5ce639ad1f` |
| `core/display.js` | `10d298551124a172c97dcb54d8d30fca416e6038b064c93ba5f7cf079fa4bf1f` |
| `core/encodings.js` | `4975904fe1da6dc22b53ecb8462ec7c9a43e99db72dba4aab9f625c4177e355d` |
| `core/inflator.js` | `b1955cbd1608619911b8dfd451fae07f38c5767098f6f47e4a2a1682c11feb65` |
| `core/input/domkeytable.js` | `5eb985e1a412d1be6b606ba0d7e912fc0842d873d252bd7ec8a363670dfc800c` |
| `core/input/fixedkeys.js` | `6bb21b0a2d5f5d7ad73e75b80ddc1d844ac770c4426b312d5740f2443455f0c0` |
| `core/input/gesturehandler.js` | `c04b67fdae6ac061c7d51cb5ee8ca296ee9c7c80aa246cee5751e74eefe61758` |
| `core/input/keyboard.js` | `46f82adef772b726ae3db144fb9c12b3d03374b7c803b8a5106e422a10ddcdba` |
| `core/input/keysym.js` | `4ddf0e21f07328358974d91aa2d7f839ba2116688b28e43d3f6f4e2eae8c45cf` |
| `core/input/keysymdef.js` | `76f338d45fb73b7decb7d8b4ef6e95edf7ce9583722c5220642a9be85fc2cc89` |
| `core/input/util.js` | `2e156ea656404afcb6cc54dd85cbe5dd2dd0ba209ae9a6fb1b78ec5db17d01ce` |
| `core/input/vkeys.js` | `9a1ce4393e1aa4ace5dcdcf7e032f4a1c03a9cb964b4e0e7b7fcffd3a982eb84` |
| `core/input/xtscancodes.js` | `f0fc982627e1c02feaac1bd50d7fdd3bcbb05f0062b910d8002f47d1b08967a6` |
| `core/ra2.js` | `4aeffb60af9c163365170e2af392acb6f0a7c21a0cb6827f85dd776d33277bee` |
| `core/rfb.js` | `c46dfefbf869eda0c6d06c5dd86c42e96fc8a77c125e91599a2cdf3841261564` |
| `core/util/browser.js` | `592f56dc2195331c3b8329f8e51fa3a444a89ac85d5fa29e2364508aa9303df7` |
| `core/util/cursor.js` | `adabe605d73f38e5f29abd302489d4b9e98a9e495823a06a2edbe7f6744856fa` |
| `core/util/element.js` | `13845321fc5abccc1de25594082d8db15405af56c5f4fc32b3bdd0878bd891e3` |
| `core/util/events.js` | `ec7f4da0407ea67f5d6f5ce53bd9b3342155feb9407707de3992c5261e460ead` |
| `core/util/eventtarget.js` | `9481965aa6cee36ae2decdccc6ca5203c9371a8afc33346ad4170e471766e0da` |
| `core/util/int.js` | `b1c4b7dec2e7767322b3464f3c186b13138cdf341c621db44ddd06ce78180c01` |
| `core/util/logging.js` | `c3a49715c9f908d5bb385de5787e99b824faf34d26036d993eea4740bac9d482` |
| `core/util/strings.js` | `77d0604c7bf9e95de780f8cb9764d918ae24c86811a0a1c5b08cd30e50ad2a3a` |
| `core/websock.js` | `d1a96fc1316eaab9badcba9b0f1a7a55d748a4d4d26dc0263cc179dba2131cf4` |
| `vendor/pako/LICENSE` | `3bc404ffa7888053253eedcc5a667619aa08dc1be0bea400e5ff28c51602180f` |
| `vendor/pako/README.md` | `f78ded7db9ffa4be474ce75020659f165d97adc4276ef5c7fcd698a3aa03e6ce` |
| `vendor/pako/lib/utils/common.js` | `0749bc4661ed186714b90e7f2321c9625a8ce7c680563bfe3ac6df7aa332bf28` |
| `vendor/pako/lib/zlib/adler32.js` | `7412dd3ecc015888b99292f45e4d4ba69abc77f208508c9a1c93e8dfe54ba6cd` |
| `vendor/pako/lib/zlib/constants.js` | `9d2c0dd36349e6eec9d41a37f965b2ff7e7374fbf00fb139e3116d428b551898` |
| `vendor/pako/lib/zlib/crc32.js` | `897fa9eabd2bb239ec632326dbff85bc72cc81d4da80265cde59520db339f405` |
| `vendor/pako/lib/zlib/deflate.js` | `d2a498ad04bb5d1f67ba01696c31a2e88e8b88e4fc6c36c635294d1c445b22c1` |
| `vendor/pako/lib/zlib/gzheader.js` | `9f9b2da72593769fcc97d397ceb771bb11d5b96f202759e2ed014c2be24d40d7` |
| `vendor/pako/lib/zlib/inffast.js` | `ec1e0f7abeec681a0d5e3da46d06f499969cbf265eaaa5e21a135d2a9b878c16` |
| `vendor/pako/lib/zlib/inflate.js` | `321582ddc4023e47aa981a71caed0e1dad8b58f0d9e93a0b0a34fe7e3c45e05f` |
| `vendor/pako/lib/zlib/inftrees.js` | `94780cbdef562bcd0434c7e6f4faca1ff6be6980bf7305a200809d98d419d9c7` |
| `vendor/pako/lib/zlib/messages.js` | `7860a8d3c873c28e4f5f92db9ca3e45006e21b084210d87b8b7775cddccf05ed` |
| `vendor/pako/lib/zlib/trees.js` | `ad9cb5245b9e39edd164234d6ca19febb9dba683f1f99a67af48d62931d91426` |
| `vendor/pako/lib/zlib/zstream.js` | `240917347c379b1ad33b8bf07cc6981557173bc3408f35e9875866a14b9967b2` |
