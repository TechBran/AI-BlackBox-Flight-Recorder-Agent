# TG200 / Asterisk Telephony — Production Pass Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (fresh subagent per task, spec review then code review between tasks).

**Goal:** Make the Yeastar NeoGate TG telephony stack production-quality and multi-tenant — any TG100/200/400/800 with multiple SIMs/numbers, configured through the Portal (no terminal), SMS + voice working, credentials safe, security gates preserved.

**Architecture:** `gateways.json` becomes the single source of truth (model, per-gateway `http`+`ami` creds, `ports[]`). A per-gateway `AMIConnectionManager` replaces the SMS singleton. Status comes from AMI `gsm show spans` (the device has no REST API). Provisioning auto-configures *your* Asterisk via an install-time `blackbox.d` include + `ReadWritePaths` hole + scoped sudoers reload, while the TG side is a guided, live-validated click-through. Routing becomes line-aware with the contact-book (SMS) and IVR-PIN (calls) gates kept absolute.

**Tech Stack:** Python 3 / FastAPI / aiohttp / asyncio, SQLite, Asterisk ARI+AMI, vanilla-JS Portal modules, systemd, `install.sh`.

**Reference:** `docs/plans/2026-06-07-tg200-production-pass-design.md` (audit, device truth, gap analysis, security invariants).

### Global rules for every task
- **Prod runs live from the working tree.** Every commit MUST pass `Orchestrator/venv/bin/python -c "import Orchestrator.app"` before committing.
- **Never `git add -A`/`.`** Stage explicit paths only. Untracked local files (`Portal/zellij-iframe-smoke.html`, `blackbox-status.sh`, `scripts/generate_sarah_pdf.py`) must never be committed.
- **Never hardcode** the operator (resolve via `USERS_LIST`/`get_current_operator`) or model literals (use `config.py`).
- **Security invariants are LOCKED** (see design §3): SMS = contact-book whitelist pre-API (no match → drop, zero tokens); calls = IVR PIN; line→operator scoping only tightens. Any task touching routing MUST include tests proving an unknown sender is dropped before any task/`/chat` call.
- Run tests with `Orchestrator/venv/bin/python -m pytest <path> -v`.
- Secret-scan diffs before any push.

---

## Phase 0 — Security & foundation (no behavior change)

### Task 0.1: Stop tracking gateways.json; ship an example template

**Files:**
- Modify: `.gitignore`
- Create: `Orchestrator/asterisk/gateways.example.json`
- Git: `git rm --cached Orchestrator/asterisk/gateways.json`

**Step 1:** Append to `.gitignore` under the SECRETS section:
```
# Telephony gateway configs hold per-device credentials (encrypted, but never commit)
Orchestrator/asterisk/gateways.json
```
**Step 2:** `git rm --cached Orchestrator/asterisk/gateways.json` (keep the working file).
**Step 3:** Write `gateways.example.json` — one placeholder gateway with the v2 shape (no real secrets), `http.password` and `ami.secret` set to `"CHANGE_ME"`.
**Step 4:** Verify: `git check-ignore Orchestrator/asterisk/gateways.json` prints the path; `git status` shows `gateways.json` gone from tracking, example added.
**Step 5:** Commit `.gitignore`, the example, and the removal.

### Task 0.2: Secrets-at-rest helper (encrypt/decrypt gateway fields)

**Files:**
- Create: `Orchestrator/asterisk/secrets.py`
- Test: `Orchestrator/asterisk/tests/test_secrets.py`
- Modify: `Orchestrator/config.py` (add `TELEPHONY_SECRET_KEY = os.getenv("TELEPHONY_SECRET_KEY", "")`)
- Modify: `.env.template` (document `TELEPHONY_SECRET_KEY`)

