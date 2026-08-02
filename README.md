# HILCA — Project Context for Claude Code

## What this is
A multi-agent "dialectic" system: a Central Control Unit (CCU) casts five
specialist sub-agents that debate a submitted topic across rounds, then
synthesize a final answer. This repo implements **HILCA master reference v2**
(`reference/HILCA_master_reference_v2.txt`) — the current master file, which
supersedes the raw UiPath prototype prompts. The original .uis and older docx
remain in `reference/` for provenance.

## Layout
```
dialectic.py, prompts.py, context_gathering.py,
retrieval.py, controls.py, dialectic_stream.py,
db.py, llm.py, schemas.py, web.py   — the product code
controller.py, debate.py,
stream.py, api.py                   — legacy M1/M2 modules (kept, tested, superseded)
test_*.py                           — the test suite
frontend/                           — the live SPA (served by web.py)
reference/                          — HILCA_master_reference_v2.txt (CURRENT),
                                      old docx + the original .uis (provenance)
docs/                               — form spec + historical reproduction docs
runs/                               — per-run artifacts (.md docs + run_log.csv); gitignored
hilca_live.db                       — the live run database; gitignored
```

## The flow (master file v2)
- `prompts.py` — the refined prompt pack, ported 1:1 from the master file:
  the **Behavioral Protocol** ("Yes, And…", non-binary thinking, conditional
  exploration) in every call; **EXACTLY 5** role cards
  (`{name, role, persona, directive, rubric}`, Pydantic-validated); two-part
  CCU wrap-ups (SECTION 1 audit + SECTION 2 next-round directives); the
  sequential handoff chain (S1→S2→S3→S4→S5→CCU, S3 = reconciler, S5 = weaver);
  the P8 Final Synthesis & Executive Audit Report (audit + quality rating +
  Master Synthesis & Roadmap + Conditional Nuances); the P11 800-character
  Executive Summarizer log entry. **Do not paraphrase them.**
- `context_gathering.py` — Phase 2: master reference + evidence URL scraping +
  PDF extraction → `ContextMaterial`.
- `retrieval.py` — hybrid index over the ContextMaterial: BM25 + exact-phrase
  (keyword half) fused with embedding cosine (vector half — OpenAI embeddings
  or the offline hash embedder; in-memory vector store, failures degrade to
  BM25-only). Powers targeted per-turn chunks in stateless mode and the
  Gap-Analysis loop-back.
- `dialectic.py` — the engine:
  1. Round 1: CCU cast (P2) → operator approval → **sequential cascade** of
     opening theses (agent i sees theses 1..i-1) → CCU two-part wrap-up (P5).
  2. Main loop: agents answer the previous SECTION 2 directives (P6, with
     machine-readable `STANCE:` + `CONVERGENCE:` footers) → CCU wrap-up →
     sliding round summary. **Early exit** when the average convergence vote
     clears `HILCA_CONVERGENCE_THRESHOLD` (0.85) or stances plateau.
  3. Final loop: CCU final directive (P7) → closing statements (P6F).
  4. Closing: **Devil's Advocate validation** → **Gap Analysis** (+ targeted
     retrieval fed into P8) → P8 synthesis → P9 verdicts → P10 deliverable →
     P11 log summary → email/download.
  Artifacts per run under `runs/<run_id>/`: one .md per message +
  `run_log.csv` + SQLite rows + the `debate_state` ledger (stances/votes).
  **Checkpoints**: a JSON snapshot at every round boundary (`checkpoints`
  table); a crashed run resumes via `run(run_id, resume=True)` or
  `GET /api/runs/{id}/stream?resume=1` — completed rounds are never re-billed.
- `llm.py` — provider-agnostic (`mock` | `anthropic` | `openai`) with
  **tiered model routing** (`low` summaries / `mid` dialectic / `high` final
  synthesis; `HILCA_MODEL_LOW/MID/HIGH`), **temperature 0 by default**
  (`HILCA_TEMPERATURE`), and **Anthropic prompt caching** of the static heavy
  blocks (`HILCA_PROMPT_CACHE`, default on).
- `dialectic_stream.py` + `web.py` + `frontend/` — SSE-streamed live UI.
- **Human-in-the-loop** (`controls.py` + `interventions` table):
  - **Cast approval** — the run holds (status `awaiting_approval`) while the
    operator reviews and edits the five role cards, then approves.
    `POST /api/runs/{id}/approve {roster}` (exactly 5 cards). Disable with
    `HILCA_REQUIRE_APPROVAL=0`.
  - **Pause/resume** — before the next LLM call; **Feedback** — to the CCU or
    any sub-agent, injected attributed into the target's next prompt.

Agreed mechanizations (client-approved 2026-07-28, everything else verbatim):
1. **Exactly five agents** (per the master file; the earlier dynamic-cast
   deviation is retired). The JSON role-card array is the parse mechanism.
