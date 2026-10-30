"""Health tracking inside BlackBox ("wellness" in code, "Health" in the UI).

Design: docs/plans/2026-10-25-samsung-health-integration.md (M7). Each
operator picks one of their own paired Android phones as their health source;
the BlackBox app reads Health Connect and pushes signed batches here, into a
private per-operator SQLite store. Deterministic Python computes every number
(rollups, the person's own usual range, coverage, trouble states); models only
explain, through read-only tools that a default-deny gate offers per turn.

Modules (each imports only what it needs, so the stdlib-only ones stay usable
from the MCP server's lean venv):

  paths         where the stores live (data/wellness/, credentials/)
  catalog       categories, metrics, units, display/spoken formatters, the
                default consent, the instinct prompt and the WELLNESS RULES
  schema        pydantic models for every body the phone sends
  origin_proof  the HMAC origin proof (X-BlackBox-Origin) sign/verify
  store         one SQLite file per operator, the spec's DDL
  ingest        the signed batch pipeline
  rollups       per-day/night rollups and baselines (time injected)
  coverage      wear, HR mode, the coverage object, trouble states
  digests       day digests and the words-only weekly note
  enroll        enrollments.json, device keys, E1-E7, the bind handshake
  destinations  local / on_device / cloud, classified by resolved address
  turn          the per-turn WellnessTurn contextvar (stdlib only)
  gate          the six checks, in order, with their refusal codes
  api           the deterministic answers behind the wellness_* tools
  erase         stop & erase, and the operator-delete hook

Nothing here writes under Portal/, Volumes/, Archive/, Manifest/ or a tracked
path, and nothing logs a health value: counts only.
"""
