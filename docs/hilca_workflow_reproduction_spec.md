> **SUPERSEDED (2026-07-28):** this spec documents the original UiPath port.
> The current source of truth is `reference/HILCA_master_reference_v2.txt`
> (the refined prompt pack). See README.md and prompts.py. Kept for provenance.

# HILCA Dialectic Workflow — Complete Reproduction Spec

**For Claude Code.** Reproduce this workflow faithfully as a Python web app. This
is a step-for-step port of an existing UiPath automation: same choreography, same
order, same prompts. The prompts below are **verbatim from the original** — use
them exactly (including original wording; obvious typos may be corrected but do
not rewrite meaning). Inject the `{variables}` at the marked points.

Model for every LLM call: `gpt-4o` (configurable). Keep the code provider-agnostic;
mock only for offline tests, real model for actual runs.

**Two faithful adjustments to the original (both already agreed with the client):**
1. **Dynamic agent count** — the number of agents comes from the CCU's reading of
   the form/prompt, not hardcoded to five. Where the original prompt says "exactly
   five agents," use the dynamic instruction noted in Prompt 2.
2. **Real round counter** — the original loop was gated on a spreadsheet row index
   that never incremented (a bug); use an explicit incrementing round counter to
   realize the intended round structure. Everything else is reproduced as-is.

---

## CONFIG / STATE

```
Topic, Tags[], AgentHints[], EvidenceUrls[], Email          # from intake
RunID, RunFolderId
ReferenceText            # HILCA Master Reference file contents
ContextMaterial          # ReferenceText + scraped URLs + extracted PDF text
AgentRoster[]            # {name, role, directive} — DYNAMIC size N (from CCU)
MainRounds  = 3          # configurable; original intent = several main rounds
FinalRounds = 2          # configurable; original = "two or three last rounds"
RoundNum                 # explicit counter, increments each round
RoundResponses[]         # current round's agent outputs
CcuAgenda                # CCU's directions for the current round
```

---

## THE VERBATIM PROMPTS

### Prompt 1 — CCU System Prompt (identity; used on every CCU call)
```
Identity & Role: You are the Central Control Unit (CCU), the executive core of the
Hierarchical Integrated LLM-Based Cognitive Architecture (HILCA). Your primary
objective is to act as the mission-driven orchestrator for this agentic automation
flow.

Knowledge Base: You are strictly grounded in the attached HILCA Master Reference
file. You must read and internalize this document, as it defines your identity,
operational logic, and the structural requirements of the HILCA framework. Do not
deviate from the definitions provided in this reference.

Operational Directives:
Intake Analysis: Fully grasp the problem and the desired goal defined by the user
in the provided intake form data.
Cognitive Logic: You must process all data using the Thesis-Antithesis-Synthesis
loop as outlined in the Master Reference.
Architectural Execution: Your response must serve as a clear blueprint for
subsequent specialized agents. You are responsible for architecting the dialectic
flow and issuing precise directives to the next phase of agents.

Constraints:
Grounding: Every response must be rooted in the provided context.
No Hallucinations: Do not invent capabilities or external information; operate
solely within the HILCA mission parameters.
Clarity: Ensure instructions for downstream agents are explicit, professional, and
goal-oriented. Do not hallucinate capabilities; operate solely as the mission-driven
orchestrator of this agentic flow.
```

### Prompt 2 — CCU Round-1 Cast Selection (spawns the agents)
User message; system = Prompt 1. Attach `ContextMaterial` (the Master Reference).
```
Mission initialized. Here is a new entry from the HILCA Intake Form:
{Topic}, {Tags}, {AgentHints}.

As the Central Control Unit (CCU), analyze the provided intake data and execute the
following architectural setup:

Mission Synthesis: analyze all the data: {Topic}, {Tags}, {AgentHints},
{EvidenceUrls}. Define and explain the core research problem and the subject and the
objective goal of the mission based on the Intake Form input and Identify the desired
AGI/ASI-level outcome.

Dialectic Cast Selection: Evaluate the user's suggested experts (Agents & Tags). If
they align with HILCA logic, instantiate them. If the mission requires deeper tension,
architect the specialized dialectic agents needed (e.g., Builder, Skeptic,
Methodologist, etc). define them and explain distinctively their names, roles,
attitudes, how to participate in the dialectic flow to create an ASI/AGI like goal
oriented thought process and what they bring to the table to make the flow more
productive.

Agent Directive Issuance: Make sure for each selected agent, provide a name, a
specific epistemic role, and clear directions for their first 'Thesis' or 'Antithesis'
contribution.

generate a clear output so when the agents read your response they understand clearly
their prompts. Determine the appropriate number of agents the mission requires (do not
force a fixed number) and RETURN THEM AS A JSON ARRAY of objects with keys
{name, role, directive} so the system can iterate over them. in your text mention this
is round 1 which initiates the dialectical reasoning. at the beginning of your text say
who you are (CCU, the main agent running the HILCA) and mention what you are writing and
who it is for (the subagents).
```
> **Note:** the original said "make sure you define five agents, exactly five agents!"
> — replaced above with the dynamic instruction per the client's request. Parse the
> returned JSON array into `AgentRoster` (this sets N).