**Step 1: Failing test** `test_secrets.py`:
```python
from Orchestrator.asterisk import secrets

def test_roundtrip():
    enc = secrets.encrypt("<REDACTED-SECRET>")
    assert enc != "<REDACTED-SECRET>"
    assert enc.startswith("enc:")
    assert secrets.decrypt(enc) == "<REDACTED-SECRET>"

def test_plaintext_passthrough_decrypt():
    # legacy/plaintext values decrypt to themselves (migration tolerance)
    assert secrets.decrypt("plain") == "plain"

def test_mask():
    assert secrets.mask("anything") is True
    assert secrets.mask("") is False
```
**Step 2:** Run → fails (module missing).
**Step 3: Implement** `secrets.py`: Fernet-style symmetric encryption keyed off `TELEPHONY_SECRET_KEY` (derive a 32-byte key via SHA-256 if the env value isn't a valid Fernet key; if `cryptography` isn't available, fall back to an HMAC-keyed reversible scheme — but prefer `cryptography` which is already a transitive dep; verify with `Orchestrator/venv/bin/pip show cryptography`). API: `encrypt(s)->"enc:..."`, `decrypt(s)->s` (passthrough if not `enc:`-prefixed), `mask(s)->bool`.
**Step 4:** Run tests → pass.
**Step 5:** Commit (`secrets.py`, test, `config.py`, `.env.template`).

### Task 0.3: Externalize AMI creds (remove hardcoded secret) — stopgap before the manager

**Files:**
- Modify: `Orchestrator/config.py` — add `ASTERISK_AMI_HOST/PORT/USER/SECRET` env vars (defaults: host from first gateway or `""`, port 5038, user `""`, secret `""`).
- Modify: `Orchestrator/sms/ami_client.py` — change `__init__` defaults to `host=""`, `username=""`, `secret=""` (NO literal secret); raise/log clearly if unset.
- Modify: `Orchestrator/sms/__init__.py` — `start_sms_system` reads creds from `config` (still singleton for now).
- Test: `Orchestrator/sms/tests/test_ami_client_config.py`

**Step 1: Failing test** — assert `AMISMSClient()` has empty defaults (no `<REDACTED-SECRET>` anywhere in the module): grep-style test reading the source ensures the literal is gone; and `AMISMSClient(host="h", username="u", secret="s")` stores them.
**Step 2:** Run → fails.
**Step 3:** Implement: strip the hardcoded literal; `sms/__init__.py` builds `AMISMSClient(host=ASTERISK_AMI_HOST, port=ASTERISK_AMI_PORT, username=ASTERISK_AMI_USER, secret=ASTERISK_AMI_SECRET)`.
**Step 4:** Run tests → pass. Confirm `grep -rn "<REDACTED-SECRET>" Orchestrator/` returns nothing.
**Step 5: Import gate** then commit.

> NOTE: The real per-gateway creds arrive in Phase 2; this task removes the committed secret immediately and gives a config path. Operator action: set the new AMI user/secret on the NeoGate and in `.env`/`gateways.json`, rotating `<REDACTED-SECRET>`.

---

## Phase 1 — Data model & migration

### Task 1.1: Gateway schema v2 + migration upgrader

**Files:**
- Modify: `Orchestrator/asterisk/gateway_manager.py` (`_new_gateway`, add `MODEL_PORTS`, `migrate_gateway`, call it in `load_gateways`)
- Test: `Orchestrator/asterisk/tests/test_gateway_schema.py`

**Step 1: Failing tests:**
```python
from Orchestrator.asterisk import gateway_manager as gm

def test_new_gateway_v2_shape():
    gw = gm._new_gateway(name="Office", ip="10.0.0.5", model="TG400")
    assert gw["model"] == "TG400"
    assert gw["http"]["user"] and "password" in gw["http"]
    assert "ami" in gw and gw["ami"]["port"] == 5038
    assert len(gw["ports"]) == 4  # TG400 = 4 ports
    assert {p["span"] for p in gw["ports"]} == {2, 3, 4, 5}

def test_migrate_legacy_record():
    legacy = {"id":"x","name":"old","ip":"1.2.3.4","http_user":"admin",
              "http_password":"password","capacity":2,"phone_numbers":["+15551112222"],
              "trunk_name":"tg200","enabled":True}
    new = gm.migrate_gateway(legacy)
    assert new["model"] == "TG200"
    assert new["http"]["user"] == "admin"
    assert new["ami"]["port"] == 5038
    assert len(new["ports"]) == 2
    assert new["ports"][0]["phone_number"] == "+15551112222"
```
**Step 2:** Run → fail.
**Step 3:** Implement: `MODEL_PORTS = {"TG100":1,"TG200":2,"TG400":4,"TG800":8}`; spans start at 2 (`span = 2 + slot`). `migrate_gateway` maps `capacity→model`, `http_user/http_password→http{}`, derives `ami{}` from `config.ASTERISK_AMI_*` (one-time), distributes `phone_numbers[]` across `ports[]`. `load_gateways` runs `migrate_gateway` on each record and re-saves if changed (idempotent).
**Step 4:** Run → pass.
**Step 5:** Import gate, commit.

