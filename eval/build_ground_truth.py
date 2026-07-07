#!/usr/bin/env python3
"""Build a CONTROLLED ground-truth retrieval benchmark (Phase 1).

The real corpus has no relevance labels and its synthesized-from-gold queries
leak. This builds a corpus where WE own relevance: a deterministic spec of
TOPICS (each with a gold cluster), engineered DECOYS (a snapshot about topic X
that deliberately name-drops topic Y's rare "trap" tokens in an off-topic
aside — the keyword-injection trap), and HUBS (long multi-topic distractors).
Gemini 3.5 Flash (free) only expands each spec item into realistic prose; the
LABELS come from the spec, never from the model.

Output (model-agnostic — embed later per model in the sandbox):
  eval/ground_truth/corpus.jsonl   {sid, topic, kind, trap_tokens, envelope_text}
  eval/ground_truth/queries.jsonl  {query, topic, gold_sids[], hard_negative_sids[]}

Deterministic: fixed sids (SNAP-GT####) and timestamps; a body cache keyed by
(kind,topic,angle,borrow) so re-runs are free and reproducible. READ-ONLY wrt
the live BlackBox — writes only under eval/ground_truth/.

Run:  Orchestrator/venv/bin/python eval/build_ground_truth.py
"""
from __future__ import annotations
import hashlib
import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "eval" / "ground_truth"
CACHE = OUT / "_body_cache.json"

# ── load GOOGLE_API_KEY from .env (same pattern as the probe) ──────────────────
for ln in (REPO / ".env").read_text().splitlines() if (REPO / ".env").exists() else []:
    if "=" in ln and not ln.strip().startswith("#"):
        k, _, v = ln.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
GKEY = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
GEN_MODEL = "gemini-3.5-flash"