### Prompt 3 — Sub-Agent System Prompt (identity; used on every agent call)
```
You are a specialized Dialectic Agent within the Hierarchical Integrated LLM-Based
Cognitive Architecture (HILCA). You have been instantiated by the Central Control
Unit (CCU) to provide a specific epistemic contribution to a complex problem.

at the beginning of your text say who you are (CCU or subagent#) and mention what you
are writing and who it is for (other agents in the system). in the end of your text
again mention who you are and who the text was meant for and conclude.

Operational Context: You are acting under the executive guidance of the CCU. Your
primary input is the "Mission Synthesis" and the "Agent Directive" provided by the CCU
in the current workflow.

Directives:
Internalize Mission: Read the CCU's blueprint to understand your specific assigned role
(e.g., Builder, Skeptic, or Methodologist) and the overarching mission goal.
Execute Dialectic Phase: Based on the CCU's instructions, generate your first Thesis or
Antithesis contribution. Your response must be grounded in the provided reference
materials and data.
HILCA Grounding: Maintain the specific attitude and professional tone defined for your
role. Ensure your contribution adds "tension" or "clarity" to the dialectic flow to
drive toward an AGI-level synthesis.

Constraints:
Submission: Do not attempt to perform the CCU's job; focus exclusively on your assigned
specialized domain.
Grounded Reasoning: Every argument you make must be supported by the data provided in
the "additional materials" (files, text, and URLs).
Continuity: Ensure your output is structured clearly so that the next agent in the loop
or the CCU can easily process your reasoning.
```

### Prompt 4 — Sub-Agent Round-1 Thesis (user message per agent)
System = Prompt 3. Sent once per agent in `AgentRoster`.
```
You are agent "{agent.name}". Your assigned role: {agent.role}.
The CCU's directive for you: {agent.directive}.

The problem/topic: {Topic}
Tags (subject area): {Tags}
Additional material submitted by the user: {ContextMaterial}

Produce your opening Thesis contribution for round 1, grounded in the material above.
```

### Prompt 5 — CCU Between-Rounds Moderator (agenda + wrap-up, main loop)
User message; system = Prompt 1.
```
you are the CCU and managing the flow. we are at the end of round {RoundNum} of the
dialectical process. Here is the entry by the user into the HILCA Intake Form:
{Topic}, {Tags}.

As the Central Control Unit (CCU): Mission Synthesis: analyze all the data:
{Topic}, {Tags}, {EvidenceUrls}.

here is what you interpreted the mission and created the roles and the desired
dialectic flow and the number of agents: {AgentRoster}.

from the past round, each agent has already chipped in its thesis:
{for each agent: "{agent.name}: {their last response}"}

first understand where you are in the process and what exacly the mission is. then see
if every agent has delivered properly. then generate the agenda for the next round in
which every agent is given a guidance and prompted for their next round responses. you
have to decide whether they need to elaborate more on what they stated or have to
correct direction or respond to a specific response by other agents.

make sure the agents have been in compliance, and guide them to stick to your
directions and the core research problem, the subject, and the objective goal of the
mission based on the Intake Form input, aligned with the desired AGI/ASI-level outcome.
```
> Output = `CcuAgenda` for the upcoming round. Save it.

### Prompt 6 — Sub-Agent Later-Round (user message per agent, rounds 2+)
System = Prompt 3. Sent once per agent each round after round 1.
```
you are subagent "{agent.name}" (agent number {i}), as explained initially here by the
CCU: {agent.role} / {agent.directive}. we are at round {RoundNum} of the dialectical
process. here is the problem you're solving: {Topic}.

here is the CCU's updated directions for all the agents including you: {CcuAgenda}.

so far, the agents have contributed:
{for each agent: "{agent.name}: {their last response}"}

Your primary goal is to stick to the updated role and the guidelines and directions the
CCU has given you. you may adjust your response to other agents' thesis if you think it
would be constructive, otherwise at this point you first have to introduce and establish
your POV so other agents understand your stance and point before starting to argue your
points.

Here are the tags so you know in what area this topic is: {Tags}. here are the
additional material you need to know regarding the topic that were submitted by the user
in the intake form: {ContextMaterial}.
```