### Task 1.2: Span/model helper module

**Files:**
- Modify: `Orchestrator/asterisk/gateway_manager.py` — `spans_for_model(model)`, `slot_to_span(slot)`, `port_count(model)`.
- Test: extend `test_gateway_schema.py`.

**Steps:** TDD `spans_for_model("TG800") == [2,3,4,5,6,7,8,9]`, `slot_to_span(0)==2`. Implement, test, import gate, commit.

### Task 1.3: Encrypt-on-save / decrypt-on-use / mask-on-GET wiring

**Files:**
- Modify: `Orchestrator/asterisk/gateway_manager.py` — `save_gateways` encrypts `http.password`+`ami.secret` (via `secrets.encrypt`) if not already `enc:`; `get_gateway_decrypted(id)` returns a copy with secrets decrypted (for runtime use); add `redact_gateway(gw)` returning secrets replaced by `has_password`/`has_secret` booleans.
- Modify: `Orchestrator/routes/asterisk_routes.py` — GET endpoints return `redact_gateway(...)`; PUT/POST only overwrite a secret when a non-empty value is supplied (send-on-change).
- Test: `test_gateway_secrets_roundtrip.py`

**Steps:** TDD: save encrypts; `get_gateway_decrypted` yields plaintext; `redact_gateway` exposes no secret + booleans; PUT with empty secret leaves the stored one intact. Implement, test, import gate, commit.

---

## Phase 2 — Per-gateway AMI runtime

### Task 2.1: AMIConnectionManager

**Files:**
- Create: `Orchestrator/sms/manager.py` (`AMIConnectionManager`)
- Test: `Orchestrator/sms/tests/test_manager.py` (inject a FakeAMIClient)

**Contract:** `add_gateway(gw)`/`remove_gateway(id)`/`reconnect(id)`; `get(gateway_id)->client|None`; `clients()->dict`; `default()->client|None` (first enabled); `resolve_for_number(from_number)->(client, span)|None`. Constructs each client from `get_gateway_decrypted`. Idempotent; survives a client connect failure (logs, keeps others).

**Steps:** TDD with a fake client (no real sockets): adding two gateways yields two clients; remove disconnects+drops; `default()` returns first enabled; `resolve_for_number` maps a `ports[].phone_number` to its client+span. Implement, test, import gate, commit.

### Task 2.2: Swap the singleton for the manager

**Files:**
- Modify: `Orchestrator/sms/__init__.py` — `start_sms_system` builds `AMIConnectionManager` from `load_gateways()`, connects all enabled; `get_ami_client(gateway_id=None)` returns the per-gateway client (or `default()` when `None`, preserving existing callers); add `get_manager()`.
- Modify: `Orchestrator/asterisk/gateway_manager.py` `send_sms_via_gateway` → use `get_ami_client(gateway["id"])` + `slot_to_span`.
- Test: `test_sms_init_manager.py` (monkeypatch `load_gateways` + fake clients).

**Steps:** TDD backward-compat (`get_ami_client()` still returns a client), per-gateway resolution, span via model table. Implement, **import gate**, commit.

### Task 2.3: SMSRouter binds across all gateways

