# /snapshot-dev — Mint a Development Snapshot

Mint a snapshot of the current session's completed work into the BlackBox volume so future sessions can find it via semantic + keyword search.

## How It Works

1. POST to `/chat/save` (NOT `/chat` — that wastes an LLM round-trip).
2. Backend's auto-mint trigger (`turns_threshold=1`) fires `perform_mint()` immediately.
3. `perform_mint()` generates an embedding from the active embedding model (see `Orchestrator/embeddings/registry.py`; status at `GET /embeddings/status`) inline before returning, so the snapshot is searchable the moment the curl returns.
4. Snapshot ID returned in the response body.

## Default Behavior

- **Operator:** resolved dynamically — NEVER hard-coded. Follow the shared
  `resolve-operator` procedure (`.claude/commands/resolve-operator.md`): a slash arg
  wins; otherwise call the `get_current_operator` MCP tool — a single operator is used
  automatically, and when multiple operators exist you present an AskUserQuestion
  dropdown (pre-selecting `default`) to pick whose work this snapshot records.
- **Trigger:** invoked manually via `/snapshot-dev`, OR organically by Claude at the end of any non-trivial task per the CLAUDE.md instruction.
- **Cost:** ~one embedding call with the active model (cents-level for cloud models, free for local Ollama models). No LLM completion call.

## Operator Override

If the user passes an operator as an argument (e.g. `/snapshot-dev system`), use that
verbatim. Otherwise resolve via the `resolve-operator` procedure above (single → auto;
multiple → dropdown). Do not assume any particular operator name.

## What To Capture

The `assistant_response` field becomes the snapshot body. Future semantic search hits this. Be comprehensive enough that a fresh session searching for the work can fully reconstruct context. Include:

- **What problem was solved** (one paragraph)
- **Files created** (path + line count + one-line description per file)
- **Files modified** (path + nature of change)
- **Architecture choices worth remembering** (especially non-obvious ones — gotchas, scope nuances, invariants)
- **Test totals** (how many tests, all passing?)
- **Verification evidence** (probe results, log lines, build output summary)
- **Search hint phrases** ("Search this snapshot via X, Y, Z") so future-Claude can grep semantically
- **Resolution status (memory hygiene — REQUIRED when relevant)** — if this snapshot records the FIX for a previously-snapshotted bug/failure, say so explicitly: state FIXED/RESOLVED and reference the prior snapshot id (e.g. "supersedes the failure in SNAP-YYYYMMDD-NNNN"). Conversely, if you snapshot a bug you did NOT fully resolve, mark it OPEN. **Why:** semantic search ranks an older failure snapshot highly for the same topic, so without a resolution marker a future session retrieves the stale "it's broken" snapshot and acts on it (a real confabulation source — see the 2026-06-22 phantom Workspace-tool failure). The cross-reference lets retrieval self-correct.

## Procedure

```bash
# 1. Build the JSON payload as a heredoc-to-file (avoids shell-quoting hell with embedded JSON)
cat > /tmp/snap_payload.json <<'EOF'
{
  "operator": "<resolved operator — from resolve-operator, NOT a literal>",
  "user_message": "<one-line framing of the work being snapshotted>",
  "assistant_response": "<the full structured summary — see What To Capture above>",
  "model": "claude-opus-4-7",
  "tokens": {"prompt": 0, "completion": 0}
}
EOF

# 2. POST to /chat/save
curl -s -X POST http://localhost:9091/chat/save \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/snap_payload.json | python3 -m json.tool

# Expected response:
#   {
#     "success": true,
#     "operator": "<resolved operator>",
#     "minted": true,
#     "snap_id": "SNAP-YYYYMMDD-NNNN",
#     ...
#   }

# 3. Verify embedding generated (proof point — the mint path logs
#    "[EMBEDDING] Successfully generated embedding (N dimensions)" where N is
#    the active model's dims per GET /embeddings/status; failures log
#    "[EMBEDDING] Warning: Failed to generate embedding" from checkpoint.py or
#    "[EMBEDDING] <slug>: embedding generation failed" from the provider layer)
sudo journalctl -u blackbox.service --since "30 seconds ago" --no-pager 2>&1 | \
  grep -E "MINT|EMBEDDING|SNAP-YYYYMMDD-NNNN" | head -10
```

**IMPORTANT:** If `Write` to `/tmp` fails or shell quoting blocks long content, fall back to writing the payload via the `Write` tool to `/tmp/snap_payload.json`, then `curl --data-binary @/tmp/snap_payload.json`. JSON inside `-d '{...}'` shell-single-quotes is fragile for multi-paragraph bodies — use the file path.

## Reporting Back

When the snapshot mints, report to the user:

| Field | Value |
|---|---|
| **snap_id** | `SNAP-YYYYMMDD-NNNN` |
| **operator** | (the one used) |
| **embedding** | N dimensions ✓ — matches the active model's dims (or warn if `Failed to generate`) |
| **media artifacts** | (count if any auto-attached) |
| **search hint** | (1-2 phrases the user can grep for later) |

## Anti-Patterns (do NOT do)

- ❌ Don't POST to `/chat` (LLM round-trip, ~$0.05+ per call vs ~$0.0001 for /chat/save).
- ❌ Don't manually call `/mint` afterward — auto-mint already fired, you'd create a duplicate.
- ❌ Don't hard-code an operator (e.g. `Brandon`). Resolve via `get_current_operator`: use the single operator automatically; only AskUserQuestion (dropdown) when it reports `needs_selection` (multiple operators).
- ❌ Don't truncate the summary — semantic search quality depends on full context. The 30k char cap is a hard ceiling, not a target.
- ❌ Don't use stale model names (`gemini-2.5-pro`, `text-embedding-004`). Current defaults: `gemini-3.1-pro-preview-customtools` (chat); embeddings use the active model from `Orchestrator/embeddings/registry.py` (check `GET /embeddings/status`). The `model` field in the SaveRequest is just metadata; use whichever model actually produced the response (e.g. `claude-opus-4-7`).

## When To Auto-Trigger (per CLAUDE.md)

Per the updated CLAUDE.md, invoke this at the end of:
- Completing a multi-step plan (this session's "Android context retrieval pipeline fix" was a perfect example)
- Wrapping a meaningful debugging session (root cause found and fixed)
- Major refactor or feature landing
- Any task involving 3+ files modified

Skip for:
- Trivial Q&A ("yes", "what does X do")
- Single-file typo fixes
- Pure exploration with no code changes
- Tasks the user hasn't asked you to record
