# Devices → Operator Picker — Production-Quality Uplift (Implementation Plan)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development) to implement this plan task-by-task.
> **Revised 2026-07-07** after a 5-lens adversarial red-team (23 agents). See `## Revision log` at the end for what changed and why.

**Goal:** Make the per-device "owner (operator)" assignment work end-to-end and portably: any operator can be (re)assigned to any device from both the Portal and the Android MVP, the state persists on the backend, and the whole thing installs cleanly on any machine using that machine's own operator roster — with no hardcoded "Brandon".

**Architecture:** The device→operator mapping (`device_registry.owner`) is a single backend source of truth persisted in `devices.json` and served via `GET /devices/mesh`. This uplift (a) stops discovery from stamping a hardcoded owner, (b) adds a first-class **unassign / re-home** path (backend route + registry primitive + both UIs), (c) makes the operator-validation roster **live** (no restart lag), (d) makes the state-file location portable, and (e) scopes/declutters the Android mesh list. It deliberately does **not** touch SMS or push-notification routing — those are separate subsystems (gateway-port operator + contact books; self-subscribe store) and stay where they are. The picker's copy is clarified to say what it actually governs (device-control ownership + AI device-context).

**Tech Stack:** FastAPI + Pydantic (backend), vanilla ES modules (Portal), Jetpack Compose + Kotlin + kotlinx.serialization + OkHttp (Android), pytest (backend tests), Gradle unit tests (Android).

---

## Scope decision (locked by operator, 2026-07-07)

Chosen option: **"Fix picker + clarify."** Repair the assignment pipeline and portability; relabel the picker to what it truly controls. **Per-operator SMS/notification routing is OUT OF SCOPE** (stays in the gateway-port + contact-book + subscription systems where it already lives). See `## Non-Goals`.

## Locked Decisions