**Files:**
- Modify: `Orchestrator/sms/router.py` — accept the manager; register `on_sms` on every client; `handle_incoming` receives `gateway_id` + `span`; `send_manual`/reply select client + span by gateway/line.
- Modify: `Orchestrator/sms/ami_client.py` — `on_sms` callback signature gains `gateway_id` (pass the owning id; clients are constructed with their id).
- Test: extend `test_manager.py` / new `test_router_multi.py`.

**Steps:** TDD: an inbound event on gateway A's client routes with A's id+span; reply goes via A. Implement, import gate, commit.

---

## Phase 3 — Number/SIM-aware routing (security-critical)

### Task 3.1: message_store line columns + migration

**Files:**
- Modify: `Orchestrator/sms/message_store.py` — add `line_number TEXT DEFAULT ''`, `gateway_id TEXT DEFAULT ''`; idempotent migration via `pragma table_info`; `store_message(..., line_number='', gateway_id='')`; thread queries group by `(operator, line_number, phone_number)`; `get_recent_threads`/`get_conversation` accept optional `line_number`.
- Test: `Orchestrator/sms/tests/test_message_store_lines.py` (temp DB).

**Steps:** TDD: fresh DB has new columns; a legacy DB (old schema) migrates without data loss; threads scoped by line. Implement, import gate, commit.

### Task 3.2: Line-aware inbound routing with LOCKED whitelist

**Files:**
- Modify: `Orchestrator/sms/router.py` — `handle_incoming(sender, body, span, recvtime, gateway_id)`: resolve `(gateway_id, span)→line_number+owner`; if owner set → match `sender` against owner's book only; else search all books; **no match → drop before any task/chat** (preserve `return`).
- Test: `Orchestrator/sms/tests/test_router_security.py`

**Step 1: Failing tests (the invariants):**
```python
# unknown sender on any line → dropped, _route_through_chat NEVER called
# sender in owner's book → routed to owner
# sender NOT in owner's book but in another's → dropped (line scoping tightens)
# line with no owner + sender in some book → routed; + sender in none → dropped
```
Use a router with a stubbed `_route_through_chat` (asserts call count) + fake contacts.
**Step 2:** Run → fail.
**Step 3:** Implement line resolution + scoped match; keep the pre-API drop.
**Step 4:** Run → pass (especially the "chat never called for unknown sender" assertions).
**Step 5:** Import gate, commit.

### Task 3.3: Outbound from-number selection

**Files:**
- Modify: `Orchestrator/sms/router.py` `send_manual(operator, to, message, from_number=None, gateway_id=None)` → resolve via manager; default first enabled.
- Modify: `Orchestrator/routes/sms_routes.py` `SMSSendRequest` gains optional `from_number`; `/asterisk/sms` already has `gateway_id`+`port` → route through the same manager path.
- Modify: `ToolVault/tools/send_sms/schema.json`+`executor.py` — add optional `from_number`; resolve via manager; default unchanged.
- Test: `test_outbound_selection.py`.

**Steps:** TDD default + explicit selection; reply-same-span. Implement, import gate, commit.

### Task 3.4: Converge SMS send paths + tool

**Files:**
- Modify: `Orchestrator/routes/asterisk_routes.py` — fix the docstring ("AMI", not "HTTP API"); `/asterisk/sms` delegates to `router.send_manual` (single code path).
- Test: `test_sms_endpoints_parity.py` (both endpoints hit the manager).

**Steps:** TDD, implement, import gate, commit.

---

## Phase 4 — Status/SIM via AMI (delete the REST fiction)

### Task 4.1: `gsm show spans` status

**Files:**
- Modify: `Orchestrator/sms/ami_client.py` — add `get_all_spans()` → run `gsm show spans` via `SMSCommand`, parse per-span `{span, carrier, signal, registered, phone_number}`.
- Modify: `Orchestrator/asterisk/gateway_manager.py` `check_gateway_status` — drop the `/api/v1.0/gsm` GET; pull SIM slots from `get_ami_client(id).get_all_spans()`; keep Boa-root HEAD reachability + ARI SIP-reg.
- Test: `Orchestrator/sms/tests/test_span_parse.py` with a captured `gsm show spans` sample string.