2. **Real round counters** (the original UiPath loop gate never incremented).
3. **P9 verdicts + P10 single-document deliverable** are retained phase-2
   additions, restructured to carry the refined sections (System Audit,
   Master Synthesis & Roadmap, Conditional Nuances). `deliverable.md` is
   format-checked with one retry and a deterministic fallback.
4. **STANCE/CONVERGENCE footers** implement the master file's
   Critique-Refine-Vote convergence voting and granular debate state.

Safety guards (do not remove): explicit `RoundNum`, a total-LLM-call ceiling
(cast+retry, per-round N+2, closing N+7), a cumulative token budget.

### Run it
```
pip install -r requirements.txt
# set OPENAI_API_KEY + LLM_PROVIDER=openai in .env for a real run
start.bat            # or: python -m uvicorn web:app --reload
# open http://localhost:8000
```

### Config (env)
`LLM_PROVIDER` (mock|openai|anthropic), `OPENAI_MODEL`/`ANTHROPIC_MODEL`
(base models), `HILCA_MODEL_LOW/MID/HIGH` (tier overrides),
`HILCA_TEMPERATURE` (0; 'none' = provider default), `HILCA_PROMPT_CACHE` (1),
`HILCA_MAIN_ROUNDS` (3), `HILCA_FINAL_ROUNDS` (3), `HILCA_TOKEN_BUDGET` (2M),
`HILCA_CCU_MAX_TOKENS`, `HILCA_AGENT_MAX_TOKENS`, `HILCA_SUMMARY_MAX_TOKENS`,
`HILCA_CONVERGENCE_EXIT` (1), `HILCA_CONVERGENCE_THRESHOLD` (0.85),
`HILCA_STANCE_SIMILARITY` (0.90), `HILCA_STANCE_STABLE_ROUNDS` (2),
`HILCA_TARGETED_RETRIEVAL` (1), `HILCA_RUNNING_SUMMARY_CAP` (8000),
`HILCA_EMBEDDINGS` (auto|openai|hash|off — the vector half of hybrid
retrieval; auto = OpenAI embeddings when a key is set and not mock, else the
offline hash embedder), `HILCA_EMBED_MODEL` (text-embedding-3-small),
`HILCA_HYBRID_ALPHA` (0.5 — vector-half weight in the fused ranking),
`HILCA_REFERENCE_PATH`, `HILCA_MAX_REFERENCE_CHARS`, `HILCA_MAX_SOURCE_CHARS`,
`HILCA_MAX_CONTEXT_CHARS`, `HILCA_MAX_FETCH_BYTES`, `HILCA_DOCS_ROOT` (runs),
`HILCA_THREADED` (1), `HILCA_THREAD_KEEP` (8), `SMTP_HOST/PORT/USER/PASS/FROM`
(deliverable email, optional), `DB_PATH`.
Rate limits: real calls retry 429s with backoff (`HILCA_LLM_RETRIES`, default 5,
honoring the provider's suggested wait); `HILCA_TPM_LIMIT` (org tokens/min,
0=off) paces calls under the rolling-minute budget; a single request larger
than the org limit fails fast with instructions to lower the context caps —
see the 30k-TPM recipe in `.env.example`.
The web stream refuses the mock provider unless `HILCA_ALLOW_MOCK=1`
(offline demos only), refuses to restart a run that already started (use
`?resume=1` for interrupted runs), and only accepts `http(s)` evidence URLs.

## Stack
- Python 3.12+, Pydantic v2 for data contracts
- SQLite (stdlib `sqlite3`) — schema in `db.py`
- FastAPI + uvicorn; SSE streaming to a vanilla-JS SPA in `frontend/`
- Provider-agnostic LLM layer (`llm.py`); `mock` runs the whole flow offline.
- Optional: `pypdf` for PDF evidence extraction.

## Legacy modules (kept, not the primary flow)
`controller.py` (M1 spawn), `debate.py` (M2 convergence-referee loop),
`stream.py`, `api.py` (M1 form app). Their tests still run. The flow above
supersedes them as the product path; don't delete them without asking.

## Conventions
- Every cross-boundary object is a validated Pydantic model.
- Business logic calls `LLMClient.complete/chat` only — keep provider details
  out of the engine.
- Fail loudly: validation/LLM errors set run status to `error` and raise.
- The prompt pack in `prompts.py` is contractually verbatim from
  `reference/HILCA_master_reference_v2.txt`; changes to the wording require
  explicit client sign-off.
- Match existing style: type hints, short docstrings, no over-engineering.

## Tests
```
python -m pytest -q --basetemp=%TEMP%\pytest-hilca
```
(`--basetemp` because the default pytest temp root is not writable on this box.)

## Do not
- Do not read or print `.env` / secrets (see `.claudeignore`).
- Do not paraphrase or "improve" the prompt-pack wording.
- Do not change the five-agent cast size (master file mandate).
- Do not add a synthesis step earlier than P8.
- Do not delete the `mock` provider — it's how tests run for free; never use
  it for a real run.