1. **Discovery creates UNCLAIMED devices.** `sync_from_tailscale` (`registry.py:406`) and the ADB-pairing placeholder (`adb/manager.py:394`) set `owner=""` (never `"Brandon"`), matching the already-correct `_autoregister_from_tailnet` (`device_routes.py:135`) and create (`:326`) paths. This one change defuses both the "everything is Brandon" seed **and** the `sync-tailscale` poisoning (the manual **Sync** button *and* the CU-drawer auto-fire at `cu-drawer.js:357`, which fires only when Computer-Use is the active provider — there is no *unconditional* on-load re-poisoning).
2. **First-class unassign, not delete-and-recreate.** Add `POST /devices/{id}/unassign` + `registry.clear_owner()` (clears `owner` **and** `is_primary`). The 409 anti-accident guard on `assign_operator` stays; the UIs perform a **confirm → unassign → assign** re-home so cross-operator moves are explicit and logged, consistent with the cooperative-tailnet trust model (`tailscale_security_perimeter`). No app-layer auth added.
3. **Live operator roster.** `add_operator`/`remove_operator` mutate the shared `Orchestrator.config.USERS_LIST` **in place** (`USERS_LIST[:] = …`) instead of rebinding a per-module alias, so `GET /operators`, device-assign validation (`device_routes._live_operators`), and notification validation (`notification_routes`, top-level import) all agree with **no restart**. Verified by review: `config.USERS_LIST` is never rebound today; every importer holds the same list object by reference.
4. **Portable state file — default stays in-repo (systemd-safe).** `devices.json` becomes configurable via `BLACKBOX_DEVICE_REGISTRY_PATH`, **default byte-identical to today's location** (which the systemd unit already writes). Registry init `os.makedirs(parent, exist_ok=True)` and does a one-time backward-compatible load of the legacy package-dir file. **Constraint:** any override MUST point at a path the service's mount namespace can write (in-repo data dir, or add it to `ReadWritePaths`); a bare `~/…` path can be blocked by `ProtectHome`/`ProtectSystem` (see `mount_ns_bisect`, `protectsystem_strict_blast_radius`).
5. **Android reaches parity with the Portal:** operator-scoped mesh fetch + a hide-unassigned control (defaulting so fresh boxes stay claimable), a working (non-empty) operator picker, and an unassign/re-home affordance.
6. **Copy clarifies, doesn't lie.** Both surfaces state the picker governs *device control + AI device-context*, not text messaging. No functional SMS change.
7. **This box keeps its data.** On this dev box, `Brandon` ownership is correct, so no destructive migration runs here; the fixes make *fresh* boxes correct and give an *opt-in* `POST /devices/normalize` for cleanup (which is also what resolves this box's duplicate-IP Fold rows).

## File map

**Backend**
- `Orchestrator/device_registry/registry.py` — `sync_from_tailscale` owner default (`:406`), new `clear_owner()`, new-peer IP-collision handling in sync, `normalize()`, portable + `makedirs` `DEVICES_FILE` (`:23`).
- `Orchestrator/adb/manager.py:394` — ADB-pairing placeholder that hardcodes `owner="Brandon"`.
- `Orchestrator/routes/device_routes.py` — new `POST /{id}/unassign` and `POST /normalize` (both declared **before** `GET /{device_id}` at `:305`).
- `Orchestrator/config.py:140` — `USERS_LIST` (keep it a stable, mutated-in-place object).
- `Orchestrator/routes/admin_routes.py` — `add_operator` (`POST /operator/add`, def `:1068`, rebind `:1101`) / `remove_operator` (`DELETE /operator/{name}`, def `:1115`, rebind `:1148`) → mutate `config.USERS_LIST` in place + guard a dangling `USERS_DEFAULT`/`CURRENT_OPERATOR`.

**Portal**
- `Portal/modules/devices-section.js` — unassign sentinel + confirm-on-rehome, `d.owner`/`val` provenance, refresh-on-error, hide-unassigned, roster refetch on open.
- `Portal/index.html:456-466` — section copy/helptext + hide-unassigned control.
- `Portal/styles/features/_devices-section.css` — styles for the new controls.

**Android** (`…/app/src/main/java/com/aiblackbox/portal/`)
- `data/model/MeshDevice.kt:22-28` — already exposes `owner`/`isPrimary`/`isClaimed`; no model change (verified).
- `ui/devices/MeshDeviceViewModel.kt` — fix operator parse (`:81-94`), operator-scoped fetch + `_filter` state (`:68`), combined `rehome()` coroutine, error surfacing.
- `ui/devices/MeshDevicesSection.kt` — filter + hide-unassigned control row, unassign sentinel, confirm dialog, copy (`:110-114`).

**Tests**
- Extend `Orchestrator/tests/test_device_routes_mesh.py` (**keep** the 409-guard test), `test_device_registry_primary.py`, `test_resolve_device.py`.
- New: `test_device_unassign.py`, `test_device_registry_freshbox.py`, `test_operator_roster_live.py`, `test_device_prompt_context.py`.
- Android: `…/test/…/MeshDeviceViewModelTest.kt` (operator parse + scoped fetch).

---

# M1 — Backend: unclaimed discovery + unassign + live roster

### Task M1.1 — Discovery leaves devices UNCLAIMED
**Files:** `Orchestrator/device_registry/registry.py:406`; `Orchestrator/adb/manager.py:394`; test `test_device_registry_freshbox.py`.
- **Test isolation (do this in EVERY freshbox test):** `monkeypatch.setattr(registry_module, "DEVICES_FILE", tmp_path/"devices.json")`, reset the singleton (`registry_module._registry = None`), and mock the `tailscale status --json` subprocess. **Never** touch the live `devices.json`. (The env var alone won't retarget an already-imported module constant.)
- **Step 1 (test):** empty registry; feed mocked `tailscale status` with one peer; call `sync_from_tailscale()`; assert the created device has `owner == ""` and `is_primary is False`. Add an assertion that the *existing*-device update branch (`registry.py:389-397`) never rewrites a non-empty `owner`.
- **Step 2:** Run → FAIL (currently `"Brandon"`).
- **Step 3:** `registry.py:406` `owner="Brandon",  # Default owner` → `owner="",  # I2: discovery is UNCLAIMED — ownership is set only via POST /{id}/operator`. Same at `adb/manager.py:394`.
- **Step 4:** Run → PASS.
- **Step 5:** Commit `fix(devices): discovery creates unclaimed devices (no hardcoded Brandon owner)`.

### Task M1.2 — `registry.clear_owner()` primitive
**Files:** `registry.py`; `test_device_unassign.py`.
- **Step 1 (test):** device `owner="Brandon", is_primary=True`; `clear_owner(id)`; assert `owner == ""` **and** `is_primary is False`; reload from file → persisted.
- **Step 3 (impl):**
  ```python
  def clear_owner(self, device_id: str) -> Optional[Device]:
      """Unclaim a device: blank its owner AND demote it as primary (a primary
      must always have an owner), then persist. Returns the device or None."""
      with self._lock:
          d = self._devices.get(device_id)
          if not d:
              return None
          d.owner = ""
          d.is_primary = False
          self._save_to_file()
          print(f"[DEVICE REGISTRY] Unassigned {device_id}")
          return d
  ```
- **Step 5:** Commit `feat(devices): registry.clear_owner primitive (unclaim + demote primary)`.

### Task M1.3 — `POST /devices/{id}/unassign` route
**Files:** `device_routes.py` (declare **before** `GET /{device_id}` at `:305`); `test_device_unassign.py`.
- **Step 1 (test):** (a) unassign a Brandon-owned device with `{"operator":"Brandon"}` → 200, owner `""`; (b) then `POST /{id}/operator {"operator":"Anna"}` → 200, owner `Anna`; (c) unassign unknown id → 404; (d) blank/`system` operator → 400.
- **Step 3 (impl):**
  ```python
  @router.post("/{device_id}/unassign")
  async def unassign_operator(device_id: str, body: OperatorBody):
      """Clear a device's owner (and primary flag) so it can be re-homed. Requires a
      live operator in the body for provenance/logging; permitted within the
      cooperative tailnet trust model (the UI confirms cross-operator re-homes)."""
      _require_live_operator(body.operator)  # provenance; 400 on blank/system/unknown
      registry = get_registry()
      device = registry.clear_owner(device_id)
      if device is None:
          raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")
      return {"status": "unassigned", "device": device.to_dict()}
  ```
- **Step 5:** Commit `feat(devices): POST /{id}/unassign — first-class re-home path`.

### Task M1.4 — Live operator roster (kill restart lag)
**Files:** `admin_routes.py:1068-1101` (`POST /operator/add`), `admin_routes.py:1115-1148` (`DELETE /operator/{name}`); verify `config.py:140`; `test_operator_roster_live.py`.
- **Root cause (verified):** both handlers do `global USERS_LIST; USERS_LIST = [...]`, rebinding only *admin_routes'* alias. `device_routes._live_operators` re-reads `config.USERS_LIST` (unchanged) and `notification_routes` captured it at import — both stale until restart.
- **Step 1 (test):** start `USERS_LIST=["Brandon"]`; `POST /operator/add {"name":"Anna"}`; **without restart** assert (a) `_live_operators()` (device_routes) contains `"Anna"`; (b) `POST /devices/{id}/operator {"operator":"Anna"}` succeeds (not 400); (c) `GET /operators` includes `"Anna"`.
- **Step 3 (impl):** replace both rebinds with an in-place mutation of the shared object:
  ```python
  import Orchestrator.config as _cfg
  # after writing config.ini, in add_operator AND remove_operator:
  _cfg.USERS_LIST[:] = [u.strip() for u in CFG.get("users","list",fallback="").split(",") if u.strip()]
  ```
  In `remove_operator`, after mutating, guard a dangling default (3 lines): if `_cfg.USERS_DEFAULT` or `_cfg.CURRENT_OPERATOR` names the removed operator, re-point it at `_cfg.USERS_LIST[0]` (or a safe fallback if the list is now empty).
- **Thread-safety note:** operator add/remove is a rare admin action; slice-assignment is atomic enough under the GIL for the membership reads. If desired, guard the mutation with a tiny lock — optional, low priority.
- **Step 5:** Commit `fix(operators): mutate shared USERS_LIST in place so new operators are live without restart`.

# M2 — Backend: portability & data hygiene

### Task M2.1 — Portable, self-creating device-registry path
**Files:** `registry.py:23` + `DeviceRegistry.__init__`; `test_device_registry_freshbox.py`.
- **Step 3 (impl):**
  ```python
  DEVICES_FILE = Path(os.environ.get(
      "BLACKBOX_DEVICE_REGISTRY_PATH",
      str(Path(__file__).parent / "devices.json")))   # default == today's location
  ```
  In `__init__` (and defensively before `mkstemp` in `_save_to_file`): `os.makedirs(DEVICES_FILE.parent, exist_ok=True)`. In `_load_from_file`, if the configured path is missing but the legacy package-dir file exists, load the legacy file once and re-save to the new path.
- **Test isolation:** monkeypatch `registry_module.DEVICES_FILE` + reset the singleton (as M1.1); do **not** rely on the env var for in-process tests.
- **Step 5:** Commit `fix(devices): configurable self-creating devices.json path (systemd-safe default) + legacy load`.

### Task M2.2 — New-peer IP-collision handling on sync
**Files:** `registry.py` `sync_from_tailscale`; test in `test_device_registry_freshbox.py`.
- **Scope (precise):** this prevents *new* duplicate rows from being minted; it does **not** retroactively clean pre-existing dups. The visible Fold duplicate on THIS box (`samsung-sm-f956u-1` + `brandons-z-fold6`, both `100.111.152.112`) is resolved by **M2.3 normalize**, not here.
- **Step 1 (test):** discover a peer whose `ipv4` already belongs to a *different* existing device id → assert the existing row is updated and no second row is created.
- **Step 3 (impl):** when a discovered peer's `ipv4` matches an existing device with a different id, update the existing row (prefer the one whose DNS slug matches the current `dns_name`) instead of minting a new row; log the collision. **Merge semantics (explicit):** carry forward `owner`/`is_primary`/`default_provider` from the surviving non-empty row; **never silently drop** a row that is `is_primary` or owned by a *different* operator — surface it in the sync result instead.
- **Step 5:** Commit `fix(devices): reconcile new-peer tailscale_ip collisions on sync`.

### Task M2.3 — `POST /devices/normalize` (fresh-box + dup cleanup)
**Files:** `device_routes.py` (before `GET /{device_id}`); `registry.normalize()`; test.
- Collapses pre-existing same-IP dup rows (per M2.2 merge semantics) **and** clears owners not in the live roster (phantom operators from a prior box). Returns a summary. **Not** auto-run. This is the path that fixes this box's Fold dup and any customer box already poisoned with a phantom owner.
- Optional (nice-to-have, not required): surface a Portal admin affordance that calls it when `GET /devices/mesh` shows owners outside the live roster.
- **Step 5:** Commit `feat(devices): POST /normalize — dedupe + drop phantom-operator ownership`.

# M3 — Portal: reassign, declutter, clarify

### Task M3.1 — Unassign + confirm-on-rehome (fixes the re-home dead-end)
**Files:** `Portal/modules/devices-section.js:110-207`.
- **Provenance (the fix):** there is **no `currentOperator`/active-operator concept** in this file (state is `_operators`, `_filter`; `_filter` is `''` for "All operators"). Source the unassign provenance from data already in `buildCard(d)`:
  - **Re-home** (owned device, user picks a *different* operator `val`): `confirm("Re-home {name} from {d.owner} to {val}?")` → `await postJson('/devices/{id}/unassign', {operator: val})` (target `val` is guaranteed a live roster entry) → `await assignOperator(id, val)`.
  - **Pure unassign** (the sentinel): `await postJson('/devices/{id}/unassign', {operator: d.owner})` (`d.owner` is the current owner, guaranteed non-blank + live for an owned device).
- **Sentinel:** add an `"— Unassign —"` option with a **distinct non-empty value** (`__unassign__`) that no operator can equal; branch on it in the change handler **before** the existing `if (!val) return` guard.
- **Failure handling:** on ANY failure in the multi-step flow, `await refresh()` to re-render the true backend state (a partial re-home must not leave the select asserting a stale owner). Replaces the naive "reset select to old owner".
- **Verify:** System Menu → 📡 Devices → change an owned device's operator → confirm → succeeds; list refresh shows the new owner.
- **Commit** `feat(portal-devices): unassign sentinel + confirm-on-rehome (owned devices reassignable)`.

### Task M3.2 — Hide-unassigned (default-OFF) + roster-fresh-on-open
**Files:** `devices-section.js`, `Portal/index.html:459-466`, `_devices-section.css`.
- Add a "Hide unassigned" checkbox, **default OFF** (or auto-off when the active filter's operator owns zero devices) so a fresh box's unclaimed—hence claimable—devices stay visible. When checked, filter `render()` to `d.owner` truthy.
- On section open (menu observer, `:271-279`) **re-fetch operators** (drop the lifetime cache at `:235-236`) so a newly-added operator appears without reload.
- **Commit** `feat(portal-devices): hide-unassigned (default off) + live operator refresh on open`.

### Task M3.3 — Clarify copy (governs control + AI context, not SMS)
**Files:** `Portal/index.html:458` + helptext.
- Under `📡 Devices`: *"Assign which operator owns each device for AI device-control and chat context. (Text-message routing is configured separately under Telephony.)"*
- **Commit** `docs(portal-devices): clarify the owner picker governs control + AI context, not SMS`.

# M4 — Android: parity (peer surface)

### Task M4.1 — Fix the empty operator dropdown (parse bug)
**Files:** `ui/devices/MeshDeviceViewModel.kt:81-94`; test `MeshDeviceViewModelTest.kt`.
- **Step 1 (test):** feed `{"operators":["Brandon","Anna"]}` to the parse; assert `operators == ["Brandon","Anna"]`.
- **Step 3 (impl):**
  ```kotlin
  import kotlinx.serialization.json.contentOrNull   // REQUIRED — snippet won't compile without it
  val ops = json.parseToJsonElement(raw).jsonObject["operators"]
      ?.jsonArray?.mapNotNull { it.jsonPrimitive.contentOrNull }
      ?.filter { it.isNotBlank() } ?: emptyList()
  ```
  Surface a non-fatal hint on roster-load failure instead of silently swallowing (`:90`).
- **Commit** `fix(android-devices): parse /operators string array (owner dropdown was always empty)`.

### Task M4.2 — Operator-scope + client-side hide-unassigned (declutter)
**Files:** `MeshDeviceViewModel.kt:63-79` (+ a `_filter` StateFlow), `ui/devices/MeshDevicesSection.kt`.
- **Key correctness note:** `?operator=` is **scope-to-my-devices**, NOT a declutter filter — the backend returns `owner==null OR owner==op` (`device_routes.py:239`), so unclaimed nodes STILL come back. The real declutter is a **client-side hide-unassigned** applied in BOTH the filtered and unfiltered branches (mirror the Portal's `d.owner`-truthy filter).
- Add a filter/hide-unassigned control row. **Default hide-unassigned OFF** (or auto-off when the operator owns zero devices) so fresh-box devices remain claimable.
- **Commit** `feat(android-devices): operator-scoped mesh + client-side hide-unassigned (declutter)`.

### Task M4.3 — Unassign / re-home affordance (single coroutine)
**Files:** `MeshDeviceViewModel.kt`, `MeshDevicesSection.kt`.
- Add ONE combined VM method to avoid a race (never two independent fire-and-forget calls):
  ```kotlin
  fun rehome(deviceId: String, newOperator: String, currentOwner: String) = viewModelScope.launch {
      try {
          api.post("/devices/$deviceId/unassign", buildJsonObject { put("operator", currentOwner) }.toString())
          api.post("/devices/$deviceId/operator", buildJsonObject { put("operator", newOperator) }.toString())
          _actionMessage.value = "Reassigned to $newOperator"; refresh()
      } catch (e: Exception) { _error.value = "Reassign failed: ${e.message}" }   // persistent, not just a toast
  }
  ```
  Pure-unassign passes `device.owner` as the requester. Add an **"Unassign"** sentinel entry in the owner picker and intercept it in `onSelect` (route to unassign/rehome, not `assignOperator`). Add a confirmation `AlertDialog` before the destructive re-home. `BlackBoxApi.post` already throws `ApiHttpException` on non-2xx (`:135`), so surface `_error` inline.
- **Commit** `feat(android-devices): unassign + single-coroutine confirm-on-rehome`.

### Task M4.4 — Clarify copy + build gate
**Files:** `MeshDevicesSection.kt:110-114` subtitle → match the Portal wording.
- Run the unit gate: `./gradlew :app:testDebugUnitTest --offline`.
- **Commit** `docs(android-devices): clarify picker scope; unit gate green`.

# M5 — Tests, verification, docs

### Task M5.1 — Backend coverage (ADD, don't delete the guard)
- **KEEP** `test_device_routes_mesh.py:73-83` (the 409 anti-steal guard + "owner/primary untouched by a refused steal" — that is *intended* behavior). Only fix its misleading comment (it is not a "deadlock", it is the guard).
- **ADD** a recovery test: `assign→409`, then `unassign→200`, then `assign→200` (owner changed).
- **ADD:** fresh-box empty→assign→reload persists; new-operator-without-restart assign; new-peer IP-collision update; sync leaves owner unclaimed.
- Run: `python -m pytest Orchestrator/tests/test_device_*.py Orchestrator/tests/test_operator_roster_live.py -q`.

### Task M5.2 — Prompt-context intent test
**Files:** `test_device_prompt_context.py`.
- Assert a *claimed* device appears in `to_prompt_context(owner)` and an *unclaimed* one does not — pinning the "unclaimed until assigned" product story that M1.1 introduces.

### Task M5.3 — End-to-end verify (real surfaces)
- Backend: `curl` unassign→reassign on a live device; confirm `devices.json` updated and survives a service restart.
- Portal: reassign an owned device in the browser; verify persistence after reload.
- Android: worktree/device build per `android_build_env` + `ondevice_device_test_method`; verify empty-dropdown fixed, clutter gone, reassign works.
- Use the `verify` skill before claiming done.

### Task M5.4 — Snapshot + memory
`/snapshot-dev` under operator **Brandon-DEV** (confirmed this session). Update `MEMORY.md` pointer.

---

## Non-Goals (YAGNI / out of scope)
- **SMS / text-message routing changes.** `device.owner` stays decoupled from SMS (gateway-port operator + contact books). No wiring, no schema change.
- **Push-notification routing changes.** Stays on the self-subscribe store (`device_notification_subs.json`).
- **Merging the two "Devices" surfaces.** The ADB-pairing modal (`device-manager.js`) and the operator/mesh section (`devices-section.js`) stay separate; we only clarify copy so users find the right one.
- **App-layer auth on unassign.** The tailnet is the boundary (`tailscale_security_perimeter`); unassign requires a live operator for provenance only.

## Cross-cutting verification (after each milestone)
- `python -m pytest Orchestrator/tests/test_device_*.py Orchestrator/tests/test_operator_roster_live.py -q` → green.
- Import gate / service restart clean (`sudo systemctl restart blackbox.service`, pre-authorized).
- `GET /devices/mesh` shape stays additive (`feedback_frontend_three_surfaces`). `/ui/*.js` is served no-cache with etag (`startup.py:788-806`), so the Android WebView inherits Portal fixes transitively; still bump `index.html` `?v=` after copy edits.
- Fresh-box smoke: monkeypatch `DEVICES_FILE` to an empty temp path (not the env var), sync, assign — no "Brandon" appears; then confirm hide-unassigned default-OFF leaves the synced devices claimable.

## Rollout
- Build on `main` (staging-as-prod, `feedback_staging_box_as_production`); explicit path staging, **no `git add -A`** (`feedback_git_add_dash_a`).
- This box keeps its Brandon-owned rows (correct here); `POST /devices/normalize` is available but not auto-run, and is what collapses this box's duplicate-IP Fold rows.
- Ship = local device-validation (Fold) → push. Bump Portal `?v=` after `index.html` edits.

---

## Revision log — adversarial red-team (2026-07-07, 23 agents, 5 lenses)

All 5 lenses returned "needs-revision" (no fatal flaw; the plan's spine — unclaimed discovery, `clear_owner`+unassign, in-place roster mutation, `owner=""` routing-safety — was independently **confirmed correct**). Folded-in fixes:

- **BLOCKER (4 reviewers) — invented `currentOperator`.** M3.1 called `/unassign` with an undefined `currentOperator` (would send a blank operator → 400). Fixed: provenance is the target `val` (re-home) or `d.owner` (pure unassign), both guaranteed live. Mirrored on Android (M4.3).
- **Android re-home race.** Two independent VM calls could interleave; replaced with a single `rehome()` coroutine (unassign→assign→refresh).
- **`?operator=` doesn't declutter** (returns `owner==null OR ==op`). Declutter is now client-side hide-unassigned in both branches (M4.2).
- **Fresh-box trap:** hide-unassigned default-ON would hide all claimable devices on a fresh box. Now default-OFF / auto-off at zero-owned (M3.2, M4.2).
- **Portable path:** added `os.makedirs(exist_ok=True)`; default stays in-repo (systemd-safe); documented the `ReadWritePaths` constraint for overrides (M2.1, Locked Decision 4).
- **Dup-IP scope:** M2.2 only prevents *new* dups + defined merge semantics; the *existing* Fold dup is resolved by M2.3 normalize (was ambiguous).
- **Test integrity:** M5.1 now **adds** a recovery test and **keeps** the 409-guard test (was "replace"); freshbox tests must monkeypatch `DEVICES_FILE` + reset the singleton + mock tailscale (never touch live `devices.json`).
- **Wrong refs fixed:** operator routes are `POST /operator/add` + `DELETE /operator/{name}` (not `/operators`); 2nd hardcoded owner is `adb/manager.py:394`; Android needs `import …json.contentOrNull`.
- **Minors:** `remove_operator` guards a dangling `USERS_DEFAULT`/`CURRENT_OPERATOR`; added a prompt-context intent test (M5.2); reworded the "auto-sync-on-load" claim to the accurate "manual + CU-drawer-gated sync".