**Steps:** TDD the parser against realistic NeoGate output (capture live during build via the running AMI client if available). Implement, import gate, commit.

### Task 4.2: Discovery fingerprint → Boa/NeoGate

**Files:**
- Modify: `Orchestrator/asterisk/gateway_manager.py` `discover_gateways` — match `Server: Boa` header + body containing `neogate`/`astman`/`webcgi`; return dicts with `ip`, `model`, `mac` (best-effort).
- Test: extend discovery test with a fake Boa response.

**Steps:** TDD, implement, import gate, commit.

---

## Phase 5 — Provisioning automation

### Task 5.1: install.sh one-time Asterisk enablement

**Files:**
- Modify: `install.sh` — add an idempotent block (root, install-time):
  - `mkdir -p /etc/asterisk/blackbox.d && chown <svc_user> /etc/asterisk/blackbox.d`
  - ensure `#include "blackbox.d/*.conf"` present in `pjsip.conf` + `extensions.conf` (append once, guarded by grep)
  - drop `/etc/systemd/system/blackbox.service.d/asterisk.conf` with `ReadWritePaths=/etc/asterisk/blackbox.d`
  - install `/etc/sudoers.d/blackbox-asterisk` allowing the svc user `NOPASSWD: /usr/sbin/asterisk -rx *reload*` (validate with `visudo -c`)
  - `systemctl daemon-reload`
- Test: `scripts/tests/test_install_asterisk_block.sh` (bash; runs the block against a temp fake `/etc/asterisk` via env override; asserts include line added once on re-run).

**Steps:** Implement idempotently; document that this runs at install (NOT runtime — the orchestrator never writes sudoers, per memory). Commit. (No import gate — bash only; run the bash test.)

### Task 5.2: Asterisk config writer + reload

**Files:**
- Create: `Orchestrator/asterisk/provisioner.py` — `write_gateway_config(gw)` renders PJSIP endpoint/aor/identify (`generate_pjsip_trunk_config`, extended) + dialplan contexts (`from-tg200`/`to-tg200`/`blackbox-audiosocket`) into `/etc/asterisk/blackbox.d/<trunk>.conf`; `reload_asterisk()` runs `sudo asterisk -rx "pjsip reload"` + `"dialplan reload"`; `BLACKBOX_D = os.getenv("ASTERISK_INCLUDE_DIR", "/etc/asterisk/blackbox.d")` (override for tests).
- Test: `Orchestrator/asterisk/tests/test_provisioner.py` (point `ASTERISK_INCLUDE_DIR` at tmp; assert file content; monkeypatch reload subprocess).

**Steps:** TDD render + write (no real reload in tests). Implement, import gate, commit.

### Task 5.3: Wizard validation endpoints

**Files:**
- Modify: `Orchestrator/routes/asterisk_routes.py` — add `POST /asterisk/gateways/{id}/validate` returning `{reachable, ami_auth, spans:[...], trunk_online}`; `POST /asterisk/gateways/{id}/apply` → `provisioner.write_gateway_config` + `reload_asterisk` + manager `reconnect`; `POST /asterisk/gateways/{id}/test-sms` and `/test-call`.
- Test: `Orchestrator/tests/test_wizard_routes.py` (monkeypatch manager + provisioner).

**Steps:** TDD each endpoint's shape; apply triggers write+reload+reconnect. Implement, import gate, commit.

### Task 5.4: Config generators (TG-side instructions + our-side preview)

**Files:**
- Modify: `Orchestrator/routes/asterisk_routes.py` — `GET /asterisk/gateways/{id}/config-preview` → `{asterisk_conf, tg_steps:[...]}` (the copy-through text for the NeoGate GUI + a read-only preview of what apply writes).
- Test: extend wizard route test.

**Steps:** TDD, implement, import gate, commit.

---

## Phase 6 — UI

### Task 6.1: Fix live field-drift bugs

