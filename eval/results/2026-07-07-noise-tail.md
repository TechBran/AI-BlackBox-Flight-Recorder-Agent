# Noise-tail / precision probe — 2026-07-07

Active model `gemini-embedding-2`, k=10, junk_floor=0.4, candidate_n=40, Vertex rerank available=True. Channel attribution replicates retrieve()'s candidate generation; delivered set is the live production `retrieve()` (hybrid). READ-ONLY.

## Headline

- **Keyword-only (lexical) delivered: 100/150 (67%)** — mean per-query 67%. These snapshots never cleared any semantic gate; they enter ranking purely on lexical RRF fusion (retrieval.py:426-429).
- **Genuine semantic hits EVICTED by keyword fusion: 115** (present in the semantic-only top-k, pushed out of the hybrid top-k).

## Per-query worksheet (fill `judge:` relevant/borderline/irrelevant for precision@k)

### 'NSA checkpoint Fort Meade Officer Nguyen ticket'  (operator=Brandon, keyword-only 7/10, evicted 7)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.7517 | 0.1394 | SNAP-20260707-8064 | REL |  | Raw Session Log - [1] 2026-07-07T14:22:10Z operator=Brandon user: Can  |
| 2 | both | 0.7232 | 0.4040 | SNAP-20260416-5953 |  |  | Raw Session Log - [1] 2026-04-16T11:02:01Z operator=Brandon user: No,  |
| 3 | both | 0.5810 | 0.0166 | SNAP-20260707-8065 |  |  | Raw Session Log - [1] 2026-07-07T14:29:09Z operator=Brandon user: Yeah |
| 4 | keyword | — | 0.0156 | SNAP-20260619-7181 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 5 | keyword | — | 0.0254 | SNAP-20260314-4385 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 6 | keyword | — | 0.0135 | SNAP-20260425-6268 |  |  | Raw Session Log - [1] 2026-04-25T01:46:59Z operator=Brandon user: Bran |
| 7 | keyword | — | 0.0095 | SNAP-20260425-6272 |  |  | Raw Session Log - [1] 2026-04-25T02:18:22Z operator=Brandon user: okay |
| 8 | keyword | — | 0.0140 | SNAP-20260323-4743 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 9 | keyword | — | 0.0087 | SNAP-20260425-6267 |  |  | Raw Session Log - [1] 2026-04-25T01:36:28Z operator=Brandon user: Okay |
| 10 | keyword | — | 0.0087 | SNAP-20260425-6265 |  |  | Raw Session Log - [1] 2026-04-25T01:22:56Z operator=Brandon user: Bran |

_Evicted semantic hits: SNAP-20260306-4127, SNAP-20260428-6335, SNAP-20260416-5963, SNAP-20251219-1950, SNAP-20260416-5955, SNAP-20260312-4326, SNAP-20260312-4329_

### 'semantic search returns irrelevant snapshots noise floor reranker'  (operator=Brandon-DEV, keyword-only 7/10, evicted 8)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.7943 | 0.2396 | SNAP-20260707-8067 | REL |  | Raw Session Log - [1] 2026-07-07T14:54:07Z operator=Brandon-DEV user:  |
| 2 | keyword | — | 0.0517 | SNAP-20260707-8057 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 3 | both | 0.7267 | 0.1588 | SNAP-20260621-7209 | REL |  | Raw Session Log - [1] 2026-06-21T23:41:09Z operator=Brandon-DEV user:  |
| 4 | keyword | — | 0.0270 | SNAP-20260620-7201 |  |  | Raw Session Log - [1] 2026-06-20T21:48:24Z operator=Brandon-DEV user:  |
| 5 | keyword | — | 0.0139 | SNAP-20260623-7647 |  |  | Raw Session Log - [1] 2026-06-23T23:09:08Z operator=Brandon-DEV user:  |
| 6 | keyword | — | 0.0099 | SNAP-20260622-7595 |  |  | Raw Session Log - [1] 2026-06-22T20:25:49Z operator=Brandon-DEV user:  |
| 7 | keyword | — | 0.0320 | SNAP-20251201-1483 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 8 | keyword | — | 0.0031 | SNAP-20260706-8043 |  |  | Raw Session Log - [1] 2026-07-06T20:42:14Z operator=Brandon-DEV user:  |
| 9 | keyword | — | 0.0035 | SNAP-20260703-7945 |  |  | Raw Session Log - [1] 2026-07-03T16:34:19Z operator=Brandon-DEV user:  |
| 10 | both | 0.6446 | 0.0248 | SNAP-20260705-8000 |  |  | Raw Session Log - [1] 2026-07-05T17:58:31Z operator=Brandon-DEV user:  |