# ── THE SPEC (ground truth — WE define relevance) ─────────────────────────────
# Each topic: distinct subject, 4 gold angles, rare trap tokens (borrowed by
# decoys to create lexical collisions), 2 intent-authored queries.
TOPICS = [
    {"id": "voice_clone", "title": "ElevenLabs voice cloning & design",
     "desc": "cloning a user's voice from audio with consent, and designing a synthetic voice from a text description",
     "angles": ["cloning a voice from a 60s sample with explicit consent capture",
                "designing a voice from a text description then previewing and saving it",
                "the voice-changer re-voicing an existing recording",
                "storing cloned voice ids and listing them in the Voice Lab panel"],
     "traps": ["voice clone", "consent", "ElevenLabs", "Voice Lab"],
     "queries": ["how do I clone someone's voice with their consent",
                 "design a synthetic voice from a description and save it"]},
    {"id": "calendar", "title": "Google Calendar event tools",
     "desc": "creating, updating and listing Google Calendar events including recurring events",
     "angles": ["creating a single timed event with attendees",
                "a weekly recurring event and its RRULE",
                "listing events in a date window across multiple calendars",
                "updating and deleting an existing event by id"],
     "traps": ["recurring event", "RRULE", "attendee", "calendar"],
     "queries": ["create a recurring weekly calendar event",
                 "list google calendar events for next week"]},
    {"id": "android_battery", "title": "Android foreground-service battery",
     "desc": "keeping an Android foreground service alive without being killed by Doze / battery optimization",
     "angles": ["a foreground service surviving Doze mode",
                "requesting battery-optimization exemption from the user",
                "a partial wakelock during a long download",
                "notification channel required for a foreground service"],
     "traps": ["foreground service", "Doze", "wakelock", "battery optimization"],
     "queries": ["keep android foreground service alive in doze mode",
                 "stop the OS from killing my background download service"]},
    {"id": "pdf_export", "title": "PDF report generation",
     "desc": "rendering a formatted PDF report from app data",
     "angles": ["laying out a multi-page PDF with a header and table",
                "embedding a chart image into a generated PDF",
                "streaming a large PDF export to the browser",
                "choosing a PDF library and page size"],
     "traps": ["PDF", "render", "page size", "export"],
     "queries": ["generate a multi page pdf report with a table",
                 "embed a chart into a pdf export"]},
    {"id": "ws_reconnect", "title": "WebSocket reconnect & backoff",
     "desc": "reconnecting a dropped WebSocket with exponential backoff and resuming state",
     "angles": ["exponential backoff with jitter on socket drop",
                "resuming a subscription after reconnect",
                "detecting a dead socket with heartbeat pings",
                "capping retries and surfacing a disconnected state"],
     "traps": ["reconnect", "backoff", "heartbeat", "socket"],
     "queries": ["reconnect a websocket with exponential backoff",
                 "resume my socket subscription after a dropped connection"]},
    {"id": "grok_image", "title": "Grok image generation",
     "desc": "generating images with the Grok image model and handling resolution",
     "angles": ["generating an image from a text prompt with Grok",
                "coercing the output to a supported resolution",
                "moving the api key from url to header for image calls",
                "provider-tagged worker routing for image jobs"],
     "traps": ["Grok", "image", "resolution", "prompt"],
     "queries": ["generate an image with grok from a prompt",
                 "fix grok image resolution and api key handling"]},
    {"id": "sql_migration", "title": "Database schema migration",
     "desc": "migrating a relational database schema safely with rollback",
     "angles": ["adding a non-null column with a backfill migration",
                "a reversible migration with an up and down step",
                "zero-downtime migration with a shadow table",
                "guarding a migration behind a feature flag"],
     "traps": ["migration", "schema", "rollback", "backfill"],
     "queries": ["safely add a not null column via migration",
                 "reversible database schema migration with rollback"]},
    {"id": "geofence", "title": "Map geofencing",
     "desc": "defining a circular geofence on a map and firing enter/exit events",
     "angles": ["defining a circular geofence by center and radius",
                "firing an enter event when the device crosses the boundary",
                "drawing the geofence circle on the map overlay",
                "battery-aware geofence monitoring intervals"],
     "traps": ["geofence", "radius", "map overlay", "boundary"],
     "queries": ["create a circular geofence with enter and exit events",
                 "draw a geofence radius on a map"]},
    {"id": "oauth_pkce", "title": "OAuth 2.1 PKCE flow",
     "desc": "an OAuth authorization-code flow with PKCE, consent and token refresh",
     "angles": ["the PKCE code_verifier/code_challenge exchange",
                "the consent screen and authorization-code redirect",
                "refreshing an access token with a refresh token",
                "dynamic client registration for a public client"],
     "traps": ["OAuth", "PKCE", "token refresh", "consent"],
     "queries": ["implement oauth pkce authorization code flow",
                 "refresh an oauth access token with pkce"]},
    {"id": "csv_import", "title": "CSV import parsing",
     "desc": "importing and parsing a user-uploaded CSV with custom delimiters",
     "angles": ["detecting the delimiter and header row",
                "streaming a large CSV without loading it all in memory",
                "validating and coercing column types on import",
                "reporting per-row import errors back to the user"],
     "traps": ["CSV", "delimiter", "header row", "import"],
     "queries": ["parse an uploaded csv with a custom delimiter",
                 "stream a large csv import and report row errors"]},
    {"id": "dark_theme", "title": "Dark-mode theming tokens",
     "desc": "implementing dark mode via design tokens that respond to system theme",
     "angles": ["design tokens that switch on prefers-color-scheme",
                "a manual light/dark toggle overriding the system theme",
                "theming a chart's colors for both modes",
                "avoiding a flash of the wrong theme on load"],
     "traps": ["dark mode", "design token", "theme", "prefers-color-scheme"],
     "queries": ["implement dark mode with design tokens",
                 "add a light dark theme toggle that overrides the system"]},
    {"id": "rate_limit", "title": "API rate limiting & 429s",
     "desc": "handling upstream rate limits, throttling and 429 responses",
     "angles": ["retrying a 429 with Retry-After backoff",
                "a token-bucket throttle on outbound calls",
                "per-key rate-limit quotas",
                "surfacing a rate-limit error to the user gracefully"],
     "traps": ["rate limit", "429", "throttle", "Retry-After"],
     "queries": ["handle a 429 rate limit with retry after backoff",
                 "throttle outbound api calls with a token bucket"]},
]