**Files:** `Portal/modules/telephony-manager.js`
- `renderDiscoveredGateways`: read `d.ip`/`d.model`/`d.mac`; dedupe on `g.ip`.
- `showEditForm`: pass `ip` (not `ip_address`); `buildFormHtml` reads `prefill.ip` consistently.
**Steps:** Fix; manual verify discovered cards show IP + edit form pre-fills IP. Commit. (No pytest; note manual verification in commit body.)

### Task 6.2: Gateway form — model, AMI creds, per-line table

**Files:** `Portal/modules/telephony-manager.js`, `Portal/index.html` (modal markup if needed), `Portal/styles/features/_telephony.css` (if present; else co-locate).
- Add **Model** `<select>` (TG100/200/400/800).
- Add **AMI** user + secret (`type=password`, placeholder = recommended default, send-on-change; never pre-filled — mirror `http_password`).
- Render a **per-line table** from `gw.ports[]` + live `status.sim_slots` (carrier, signal bars, editable phone number, operator `<select>` from `USERS_LIST`, enabled toggle).
- `handleFormSave` posts `model`, `http`, `ami` (only if entered), `ports`.
**Steps:** Implement; verify against a real gateway card. Commit.

### Task 6.3: Wizard stepper modal

**Files:** `Portal/modules/telephony-wizard.js` (new), `Portal/index.html`, CSS.
- Stepper: Identify → Validate (calls `/validate`, live green/red + signal) → Configure (calls `/config-preview`; "Apply" → `/apply`; copy buttons for `tg_steps`) → Done.
- On apply success, prompt restart only if the response asks for it.
**Steps:** Implement; verify end-to-end against the live TG200. Commit.

### Task 6.4: SMS UI line-aware

**Files:** Portal SMS module(s) (locate via `grep -rn "sms/threads\|/sms/messages" Portal/`).
- Thread list labeled by receiving line; compose adds a from-number `<select>` (default first enabled).
- Pass `from_number` to `/sms/send`; pass `line_number` filter to threads.
**Steps:** Implement; verify. Commit.

### Task 6.5: API hygiene sweep

**Files:** `Orchestrator/routes/asterisk_routes.py`, `sms_routes.py`.
- Ensure all gateway GETs return `redact_gateway`; remove any remaining secret leakage; final docstring corrections.
- Test: `test_no_secret_leak.py` — GET responses contain no `enc:`/plaintext secret, only `has_*` booleans.
**Steps:** TDD, implement, import gate, commit.

---

## Phase 7 — Finalize

### Task 7.1: .env.template telephony section
Document `ASTERISK_ENABLED`, `ASTERISK_ARI_*`, `ASTERISK_AMI_*`, `TELEPHONY_SECRET_KEY`, `ASTERISK_INCLUDE_DIR`, `PHONE_PIN_*`, with the recommended-default note. Commit.

### Task 7.2: Full regression + final review
- `Orchestrator/venv/bin/python -m pytest Orchestrator -q` (telephony suites green; whole suite no new failures).
- `import Orchestrator.app` gate.
- Restart the service (pre-authorized): `sudo systemctl restart blackbox.service`; confirm SMS manager connects + gateway cards show real SIM signal.
- Dispatch the final code-reviewer subagent over the whole branch.

### Task 7.3: Merge + snapshot
- Secret-scan the full diff (`git diff main... | grep -niE "secret|password|6157|api[_-]key"` → only masked/var refs).
- On user's go: merge to `main`, push, delete branch.
- `/snapshot-dev` documenting the production pass (search hints: "TG200 production pass", "NeoGate multi-gateway", "AMI per-gateway manager", "telephony wizard").

---

## Execution notes
- **Order matters:** Phase 0 removes the committed secret first; Phase 2 can't precede Phase 1 (needs the schema); Phase 6 UI follows the backend it drives.
- Capture a real `gsm show spans` sample early (Task 4.1) from the live AMI client to ground the parser.
- Keep each commit importable; restart only at Phase 7 (or when a task explicitly needs the manager live to capture device output).