_Evicted semantic hits: SNAP-20260622-7601, SNAP-20260506-6482, SNAP-20260116-2370, SNAP-20251207-1646, SNAP-20251124-1332, SNAP-20260621-7202, SNAP-20260622-7390, SNAP-20251116-984_

### 'ElevenLabs API key onboarding wizard environment variable'  (operator=Brandon, keyword-only 7/10, evicted 9)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.6652 | 0.1550 | SNAP-20260613-7030 |  |  | Raw Session Log - [1] 2026-06-13T21:27:06Z operator=Brandon user: Elev |
| 2 | semantic | 0.6413 | 0.1869 | SNAP-20260520-6724 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 3 | both | 0.6564 | 0.0399 | SNAP-20260613-7014 |  |  | Raw Session Log - [1] 2026-06-13T00:22:24Z operator=Brandon user: Elev |
| 4 | keyword | — | 0.0340 | SNAP-20260517-6698 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 5 | keyword | — | 0.0303 | SNAP-20260517-6702 |  |  | Raw Session Log - [1] 2026-05-17T20:50:53Z operator=Brandon user: ONBO |
| 6 | keyword | — | 0.0181 | SNAP-20260704-7959 |  |  | Raw Session Log - [1] 2026-07-04T06:03:17Z operator=Brandon user: Yeah |
| 7 | keyword | — | 0.0288 | SNAP-20260512-6591 |  |  | Raw Session Log - [1] 2026-05-12T01:17:58Z operator=Brandon user: Trac |
| 8 | keyword | — | 0.0277 | SNAP-20260512-6592 |  |  | Raw Session Log - [1] 2026-05-12T02:09:22Z operator=Brandon user: Bran |
| 9 | keyword | — | 0.0270 | SNAP-20260512-6602 |  |  | Raw Session Log - [1] 2026-05-12T18:59:57Z operator=Brandon user: Wiza |
| 10 | keyword | — | 0.0265 | SNAP-20260511-6583 |  |  | Raw Session Log - [1] 2026-05-11T18:52:24Z operator=Brandon user: all  |

_Evicted semantic hits: SNAP-20260317-4492, SNAP-20260514-6608, SNAP-20260623-7609, SNAP-20260124-2661, SNAP-20260617-7111, SNAP-20260517-6699, SNAP-20260510-6558, SNAP-20260226-3833, SNAP-20260422-6177_

### 'frontier model device control phone tablet over Tailscale'  (operator=Brandon, keyword-only 8/10, evicted 9)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.7077 | 0.2880 | SNAP-20260702-7920 |  |  | Raw Session Log - [1] 2026-07-02T18:35:47Z operator=Brandon user: Fron |
| 2 | both | 0.6939 | 0.2482 | SNAP-20260701-7890 |  |  | Raw Session Log - [1] 2026-07-01T01:28:00Z operator=Brandon user: Fron |
| 3 | keyword | — | 0.2253 | SNAP-20260619-7181 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 4 | keyword | — | 0.1707 | SNAP-20260619-7185 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 5 | keyword | — | 0.1559 | SNAP-20260619-7188 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 6 | keyword | — | 0.1004 | SNAP-20260619-7191 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 7 | keyword | — | 0.0556 | SNAP-20260619-7184 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 8 | keyword | — | 0.0466 | SNAP-20260619-7182 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 9 | keyword | — | 0.0435 | SNAP-20260628-7825 |  |  | Raw Session Log - [1] 2026-06-28T21:58:26Z operator=Brandon user: And  |
| 10 | keyword | — | 0.0454 | SNAP-20260619-7189 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |

_Evicted semantic hits: SNAP-20260405-5592, SNAP-20260221-3691, SNAP-20260217-3522, SNAP-20260619-7186, SNAP-20260702-7926, SNAP-20260327-4926, SNAP-20260702-7929, SNAP-20260314-4388, SNAP-20260220-3642_

### 'cron scheduler restart catch-up validation exec safety'  (operator=Brandon, keyword-only 6/10, evicted 7)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | keyword | — | 0.0362 | SNAP-20260508-6519 |  |  | Raw Session Log - [1] 2026-05-08T19:22:55Z operator=Brandon user: DEVE |
| 2 | both | 0.6596 | 0.0514 | SNAP-20260402-5413 |  |  | Raw Session Log - [1] 2026-04-02T15:00:07Z operator=Brandon user: So,  |
| 3 | both | 0.6363 | 0.0941 | SNAP-20260206-3144 |  |  | Raw Session Log - [1] 2026-02-06T18:47:29Z operator=Brandon user: [Sch |
| 4 | keyword | — | 0.0123 | SNAP-20260520-6724 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 5 | keyword | — | 0.0087 | SNAP-20260517-6697 |  |  | Raw Session Log - [1] 2026-05-17T15:44:38Z operator=Brandon user: API  |
| 6 | keyword | — | 0.0041 | SNAP-20260625-7726 |  |  | Raw Session Log - [1] 2026-06-25T21:31:48Z operator=Brandon user: Uh,  |
| 7 | keyword | — | 0.0054 | SNAP-20260607-6931 |  |  | Raw Session Log - [1] 2026-06-07T06:52:45Z operator=Brandon user: Rebu |
| 8 | keyword | — | 0.0055 | SNAP-20260514-6608 |  |  | Raw Session Log - [1] 2026-05-14T00:35:32Z operator=Brandon user: Trac |
| 9 | semantic | 0.6184 | 0.0105 | SNAP-20260630-7864 |  |  | Raw Session Log - [1] 2026-06-30T01:27:32Z operator=Brandon user: Elev |
| 10 | semantic | 0.6319 | 0.0105 | SNAP-20260514-6609 |  |  | Raw Session Log - [1] 2026-05-14T02:10:20Z operator=Brandon user: Very |

_Evicted semantic hits: SNAP-20260529-6856, SNAP-20260518-6707, SNAP-20260207-3162, SNAP-20260517-6696, SNAP-20260513-6606, SNAP-20260706-8009, SNAP-20260206-3127_

### 'reranker tiering body-only Cohere Vertex hardware'  (operator=Brandon, keyword-only 7/10, evicted 7)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.7025 | 0.0603 | SNAP-20260705-7983 |  |  | Raw Session Log - [1] 2026-07-05T00:25:30Z operator=Brandon user: Use  |
| 2 | keyword | — | 0.0340 | SNAP-20260705-7992 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 3 | both | 0.6863 | 0.0304 | SNAP-20260705-7990 |  |  | Raw Session Log - [1] 2026-07-05T06:03:08Z operator=Brandon user: No,  |
| 4 | keyword | — | 0.0105 | SNAP-20260628-7825 |  |  | Raw Session Log - [1] 2026-06-28T21:58:26Z operator=Brandon user: And  |
| 5 | keyword | — | 0.0109 | SNAP-20260619-7183 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 6 | keyword | — | 0.0113 | SNAP-20260329-5098 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 7 | keyword | — | 0.0067 | SNAP-20260506-6476 |  |  | Raw Session Log - [1] 2026-05-06T19:04:36Z operator=Brandon user: yeah |
| 8 | keyword | — | 0.0113 | SNAP-20260218-3579 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 9 | keyword | — | 0.0085 | SNAP-20260417-5990 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 10 | semantic | 0.6744 | 0.0088 | SNAP-20260627-7782 |  |  | Raw Session Log - [1] 2026-06-27T16:32:44Z operator=Brandon user: Comp |

_Evicted semantic hits: SNAP-20260423-6210, SNAP-20260704-7958, SNAP-20260628-7823, SNAP-20260703-7949, SNAP-20260127-2762, SNAP-20260707-8065, SNAP-20260303-4033_

### 'on-device Gemma phone litertlm native tool loop'  (operator=Brandon, keyword-only 3/10, evicted 5)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.7613 | 0.2683 | SNAP-20260630-7888 |  |  | Raw Session Log - [1] 2026-06-30T23:45:41Z operator=Brandon user: On-d |
| 2 | both | 0.7252 | 0.2498 | SNAP-20260619-7187 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 3 | keyword | — | 0.2130 | SNAP-20260619-7185 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 4 | keyword | — | 0.1599 | SNAP-20260619-7181 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 5 | keyword | — | 0.1483 | SNAP-20260701-7890 |  |  | Raw Session Log - [1] 2026-07-01T01:28:00Z operator=Brandon user: Fron |
| 6 | both | 0.7481 | 0.2055 | SNAP-20260616-7093 |  |  | Raw Session Log - [1] 2026-06-16T01:13:42Z operator=Brandon user: Chec |
| 7 | semantic | 0.7385 | 0.1529 | SNAP-20260616-7095 |  |  | Raw Session Log - [1] 2026-06-16T22:06:39Z operator=Brandon user: Say  |
| 8 | both | 0.7351 | 0.1486 | SNAP-20260619-7182 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 9 | both | 0.7644 | 0.2037 | SNAP-20260614-7066 |  |  | Raw Session Log - [1] 2026-06-14T19:05:52Z operator=Brandon user: Desi |
| 10 | both | 0.7255 | 0.1354 | SNAP-20260619-7190 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |

_Evicted semantic hits: SNAP-20260617-7149, SNAP-20260619-7186, SNAP-20260615-7085, SNAP-20260702-7917, SNAP-20260619-7183_

### 'website revamp landing page Firebase pricing Stripe'  (operator=Brandon, keyword-only 5/10, evicted 6)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.7356 | 0.0948 | SNAP-20260627-7782 |  |  | Raw Session Log - [1] 2026-06-27T16:32:44Z operator=Brandon user: Comp |
| 2 | both | 0.6523 | 0.0208 | SNAP-20260617-7147 |  |  | Raw Session Log - [1] 2026-06-17T18:43:50Z operator=Brandon user: Yeah |
| 3 | both | 0.6503 | 0.1128 | SNAP-20260213-3430 |  |  | Raw Session Log - [1] 2026-02-13T22:25:13Z operator=Brandon user: DEVE |
| 4 | keyword | — | 0.0164 | SNAP-20260629-7845 |  |  | Raw Session Log - [1] 2026-06-29T09:34:17Z operator=Brandon user: ET,  |
| 5 | keyword | — | 0.0203 | SNAP-20260517-6703 |  |  | Raw Session Log - [1] 2026-05-17T22:06:26Z operator=Brandon user: E18  |
| 6 | keyword | — | 0.0142 | SNAP-20260628-7820 |  |  | Raw Session Log - [1] 2026-06-28T18:51:51Z operator=Brandon user: Yeah |
| 7 | keyword | — | 0.0125 | SNAP-20260509-6532 |  |  | Raw Session Log - [1] 2026-05-09T19:31:10Z operator=Brandon user: Snap |
| 8 | keyword | — | 0.0073 | SNAP-20260517-6704 |  |  | Raw Session Log - [1] 2026-05-17T22:54:15Z operator=Brandon user: COMP |
| 9 | both | 0.6593 | 0.0265 | SNAP-20260208-3216 |  |  | Raw Session Log - [1] 2026-02-08T22:33:47Z operator=Brandon user: I wa |
| 10 | both | 0.6259 | 0.0354 | SNAP-20260119-2409 |  |  | Raw Session Log - [1] 2026-01-19T22:36:57Z operator=Brandon user: DEVE |

_Evicted semantic hits: SNAP-20260118-2390, SNAP-20260213-3414, SNAP-20260318-4542, SNAP-20260628-7819, SNAP-20260208-3218, SNAP-20260318-4538_

### 'SMS inbound routing thread memory peer takeover'  (operator=Brandon, keyword-only 6/10, evicted 8)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.6959 | 0.0324 | SNAP-20260626-7743 |  |  | Raw Session Log - [1] 2026-06-26T02:29:29Z operator=Brandon user: [SMS |
| 2 | semantic | 0.6784 | 0.0260 | SNAP-20260626-7741 |  |  | Raw Session Log - [1] 2026-06-26T00:41:38Z operator=Brandon content: [ |
| 3 | both | 0.7015 | 0.0524 | SNAP-20260327-4925 |  |  | Raw Session Log - [1] 2026-03-27T10:45:17Z operator=Brandon user: DEVE |
| 4 | keyword | — | 0.0161 | SNAP-20260704-7963 |  |  | Raw Session Log - [1] 2026-07-04T11:01:40Z operator=Brandon user: [Sch |
| 5 | keyword | — | 0.0179 | SNAP-20260530-6870 |  |  | Raw Session Log - [1] 2026-05-30T17:54:36Z operator=Brandon user: now  |
| 6 | keyword | — | 0.0131 | SNAP-20260702-7908 |  |  | Raw Session Log - [1] 2026-07-02T11:01:48Z operator=Brandon user: [Sch |
| 7 | keyword | — | 0.0135 | SNAP-20260625-7726 |  |  | Raw Session Log - [1] 2026-06-25T21:31:48Z operator=Brandon user: Uh,  |
| 8 | keyword | — | 0.0298 | SNAP-20260217-3564 |  |  | Raw Session Log - [1] 2026-02-17T23:26:56Z operator=Brandon user: DEVE |
| 9 | keyword | — | 0.0127 | SNAP-20260701-7897 |  |  | Raw Session Log - [1] 2026-07-01T11:01:58Z operator=Brandon user: [Sch |
| 10 | both | 0.6854 | 0.0235 | SNAP-20260209-3240 |  |  | Raw Session Log - [1] 2026-02-09T10:51:00Z operator=Brandon user: yeah |

_Evicted semantic hits: SNAP-20260609-6961, SNAP-20260203-2959, SNAP-20260203-2956, SNAP-20260530-6868, SNAP-20260626-7742, SNAP-20260326-4862, SNAP-20260630-7883, SNAP-20260328-5040_

### 'per-card re-embed chunked store schema upgrade'  (operator=Brandon, keyword-only 4/10, evicted 6)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.6824 | 0.0810 | SNAP-20260703-7941 |  |  | Raw Session Log - [1] 2026-07-03T13:44:55Z operator=Brandon user: Retr |
| 2 | both | 0.6294 | 0.0364 | SNAP-20260702-7907 |  |  | Raw Session Log - [1] 2026-07-02T02:57:07Z operator=Brandon user: Scop |
| 3 | both | 0.6609 | 0.0321 | SNAP-20260702-7906 |  |  | Raw Session Log - [1] 2026-07-02T02:26:50Z operator=Brandon user: Audi |
| 4 | keyword | — | 0.0236 | SNAP-20260605-6912 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 5 | keyword | — | 0.0188 | SNAP-20260612-7013 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 6 | keyword | — | 0.0131 | SNAP-20260630-7864 |  |  | Raw Session Log - [1] 2026-06-30T01:27:32Z operator=Brandon user: Elev |
| 7 | keyword | — | 0.0066 | SNAP-20260702-7927 |  |  | Raw Session Log - [1] 2026-07-02T20:07:51Z operator=Brandon user: [Voi |
| 8 | semantic | 0.6369 | 0.0101 | SNAP-20260623-7609 |  |  | Raw Session Log - [1] 2026-06-23T10:30:33Z operator=Brandon user: Okay |
| 9 | semantic | 0.6366 | 0.0104 | SNAP-20260628-7820 |  |  | Raw Session Log - [1] 2026-06-28T18:51:51Z operator=Brandon user: Yeah |
| 10 | both | 0.6367 | 0.0138 | SNAP-20260704-7952 |  |  | Raw Session Log - [1] 2026-07-04T04:35:16Z operator=Brandon user: All  |

_Evicted semantic hits: SNAP-20260612-7011, SNAP-20260628-7823, SNAP-20260617-7149, SNAP-20260613-7031, SNAP-20260211-3369, SNAP-20260614-7060_

### 'Android terminal mic live speech to text partials'  (operator=Brandon, keyword-only 7/10, evicted 8)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.7078 | 0.2662 | SNAP-20260629-7834 |  |  | Raw Session Log - [1] 2026-06-29T02:29:51Z operator=Brandon user: Andr |
| 2 | both | 0.6837 | 0.1295 | SNAP-20260606-6930 |  |  | Raw Session Log - [1] 2026-06-06T05:11:13Z operator=Brandon user: CLI  |
| 3 | both | 0.6522 | 0.0959 | SNAP-20260629-7845 |  |  | Raw Session Log - [1] 2026-06-29T09:34:17Z operator=Brandon user: ET,  |
| 4 | keyword | — | 0.0760 | SNAP-20260630-7864 |  |  | Raw Session Log - [1] 2026-06-30T01:27:32Z operator=Brandon user: Elev |
| 5 | keyword | — | 0.0466 | SNAP-20260604-6900 |  |  | Raw Session Log - [1] 2026-06-04T18:56:35Z operator=Brandon user: Rese |
| 6 | keyword | — | 0.0466 | SNAP-20260605-6912 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 7 | keyword | — | 0.0373 | SNAP-20260613-7030 |  |  | Raw Session Log - [1] 2026-06-13T21:27:06Z operator=Brandon user: Elev |
| 8 | keyword | — | 0.0368 | SNAP-20260531-6872 |  |  | Raw Session Log - [1] 2026-05-31T01:43:38Z operator=Brandon user: Andr |
| 9 | keyword | — | 0.0338 | SNAP-20260530-6868 |  |  | Raw Session Log - [1] 2026-05-30T14:43:26Z operator=Brandon user: So,  |
| 10 | keyword | — | 0.0411 | SNAP-20260425-6254 |  |  | Raw Session Log - [1] 2026-04-25T00:26:14Z operator=Brandon user: Plan |

_Evicted semantic hits: SNAP-20260410-5871, SNAP-20260429-6342, SNAP-20251217-1914, SNAP-20260316-4463, SNAP-20251010-177, SNAP-20260605-6914, SNAP-20260501-6370, SNAP-20251008-165_

### 'operator picker device unassign owner reassign'  (operator=Brandon, keyword-only 9/10, evicted 9)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | keyword | — | 0.0248 | SNAP-20260702-7920 |  |  | Raw Session Log - [1] 2026-07-02T18:35:47Z operator=Brandon user: Fron |
| 2 | semantic | 0.6357 | 0.0262 | SNAP-20260531-6875 |  |  | Raw Session Log - [1] 2026-05-31T22:34:46Z operator=Brandon user: Dyna |
| 3 | keyword | — | 0.0152 | SNAP-20260702-7926 |  |  | Raw Session Log - [1] 2026-07-02T19:54:22Z operator=Brandon user: Gala |
| 4 | keyword | — | 0.0138 | SNAP-20260701-7890 |  |  | Raw Session Log - [1] 2026-07-01T01:28:00Z operator=Brandon user: Fron |
| 5 | keyword | — | 0.0143 | SNAP-20260619-7181 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 6 | keyword | — | 0.0112 | SNAP-20260705-7999 |  |  | Raw Session Log - [1] 2026-07-05T17:35:32Z operator=Brandon user: This |
| 7 | keyword | — | 0.0104 | SNAP-20260614-7066 |  |  | Raw Session Log - [1] 2026-06-14T19:05:52Z operator=Brandon user: Desi |
| 8 | keyword | — | 0.0099 | SNAP-20260617-7109 |  |  | Raw Session Log - [1] 2026-06-17T04:51:27Z operator=Brandon user: On-d |
| 9 | keyword | — | 0.0098 | SNAP-20260619-7190 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 10 | keyword | — | 0.0073 | SNAP-20260628-7805 |  |  | Raw Session Log - [1] 2026-06-28T01:02:01Z operator=Brandon user: Fix  |

_Evicted semantic hits: SNAP-20260209-3240, SNAP-20260614-7062, SNAP-20251018-583, SNAP-20260408-5782, SNAP-20260205-3098, SNAP-20260622-7597, SNAP-20260321-4679, SNAP-20260224-3753, SNAP-20260331-5264_

### 'MCP remote tool server OAuth Tailscale funnel bearer'  (operator=Brandon, keyword-only 8/10, evicted 8)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.7755 | 0.3153 | SNAP-20260627-7777 |  |  | Raw Session Log - [1] 2026-06-27T07:07:38Z operator=Brandon user: Buil |
| 2 | keyword | — | 0.1063 | SNAP-20260627-7783 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 3 | both | 0.7091 | 0.0543 | SNAP-20260627-7780 |  |  | Raw Session Log - [1] 2026-06-27T14:20:46Z operator=Brandon user: Make |
| 4 | keyword | — | 0.0448 | SNAP-20260531-6875 |  |  | Raw Session Log - [1] 2026-05-31T22:34:46Z operator=Brandon user: Dyna |
| 5 | keyword | — | 0.0147 | SNAP-20260630-7888 |  |  | Raw Session Log - [1] 2026-06-30T23:45:41Z operator=Brandon user: On-d |
| 6 | keyword | — | 0.0387 | SNAP-20260329-5135 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 7 | keyword | — | 0.0146 | SNAP-20260627-7782 |  |  | Raw Session Log - [1] 2026-06-27T16:32:44Z operator=Brandon user: Comp |
| 8 | keyword | — | 0.0148 | SNAP-20260607-6931 |  |  | Raw Session Log - [1] 2026-06-07T06:52:45Z operator=Brandon user: Rebu |
| 9 | keyword | — | 0.0166 | SNAP-20260521-6752 |  |  | Raw Session Log - [1] 2026-05-21T14:39:11Z operator=Brandon user: okay |
| 10 | keyword | — | 0.0115 | SNAP-20260622-7588 |  |  | Raw Session Log - [1] 2026-06-22T16:14:18Z operator=Brandon user: Okay |

_Evicted semantic hits: SNAP-20251206-1593, SNAP-20260429-6342, SNAP-20260217-3544, SNAP-20260516-6664, SNAP-20251012-228, SNAP-20260329-5132, SNAP-20251006-152, SNAP-20260218-3583_

### 'generation ember backdrop particles setting portal android'  (operator=Brandon, keyword-only 9/10, evicted 9)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | keyword | — | 0.0755 | SNAP-20260521-6748 |  |  | Raw Session Log - [1] 2026-05-21T00:14:06Z operator=Brandon user: Hamb |
| 2 | keyword | — | 0.0547 | SNAP-20260521-6749 |  |  | Raw Session Log - [1] 2026-05-21T00:41:31Z operator=Brandon user: End- |
| 3 | keyword | — | 0.0454 | SNAP-20260520-6741 |  |  | Raw Session Log - [1] 2026-05-20T21:22:04Z operator=Brandon user: Port |
| 4 | keyword | — | 0.0129 | SNAP-20260531-6872 |  |  | Raw Session Log - [1] 2026-05-31T01:43:38Z operator=Brandon user: Andr |
| 5 | keyword | — | 0.0082 | SNAP-20260705-7999 |  |  | Raw Session Log - [1] 2026-07-05T17:35:32Z operator=Brandon user: This |
| 6 | semantic | 0.6210 | 0.0358 | SNAP-20260316-4445 |  |  | Raw Session Log - [1] 2026-03-16T21:40:56Z operator=Brandon user: DEVE |
| 7 | keyword | — | 0.0117 | SNAP-20260512-6603 |  |  | Raw Session Log - [1] 2026-05-12T21:55:55Z operator=Brandon user: Phas |
| 8 | keyword | — | 0.0096 | SNAP-20260531-6873 |  |  | Raw Session Log - [1] 2026-05-31T19:53:24Z operator=Brandon user: TTS  |
| 9 | keyword | — | 0.0099 | SNAP-20260501-6370 |  |  | Raw Session Log - [1] 2026-05-01T11:32:35Z operator=Brandon user: Cont |
| 10 | keyword | — | 0.0066 | SNAP-20260530-6868 |  |  | Raw Session Log - [1] 2026-05-30T14:43:26Z operator=Brandon user: So,  |

_Evicted semantic hits: SNAP-20260118-2386, SNAP-20251010-170, SNAP-20260317-4492, SNAP-20260209-3228, SNAP-20260209-3229, SNAP-20260411-5892, SNAP-20260316-4432, SNAP-20251220-1962, SNAP-20260119-2397_

### 'Google Workspace Docs Sheets Slides Drive Calendar tools'  (operator=Brandon, keyword-only 7/10, evicted 9)

| # | channel | cosine | rerank | snap_id | rel | judge | snippet |
|---|---|---|---|---|---|---|---|
| 1 | both | 0.6410 | 0.0871 | SNAP-20260622-7586 |  |  | Raw Session Log - [1] 2026-06-22T14:11:11Z operator=Brandon user: Okay |
| 2 | both | 0.6877 | 0.0650 | SNAP-20260622-7584 |  |  | Raw Session Log - [1] 2026-06-22T10:37:22Z operator=Brandon user: Got  |
| 3 | both | 0.6420 | 0.0299 | SNAP-20260628-7829 |  |  | Raw Session Log - [1] 2026-06-28T22:29:33Z operator=Brandon user: Okay |
| 4 | keyword | — | 0.0245 | SNAP-20260706-8008 |  |  | Raw Session Log - [1] 2026-07-06T00:25:29Z operator=Brandon user: Look |
| 5 | keyword | — | 0.0204 | SNAP-20260627-7782 |  |  | Raw Session Log - [1] 2026-06-27T16:32:44Z operator=Brandon user: Comp |
| 6 | keyword | — | 0.0267 | SNAP-20260329-5097 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |
| 7 | keyword | — | 0.0285 | SNAP-20260304-4058 |  |  | Raw Session Log - [1] 2026-03-04T15:24:39Z operator=Brandon user: Yeah |
| 8 | keyword | — | 0.0190 | SNAP-20260607-6931 |  |  | Raw Session Log - [1] 2026-06-07T06:52:45Z operator=Brandon user: Rebu |
| 9 | keyword | — | 0.0191 | SNAP-20260517-6703 |  |  | Raw Session Log - [1] 2026-05-17T22:06:26Z operator=Brandon user: E18  |
| 10 | keyword | — | 0.0236 | SNAP-20260329-5098 |  |  | Raw Session Log - [1] [CHECKPOINT] Compressed summary of 26 snapshots  |

_Evicted semantic hits: SNAP-20260423-6212, SNAP-20260124-2665, SNAP-20260629-7846, SNAP-20260628-7828, SNAP-20260302-4016, SNAP-20260422-6187, SNAP-20250919-107, SNAP-20260323-4731, SNAP-20260321-4678_
