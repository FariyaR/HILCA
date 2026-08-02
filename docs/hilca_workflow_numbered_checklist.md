 > **SUPERSEDED (2026-07-28):** this checklist documents the original UiPath
> port. The current source of truth is `reference/HILCA_master_reference_v2.txt`
> (the refined prompt pack: Behavioral Protocol, exactly-5 role cards, two-part
> CCU wrap-ups, convergence voting, Devil's Advocate/Gap-Analysis closing).
> See README.md and prompts.py. Kept for provenance.

# HILCA Workflow — Literal 1:1 Build Checklist (his exact order)

Give this to Claude Code **together with** `hilca_workflow_reproduction_spec.md`
(which holds the verbatim prompts P1–P8). This checklist is the ordered step list;
that file is the prompt text.

**Faithful to his order.** Numbers in `(his #…)` reference the original UiPath
activity indices so this maps 1:1 to his workflow. Two agreed adjustments:
- The 5 hardcoded `SubAgent 1–5` blocks are flattened to **`for each agent in
  roster`** (dynamic N).
- His 32 `Column A–AF` write activities per row are collapsed to one **Write Row**
  (with a row-per-message schema, since a fixed 32-col row can't hold a dynamic
  agent count).
- The loop bounds (`< 40`, `< 3`) use a **real incrementing counter** instead of
  the original's non-incrementing sheet-row-index (the original bug).

---

## PHASE 1 — INTAKE
1. **Trigger:** Row Added to the Bottom of the Intake Sheet. *(his #1)*
2. **Read row fields:** Timestamp, Topic, Tag 1–5, Agent 1–3 (role hints), Email,
   Evidence URL 1–3. *(his #2–16)*
3. **Mark row** Processed = TRUE; generate and write **Run_ID**. *(his #17–18)*

## PHASE 2 — CONTEXT GATHERING
4. **Download** the HILCA MasterReference file → read its text. *(his #19)*
5. `log: "new row read and masterreference downloaded"`. *(his #20)*
6. **Web Reader** — scrape Intake URL 1. *(his #21)*
7. **Web Reader** — scrape Intake URL 2. *(his #22)*
8. **Web Reader** — scrape Intake URL 3. *(his #23)*
9. `log`. *(his #24)*
10. **Download files from URL cells** *(his #25–39)*:
    - Read cell 1; **if not empty** → download file from URL 1; if success → `log`.
    - Read cell 2; **if not empty** → download file from URL 2; if success → `log`.
    - Read cell 3; **if not empty** → download file from URL 3; if success → `log`.
    - (PDFs → extract text.)
11. `log: "supportive intake websites and files were scraped and downloaded"`. *(his #40)*
12. **Assemble** `ContextMaterial` = MasterReference text + scraped URL text +
    downloaded/extracted file text.

## PHASE 3 — ROUND 1 (cast creation + opening theses)
13. **CCU Creates Agent Roles** — LLM call, system = **P1**, user = **P2** (dynamic
    count; return JSON array) → parse into `AgentRoster` (sets N). *(his #41)*
14. **Create Folder** record for this row. *(his #42)*
15. **Create Document + Write Text** — save the CCU's primary agent prompts. *(his #43–44)*
16. `log: "CCU created the roles and were saved in doc file"`. *(his #45)*
17. **FOR EACH agent in `AgentRoster`** *(flattens his #46–65, SubAgent 1–5)*:
    - LLM call — system = **P3**, user = **P4** → opening thesis.
    - **Create Document + Write Text** — save this agent's thesis.
    - `log: "agent {name} created thesis and was saved"`.
    - Append the thesis to `RoundResponses`.
18. `log: "First round is complete. Now CCU sums up Round 1"`. *(his #66)*
19. **CCU Wraps up Round 1** — LLM call, system = **P1**, user = **P5** → round-1
    summary. *(his #67)*
20. **Create Document + Write Text** — save the wrap-up. *(his #68–69)*
21. `log: "Round 1 Wrap Up Report Doc Saved"`. *(his #70)*
22. **Create Spreadsheet** (the run log). *(his #71)*
23. **Write Row** — round-1 record (row-per-message: RunID, round, agent, type,
    text). *(his #72–137, columns collapsed)*
24. `log: "Round 1 Ended And CCU Saved The Round Wrap Up Report Sheet"`. *(his #138)*

## PHASE 4 — MAIN ROUND LOOP
25. `log: "Initiating Round Loop"`. *(his #139)*
26. **WHILE `RoundNum < MainRounds`** (real incrementing counter; original was
    `< 40`) *(his #140)* — increment `RoundNum` each pass:
    a. `log: "CCU Generating Prompt for the Next Round"`. *(his #142)*
    b. **CCU next-round agenda** — LLM call, system = **P1**, user = **P5** (inject
       roster + prior `RoundResponses`) → `CcuAgenda`. *(his #143)*
    c. **Create Document + Write Text** — save the CCU round prompt. *(his #143–144)*
    d. `log`. *(his #145)*
    e. **FOR EACH agent in `AgentRoster`** *(flattens his #146–160)*:
       - LLM call — system = **P3**, user = **P6** (inject `CcuAgenda`, roster, prior
         responses) → response.
       - **Create Document + Write Text** — save response.
       - `log: "SubAgent {name} Thesis created and Saved"`.
    f. Replace `RoundResponses` with this round's responses.
    g. **CCU Wraps up Each Round** — LLM call, system = **P1**, user = **P5**. *(his #161)*
    h. **Create Document + Write Text**. *(his #162)*
    i. `log: "CCU Wrapped Up the Past Round and Saved Doc"`. *(his #163)*
    j. **Write Row** — round record. *(his #164–196, columns collapsed)*
    k. `log: "Round Ended And Sheet Updated"`. *(his #197)*

## PHASE 5 — FINAL CONCLUSIVE LOOP
27. `log: "Loop Round Ended and Final Round Begins"`. *(his #198)*
28. **WHILE `FinalRoundNum < FinalRounds`** (real counter; original was `< 3`)
    *(his #199)* — increment each pass:
    a. `log: "CCU Generating Prompt for the Final Rounds"`. *(his #201)*
    b. **CCU final agenda** — LLM call, system = **P1**, user = **P7** → final agenda.
       *(his #202)*
    c. **Create Document + Write Text**. *(his #202–203)*
    d. `log`. *(his #204)*
    e. **FOR EACH agent in `AgentRoster`** *(flattens his #205–219)*:
       - LLM call — system = **P3**, user = **P6** (concluding round) → response.
       - **Create Document + Write Text**.
       - `log: "SubAgent {name} Thesis created and Saved Loop Final"`.
    f. Replace `RoundResponses`.
    g. **CCU Wraps up The Whole Dialect** — LLM call, system = **P1**, user = **P7**.
       *(his #220)*
    h. **Create Document + Write Text**. *(his #221)*
    i. `log: "CCU Wrapped Up the Past Round and Saved Doc"`. *(his #222)*
    j. **Write Row** — final record. *(his #223–255, columns collapsed)*
    k. `log: "Round Ended And Sheet Updated"`. *(his #256)*

## PHASE 6 — FINALIZE
29. `log: "The whole Dialectic Reasoning is Completed"`. *(his #257)*
30. **CCU Summary for The Log** — LLM call, system = **P1**, user = **P8** → final
    result summary; write to the log. *(his #258)*
31. `log: "Final Result summary Log"`. *(his #259)*

---

## Coverage check (so nothing's missing)
His 259 activities = **~56 functional steps** + 96 `Column A–AF` cells (3 rows × 32)
+ ~40 `====>` log markers + loop/body containers. This checklist reproduces **every
functional step in his order**, collapses the column cells into the Write Row they
belong to, and keeps the log markers as `log:` steps. The only structural change is
`SubAgent 1–5` → `for each agent` (dynamic N), as agreed.

## Storage note
His flow wrote a Google Doc per agent per round + a 32-column sheet row per round.
Because agent count is now dynamic, use: **one Doc/record per message** and a
**row-per-message** log (`RunID | round | agent | type | text | created_at`). The
per-round Docs and the run sheet are still fully reconstructable from this.