# DECOYS: a snapshot genuinely ABOUT home, that name-drops borrow's trap tokens
# in an off-topic aside. It is a HARD NEGATIVE for the borrow topic's queries.
DECOYS = [
    ("csv_import", "rate_limit"),     # csv importer that "also hit a 429"
    ("android_battery", "pdf_export"),  # battery work that "exported logs to PDF"
    ("calendar", "oauth_pkce"),       # calendar sync that mentions "oauth token refresh / consent"
    ("dark_theme", "oauth_pkce"),     # dark-mode "design token" vs oauth "token"
    ("geofence", "android_battery"),  # geofence that mentions "wakelock / doze"
    ("grok_image", "pdf_export"),     # image gen that "rendered to PDF"
    ("ws_reconnect", "rate_limit"),   # reconnect that "backs off on 429 rate limit"
    ("sql_migration", "csv_import"),  # migration that "imported a CSV seed"
    ("voice_clone", "oauth_pkce"),    # voice lab that "needs consent" (consent collision)
    ("pdf_export", "dark_theme"),     # pdf that "themes the report for dark mode"
    ("rate_limit", "grok_image"),     # throttle work that mentions "grok image calls"
    ("oauth_pkce", "calendar"),       # oauth used "for the calendar attendee scope"
]

# HUBS: long, topically-broad snapshots blending 3 topics — distractors that are
# not the gold for any single query (hubness stress).
HUBS = [
    ["voice_clone", "grok_image", "pdf_export"],
    ["calendar", "oauth_pkce", "rate_limit"],
    ["android_battery", "geofence", "ws_reconnect"],
    ["csv_import", "sql_migration", "dark_theme"],
]

TOPIC_BY_ID = {t["id"]: t for t in TOPICS}


def _client():
    from google import genai
    return genai.Client(api_key=GKEY)


def _cache_load():
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def _cache_save(c):
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(c))


def gen_body(client, cache, key: str, prompt: str) -> str:
    if key in cache:
        return cache[key]
    for attempt in range(4):
        try:
            r = client.models.generate_content(model=GEN_MODEL, contents=prompt)
            txt = (r.text or "").strip()
            if txt:
                cache[key] = txt
                _cache_save(cache)
                return txt
        except Exception as e:
            print(f"  gen retry {attempt} ({str(e)[:80]})")
            time.sleep(2 + attempt * 2)
    raise SystemExit(f"generation failed for {key}")


def envelope(sid: str, ts: str, operator: str, body: str) -> str:
    """A realistic BlackBox snapshot envelope so content_mode=full parity holds
    (extract_snapshot_content strips this; embed sees it)."""
    return (
        f"=== START SNAPSHOT — UTC {ts} — {sid} (7.1.0) ===\n"
        "CROSS-FILE BEACON\n" + "=" * 60 + "\n"
        f"COUNT=1 | TARGET_ID={sid}\nResult: Tail lock confirmed\n" + "=" * 60 + "\n\n"
        f"VOLUME TRACKER\nTail: {sid}\nMode: NORMAL\n\n"
        f"GAUGES\nMODEL: gemini-3.5-flash\nOPERATOR: {operator}\nMODE: Normal\n\n"
        "SNAPSHOT BODY\n\nRaw Session Log\n"
        f"{body}\n\n"
        f"=== END SNAPSHOT — {sid} — UTC {ts} ===\n"
    )