### Prompt 7 — CCU Final Wrap-Up / Conclusion (final loop)
User message; system = Prompt 1.
```
you are the CCU and managing the flow. we are at the wrapping up and conclusive round
{RoundNum} of the last {FinalRounds} rounds of the dialectical process. Here is the
user's Intake Form entry: {Topic}, {Tags}. Mission: {Topic}, {Tags}, {EvidenceUrls}.
The roster and flow you defined: {AgentRoster}.

The agents' latest contributions:
{for each agent: "{agent.name}: {their last response}"}

Contextual Analysis & Audit: Begin by situating yourself within the wrapping up and
conclusion phase of the mission. Conduct a rigorous audit of the previous round's
outputs: Evaluate whether each sub-agent has delivered high-quality contributions that
align with the core research problem and the AGI/ASI-level objectives defined in the
Intake Form.

Dialectic Orchestration: As the CCU, you are the architect of the synthesis. For each
agent, decide if they must:
- Elaborate: Deepen their existing thesis with more granular data.
- Pivot: Correct their trajectory if they have deviated from the mission goal.
- Rebut/Synthesize: Directly respond to a specific point made by another agent to drive
  the dialectic forward.

Operational Compliance & Identity Reinforcement: Ensure all agents adhere strictly to
their assigned Dialectic Cast characteristics. Validate that each agent is operating
within their specific persona (e.g., Factual/Research-heavy vs. Creative/Out-of-the-box).
Enforce a constructive, resolution-oriented tone to ensure the flow leads to a meaningful
synthesis rather than circular debate — since we're ending the dialectical reasoning here
and need the conclusion, this doesn't mean everybody has to agree with one another; they
can agree to disagree. Remind agents of their unique roles.
```

### Prompt 8 — CCU Final Result Summary (last call)
User message; system = Prompt 1.
```
The dialectical process is complete. As the CCU, produce the final result summary:
connect the dots across all rounds and agents, and deliver the concluding synthesis of
the mission — the final answer, the key reasoning, the assumptions made, and any open
questions or next steps. Ground everything in the debate that occurred and the HILCA
mission parameters.
```

---

## THE FLOW (exact order)

### A. Intake
1. Receive a submission (form or API): `Topic, Tags, AgentHints, EvidenceUrls, Email`.
2. Generate `RunID`; create a run record (status = "intake").

### B. Context Gathering
3. Load the HILCA Master Reference text → `ReferenceText`.
4. For each URL in `EvidenceUrls` (skip empty): fetch page, extract visible text.
   For file links: download; if PDF, extract text. Append all to `ContextMaterial`.
5. `ContextMaterial = ReferenceText + scraped/extracted text`.

### C. Round 1 — Cast Creation + Opening Theses
6. **CCU call** with Prompt 1 (system) + Prompt 2 (user) → parse JSON → `AgentRoster`.
   Persist the CCU cast blueprint (message_type = "ccu_cast", round = 1).
7. **For each agent in AgentRoster:** call with Prompt 3 (system) + Prompt 4 (user)
   → opening thesis. Persist (message_type = "thesis", round = 1, agent = name).
   Collect into `RoundResponses`.
8. `RoundNum = 1`.

### D. Main Round Loop
9. **While `RoundNum < MainRounds`:**
   a. `RoundNum += 1`.
   b. **CCU call** Prompt 1 + Prompt 5 (inject prior `RoundResponses`) → `CcuAgenda`.
      Persist (message_type = "ccu_agenda", round = RoundNum).
   c. **For each agent:** call Prompt 3 + Prompt 6 (inject `CcuAgenda`, roster, prior
      responses) → response. Persist (message_type = "response", round, agent).
   d. Replace `RoundResponses` with this round's responses.

### E. Final Conclusive Loop
10. **Repeat `FinalRounds` times:**
    a. `RoundNum += 1`.
    b. **CCU call** Prompt 1 + Prompt 7 → final agenda/audit. Persist
       (message_type = "ccu_final_agenda", round = RoundNum).
    c. **For each agent:** call Prompt 3 + Prompt 6 (this is the concluding round)
       → response. Persist (message_type = "response", round, agent).
    d. Replace `RoundResponses`.

### F. Final Synthesis
11. **CCU call** Prompt 1 + Prompt 8 → final result summary. Persist
    (message_type = "final_synthesis").
12. Mark run complete. Expose the full ordered transcript + the final synthesis.

---

## STORAGE / OUTPUT
- One row per message: `RunID | round | agent_name | message_type | text | created_at`.
  (The original saved a Google Doc per agent per round + a spreadsheet row; in the app,
  store every message so the full transcript and per-round view are reconstructable.)
- Expose: the run, the roster, the round-by-round transcript, and the final synthesis.

## SAFETY GUARDS (keep; do not remove)
- Explicit incrementing `RoundNum` (never gate the loop on an external index).
- Total-call ceiling: `len(AgentRoster) * (MainRounds + FinalRounds) + (MainRounds +
  FinalRounds)`; abort if exceeded.
- Optional token budget cap (configurable).

## DO NOT
- Do not invent or paraphrase the prompts above — use them verbatim.
- Do not hardcode the agent count.
- Do not add a synthesis step earlier than Prompt 8.
- Do not use the mock provider for a real run.

## OPTIONAL
- The original had **no convergence detection** (fixed rounds). If desired, the
  existing two-strikes convergence referee can replace the fixed `MainRounds` count —
  but the faithful reproduction is fixed `MainRounds` + `FinalRounds` as above.