GOLD_PROMPT = (
    "Write a realistic AI-assistant development/session log about ONE narrow "
    "topic, as a 'Raw Session Log' of a user asking for help and the assistant "
    "answering. Topic: {title}. Specific angle: {angle}. 250-450 words. Use "
    "concrete technical detail and mention these terms naturally: {traps}. Write "
    "ONLY the log body (a couple of `- [n] user:` / `- [n] assistant:` turns), "
    "no envelope, no preamble."
)
DECOY_PROMPT = (
    "Write a realistic AI-assistant session log PRIMARILY about: {home_title} "
    "({home_desc}). 250-400 words, concrete. IMPORTANT: it must be genuinely "
    "about {home_title}, but include ONE brief off-topic aside that casually "
    "name-drops these unrelated terms: {borrow_traps} (e.g. a passing mention "
    "like 'unrelated, we also briefly hit ...'). Do NOT actually solve or "
    "explain the {borrow_title} topic — just mention the words in passing. "
    "Write ONLY the log body, no envelope."
)
HUB_PROMPT = (
    "Write a long, wide-ranging AI-assistant session log that jumps across "
    "THREE unrelated topics in one sitting: {titles}. 500-800 words, a few "
    "turns per topic, topically diffuse (a 'catch-up' session). Mention terms "
    "from all three: {all_traps}. Write ONLY the log body, no envelope."
)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    client = _client()
    cache = _cache_load()
    corpus = []
    n = 0

    def sid_ts(i):
        # deterministic spread across 2026-01..2026-06
        month = 1 + (i % 6)
        day = 1 + (i % 27)
        return f"SNAP-GT{i:04d}", f"2026-{month:02d}-{day:02d}T12:00:00Z"

    gold_sids_by_topic = {t["id"]: [] for t in TOPICS}

    # GOLDS
    for t in TOPICS:
        for a_i, angle in enumerate(t["angles"]):
            n += 1
            sid, ts = sid_ts(n)
            body = gen_body(client, cache, f"gold|{t['id']}|{a_i}",
                            GOLD_PROMPT.format(title=t["title"], angle=angle,
                                               traps=", ".join(t["traps"])))
            corpus.append({"sid": sid, "topic": t["id"], "kind": "gold",
                           "trap_tokens": t["traps"],
                           "envelope_text": envelope(sid, ts, "test", body)})
            gold_sids_by_topic[t["id"]].append(sid)
        print(f"[gold] {t['id']}: {len(t['angles'])} snapshots")

    # DECOYS
    decoy_hardneg_by_topic = {t["id"]: [] for t in TOPICS}
    for home, borrow in DECOYS:
        n += 1
        sid, ts = sid_ts(n)
        h, b = TOPIC_BY_ID[home], TOPIC_BY_ID[borrow]
        body = gen_body(client, cache, f"decoy|{home}|{borrow}",
                        DECOY_PROMPT.format(home_title=h["title"], home_desc=h["desc"],
                                            borrow_title=b["title"],
                                            borrow_traps=", ".join(b["traps"])))
        corpus.append({"sid": sid, "topic": home, "kind": "decoy",
                       "borrows": borrow, "trap_tokens": b["traps"],
                       "envelope_text": envelope(sid, ts, "test", body)})
        decoy_hardneg_by_topic[borrow].append(sid)
        print(f"[decoy] {home} borrows {borrow} -> {sid}")

    # HUBS
    for hi, topics in enumerate(HUBS):
        n += 1
        sid, ts = sid_ts(n)
        titles = [TOPIC_BY_ID[x]["title"] for x in topics]
        all_traps = [tok for x in topics for tok in TOPIC_BY_ID[x]["traps"]]
        body = gen_body(client, cache, f"hub|{hi}",
                        HUB_PROMPT.format(titles="; ".join(titles),
                                          all_traps=", ".join(all_traps)))
        corpus.append({"sid": sid, "topic": "+".join(topics), "kind": "hub",
                       "trap_tokens": all_traps,
                       "envelope_text": envelope(sid, ts, "test", body)})
        print(f"[hub] {topics} -> {sid}")

    # QUERIES
    queries = []
    for t in TOPICS:
        for q in t["queries"]:
            queries.append({
                "query": q, "topic": t["id"],
                "gold_sids": gold_sids_by_topic[t["id"]],
                "hard_negative_sids": decoy_hardneg_by_topic[t["id"]],
            })

    (OUT / "corpus.jsonl").write_text("\n".join(json.dumps(c) for c in corpus) + "\n")
    (OUT / "queries.jsonl").write_text("\n".join(json.dumps(q) for q in queries) + "\n")
    print(f"\n[done] corpus={len(corpus)} (golds={sum(1 for c in corpus if c['kind']=='gold')}, "
          f"decoys={sum(1 for c in corpus if c['kind']=='decoy')}, "
          f"hubs={sum(1 for c in corpus if c['kind']=='hub')})  queries={len(queries)}")
    print(f"[done] wrote {OUT/'corpus.jsonl'} and {OUT/'queries.jsonl'}")


if __name__ == "__main__":
    main()
