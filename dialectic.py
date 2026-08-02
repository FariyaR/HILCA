"""The HILCA dialectic engine — implementing the master file v2 spec
(`reference/HILCA_master_reference_v2.txt`): the refined prompt pack (the
Behavioral Protocol, role cards, two-part CCU wrap-ups, the sequential
handoff chain) plus the engineering layer from the file's developer-extraction
batches (convergence voting, stance stabilization, Devil's Advocate
validation, gap-analysis loop-back with targeted retrieval, sliding
summarization, tiered model routing, round-boundary checkpoints).

Choreography per run:
  PHASE 1  intake row -> run record (done by the caller / web layer)
  PHASE 2  master reference + evidence URLs -> ContextMaterial (+ BM25 index)
  PHASE 3  round 1: CCU cast selection (P2, EXACTLY 5 role cards) ->
           sequential cascade of opening theses (P4: agent i sees theses
           1..i-1) -> CCU two-part wrap-up (P5: SECTION 1 audit + SECTION 2
           round-2 directives) -> round summary -> write rows -> checkpoint
  PHASE 4  main loop (rounds 2..MainRounds): agents respond to the previous
           wrap-up's SECTION 2 directives (P6, with STANCE + CONVERGENCE
           footers) -> CCU two-part wrap-up (P5) -> round summary -> rows ->
           checkpoint; the loop exits early when the average convergence vote
           clears HILCA_CONVERGENCE_THRESHOLD or stances stabilize
  PHASE 5  final loop (FinalRounds): CCU final directive (P7) -> final
           closing statements (P6F) -> rows -> checkpoint
  PHASE 6  Devil's Advocate validation -> Gap Analysis (+ targeted retrieval
           loop-back) -> CCU Final Synthesis & Executive Audit Report (P8) ->
           agent verdicts (P9) -> the single-document deliverable (P10,
           format-checked) -> the 800-char Executive Summarizer log entry
           (P11) -> download / optional email -> complete

Every message is a DB row, a markdown file under runs/<run_id>/, and a row in
that run's run_log.csv. STANCE/CONVERGENCE lines land in the debate_state
table (the master file's granular state schema); a JSON snapshot lands in the
checkpoints table at every round boundary so a crashed run resumes instead of
restarting.

Safety guards: explicit incrementing RoundNum, a total-LLM-call ceiling
derived from the round counts, and a configurable cumulative token budget.
"""
from __future__ import annotations

import csv
import difflib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import controls
import prompts
from context_gathering import gather_context
from db import Store
from llm import LLMClient
from retrieval import HybridIndex
from schemas import RoleCard

AGENT_COUNT = prompts.AGENT_COUNT          # master file v2: exactly five
MAX_AGENTS = AGENT_COUNT                   # kept for the web layer's roster validation
MAIN_ROUNDS = int(os.getenv("HILCA_MAIN_ROUNDS", "3"))     # rounds 1..MAIN_ROUNDS (round 1 included)
FINAL_ROUNDS = int(os.getenv("HILCA_FINAL_ROUNDS", "3"))   # the final conclusion loop (original ran 3)
TOKEN_BUDGET = int(os.getenv("HILCA_TOKEN_BUDGET", "2000000"))
CCU_MAX_TOKENS = int(os.getenv("HILCA_CCU_MAX_TOKENS", "8000"))
AGENT_MAX_TOKENS = int(os.getenv("HILCA_AGENT_MAX_TOKENS", "2500"))
COMPACT_MAX_TOKENS = int(os.getenv("HILCA_COMPACT_MAX_TOKENS", "700"))
SUMMARY_MAX_TOKENS = int(os.getenv("HILCA_SUMMARY_MAX_TOKENS", "600"))
DOCS_ROOT = os.getenv("HILCA_DOCS_ROOT", "runs")
THREADED = os.getenv("HILCA_THREADED", "1") == "1"
THREAD_KEEP = int(os.getenv("HILCA_THREAD_KEEP", "8"))  # last N exchanges kept per thread

# Convergence machinery (master file batch 2: Critique-Refine-Vote) and the
# stateless-mode targeted retrieval switch are read from env at call time so
# an operator (or a test) can adjust them without re-importing the engine.
def _convergence_exit() -> bool:
    return os.getenv("HILCA_CONVERGENCE_EXIT", "1") == "1"


def _convergence_threshold() -> float:
    return float(os.getenv("HILCA_CONVERGENCE_THRESHOLD", "0.85"))


def _stance_similarity() -> float:
    return float(os.getenv("HILCA_STANCE_SIMILARITY", "0.90"))


def _stance_stable_rounds() -> int:
    return int(os.getenv("HILCA_STANCE_STABLE_ROUNDS", "2"))


def _targeted_retrieval() -> bool:
    return os.getenv("HILCA_TARGETED_RETRIEVAL", "1") == "1"


RUNNING_SUMMARY_CAP = int(os.getenv("HILCA_RUNNING_SUMMARY_CAP", "8000"))

CSV_HEADER = ["RunID", "round", "agent_name", "message_type", "text", "created_at"]

VERDICT_RE = re.compile(r"VERDICT\s*:\s*(AGREE|DISAGREE)", re.IGNORECASE)
STANCE_RE = re.compile(r"^\s*STANCE\s*:\s*(.+?)\s*$", re.MULTILINE)
CONV_RE = re.compile(r"^\s*CONVERGENCE\s*:\s*([01](?:\.\d+)?)\s*$", re.MULTILINE)
GAP_RE = re.compile(r"^\s*GAP\s*:\s*(.+?)\s*$", re.MULTILINE)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "agent"


def parse_verdict(text: str) -> str:
    """P9 asks each agent to open with 'VERDICT: AGREE' or 'VERDICT: DISAGREE'."""
    m = VERDICT_RE.search(text)
    return m.group(1).lower() if m else "unclear"


def parse_stance(text: str) -> Optional[str]:
    m = STANCE_RE.search(text)
    return m.group(1) if m else None


def parse_convergence(text: str) -> Optional[float]:
    m = CONV_RE.search(text)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return v if 0.0 <= v <= 1.0 else None


def split_wrapup_sections(text: str) -> Tuple[str, str]:
    """Split a two-part CCU wrap-up into (SECTION 1 audit, SECTION 2 directives).

    Falls back to ('', whole text) when the model skipped the headers, so the
    next round always has *some* directives to work from."""
    m = re.search(r"\**\s*SECTION\s*2\s*[:\-]", text, re.IGNORECASE)
    if m:
        return text[:m.start()].strip(), text[m.start():].strip()
    return "", text.strip()


def _strip_doc_fences(text: str) -> str:
    """A model sometimes wraps the whole deliverable in one ``` fence; unwrap it."""
    t = text.strip()
    m = re.fullmatch(r"```(?:markdown|md)?\s*(.*?)\s*```", t, re.DOTALL)
    return m.group(1).strip() if m else t


TRANSCRIPT_TYPE_LABELS = {
    "ccu_cast": "cast selection & mission blueprint",
    "ccu_cast_raw": "cast selection (raw, format retry)",
    "thesis": "opening thesis",
    "response": "contribution",
    "ccu_directives": "round directives",
    "ccu_wrapup": "round wrap-up",
    "ccu_final_agenda": "final-round agenda",
    "round_summary": "round summary (engine)",
    "da_validation": "Devil's Advocate validation",
    "gap_analysis": "gap analysis",
    "final_synthesis": "Final Synthesis & Executive Audit Report",
    "final_verdict": "final verdict & final say",
    "human_feedback": "human operator feedback",
    "context_compacted": "context compaction (engine)",
    "log_summary": "executive log summary",
}

# The deliverable itself is not part of the debate record it summarizes.
TRANSCRIPT_SKIP_TYPES = frozenset({"deliverable"})


def render_transcript(run_id: str, topic: str, messages: List[dict],
                      skip: frozenset = TRANSCRIPT_SKIP_TYPES) -> str:
    """The complete dialectic transcript as one readable markdown document.

    Two consumers: the run folder's transcript.md (offered to the user for
    deeper analysis alongside the single-document deliverable) and the P10
    prompt (so the report is grounded in what was actually said)."""
    parts = [
        "# HILCA Dialectic — Complete Transcript", "",
        f"- Run: `{run_id}`",
        f"- Topic: {topic}", "",
    ]
    for m in messages:
        mtype = m.get("message_type", "")
        if mtype in skip:
            continue
        label = TRANSCRIPT_TYPE_LABELS.get(mtype, mtype or "message")
        parts += [f"### Round {m['round_num']} · {m['agent_name']} — {label}", "",
                  str(m["message"]).strip(), ""]
    return "\n".join(parts)


class RosterCountError(ValueError):
    """The CCU returned a parsable roster of the wrong size (must be exactly 5)."""

    def __init__(self, found: int):
        super().__init__(
            f"CCU returned {found} role cards; the master file mandates exactly {AGENT_COUNT}."
        )
        self.found = found


def extract_roster(ccu_text: str) -> List[Dict[str, str]]:
    """Parse the JSON array of role cards out of the CCU's cast text.

    Cards are validated through schemas.RoleCard ({name, role, directive}
    required; {persona, rubric} defaulted when omitted). Fenced ```json blocks
    are authoritative when present; otherwise every JSON array in the raw text
    is decoded with a real JSON decoder and the last valid one wins. Exactly
    AGENT_COUNT cards are required (RosterCountError otherwise, so the engine
    can retry once with a correction)."""
    fenced: List[List[dict]] = []
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", ccu_text, re.DOTALL):
        try:
            _collect_roster(json.loads(m.group(1).strip()), fenced)
        except (json.JSONDecodeError, ValueError):
            continue

    candidates = fenced
    if not candidates:
        raw: List[List[dict]] = []
        decoder = json.JSONDecoder()
        i = ccu_text.find("[")
        while i != -1:
            try:
                data, end = decoder.raw_decode(ccu_text, i)
            except json.JSONDecodeError:
                i = ccu_text.find("[", i + 1)
                continue
            _collect_roster(data, raw)
            i = ccu_text.find("[", end)
        candidates = raw

    if not candidates:
        # Truncation salvage: a reply cut off at the output-token cap never
        # closes the array bracket, so no complete array parses above. Recover
        # every complete role-card object individually; the exactly-5 gate
        # below still applies, so a partial recovery becomes a count retry
        # instead of a hard failure.
        objs: List[dict] = []
        decoder = json.JSONDecoder()
        i = ccu_text.find("{")
        while i != -1:
            try:
                data, end = decoder.raw_decode(ccu_text, i)
            except json.JSONDecodeError:
                i = ccu_text.find("{", i + 1)
                continue
            if isinstance(data, dict) and {"name", "role", "directive"} <= set(data):
                objs.append(data)
                i = ccu_text.find("{", end)
            else:
                # Not a role card; rescan inside it in case cards are nested.
                i = ccu_text.find("{", i + 1)
        if objs:
            try:
                _collect_roster(objs, candidates)
            except (ValueError, TypeError):
                pass

    if not candidates:
        raise ValueError("CCU cast response contained no parsable role-card JSON array.")
    roster = candidates[-1]
    if len(roster) != AGENT_COUNT:
        raise RosterCountError(len(roster))
    # Names key the per-agent response tracking; duplicates would silently
    # collapse two agents into one in every later prompt — uniquify them.
    used: set[str] = set()
    for a in roster:
        name, k = a["name"], 2
        while name in used:
            name = f"{a['name']} ({k})"
            k += 1
        a["name"] = name
        used.add(name)
    return roster


def _collect_roster(data, out: List[List[dict]]) -> None:
    if (
        isinstance(data, list)
        and data
        and all(isinstance(a, dict) and {"name", "role", "directive"} <= set(a) for a in data)
    ):
        cards = [RoleCard(**{k: str(a[k]) for k in ("name", "role", "directive", "persona", "rubric") if k in a})
                 for a in data]
        out.append([c.model_dump() for c in cards])


class DialecticFlow:
    """Runs one intake row through the full master-file-v2 dialectic."""

    def __init__(
        self,
        store: Store,
        llm: Optional[LLMClient] = None,
        main_rounds: int = MAIN_ROUNDS,
        final_rounds: int = FINAL_ROUNDS,
        token_budget: int = TOKEN_BUDGET,
        emit: Optional[Callable[[str, dict], None]] = None,
        docs_root: str = DOCS_ROOT,
        reference_path: Optional[str] = None,
        require_approval: bool = False,
        threaded: Optional[bool] = None,
        context_window: Optional[int] = None,
        compact_at: Optional[float] = None,
    ):
        self.store = store
        self.llm = llm or LLMClient()
        self.threaded = THREADED if threaded is None else threaded
        self.thread_keep = max(1, THREAD_KEEP)
        # Context compaction (Claude-style /compact): the working context
        # budget the usage ring measures against, and the fraction of it at
        # which the engine compacts automatically (0 disables auto-compaction).
        self.context_window = (
            int(os.getenv("HILCA_CONTEXT_WINDOW", "30000")) if context_window is None
            else context_window
        )
        self.compact_at = (
            float(os.getenv("HILCA_COMPACT_AT", "0.70")) if compact_at is None
            else compact_at
        )
        # Round 1 always runs, so fewer than 1 main round is impossible; a
        # smaller configured value would undercount the call ceiling and
        # false-trip the guard mid-run.
        self.main_rounds = max(1, main_rounds)
        self.final_rounds = max(0, final_rounds)
        self.token_budget = token_budget
        self.emit = emit or (lambda event, data: None)
        self.docs_root = docs_root
        self.reference_path = reference_path
        # When set (the web flow), the run holds after the CCU casts the agents
        # until the operator approves — and possibly edits — the five cards.
        self.require_approval = require_approval

        self._run_id: str = ""
        self._log_seq = 0
        self._llm_calls = 0
        self._call_ceiling: Optional[int] = None
        self._tokens_used = 0
        self._folder: Optional[Path] = None
        self._csv_path: Optional[Path] = None
        self._pending_rows: List[List[str]] = []
        self._last_responses: Dict[str, str] = {}   # agent name -> latest contribution
        self._fresh_round: Dict[str, int] = {}      # agent name -> round of that contribution
        self._stances: Dict[str, Dict[int, str]] = {}
        self._votes: Dict[int, Dict[str, float]] = {}
        self._threads: Dict[str, List[dict]] = {}
        self._running_summary: List[str] = []       # one entry per summarized round
        self._index: Optional[HybridIndex] = None
        self._context_material: str = ""
        self._stable_streak = 0
        self._round = 1                              # display round for maintenance rows
        self._last_prompt_chars = 0                  # stateless-mode context estimate
        self._calls_since_compact = 10**9            # auto-compact anti-thrash guard

    # ------------------------------------------------------------------ run --
    def run(self, run_id: str, resume: bool = False) -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"No run found with id {run_id}")

        self._run_id = run_id
        topic: str = run["topic"]
        tags = ", ".join(run["tags"]) if run["tags"] else "(none)"
        agent_hints = ", ".join(run["agent_hints"]) if run["agent_hints"] else "(none)"
        evidence_urls = ", ".join(run["evidence_urls"]) if run["evidence_urls"] else "(none)"

        checkpoint = self.store.get_checkpoint(run_id) if resume else None
        if resume and checkpoint is None:
            raise ValueError(f"Run {run_id} has no checkpoint to resume from.")

        try:
            if checkpoint:
                roster, round_num, final_round_num, phase, ccu_cast_text, directives = \
                    self._restore(checkpoint)
                self._round = round_num
                self._index = self._build_index()
                self.store.set_status(run_id, "dialectic")
                self.emit("resumed_from_checkpoint", {"phase": phase, "round": round_num})
                self._log(f"Run resumed from checkpoint (phase {phase}, round {round_num})")
            else:
                # ---------------- PHASE 2 — CONTEXT GATHERING ----------------
                self.store.set_status(run_id, "gathering_context")
                self._context_material = gather_context(
                    run["evidence_urls"], reference_path=self.reference_path, log=self._log
                )
                self._index = self._build_index()
                self.emit("context_ready", {"chars": len(self._context_material)})
                self._emit_context_usage()

                # ---------------- PHASE 3 — ROUND 1 --------------------------
                round_num, final_round_num, phase = 1, 0, "main"
                roster, ccu_cast_text, directives = self._phase_round1(
                    run, topic, tags, agent_hints, evidence_urls
                )
                self._checkpoint("main", round_num, final_round_num, roster, ccu_cast_text, directives)

            # ---------------- PHASE 4 — MAIN ROUND LOOP -------------------
            if phase == "main":
                self._log("Initiating Round Loop")
                while round_num < self.main_rounds:
                    round_num += 1
                    self._round = round_num
                    self.emit("round_started", {"round": round_num, "phase": "main"})
                    directives = self._run_main_round(
                        round_num, roster, topic, tags, evidence_urls, ccu_cast_text, directives
                    )
                    self._checkpoint("main", round_num, final_round_num, roster, ccu_cast_text, directives)
                    if self._converged(round_num, roster):
                        break
                phase = "final"

            # ---------------- PHASE 5 — FINAL CONCLUSIVE LOOP -------------
            if phase == "final":
                self._log("Loop Round Ended and Final Round Begins")
                while final_round_num < self.final_rounds:
                    final_round_num += 1
                    round_num += 1
                    self._round = round_num
                    self.emit("round_started", {"round": round_num, "phase": "final"})
                    directives = self._run_final_round(
                        round_num, roster, topic, tags, agent_hints, evidence_urls, ccu_cast_text
                    )
                    self._checkpoint("final", round_num, final_round_num, roster, ccu_cast_text, directives)
                phase = "closing"

            # ---------------- PHASE 6 — CLOSING ---------------------------
            self._checkpoint("closing", round_num, final_round_num, roster, ccu_cast_text, directives)
            self._log("The whole Dialectic Reasoning is Completed")

            # Devil's Advocate validation (batch 1: adversarial self-correction)
            da_findings = self._devils_advocate(round_num, topic, roster)
            # Gap-Analysis reflection + targeted retrieval loop-back (batch 3)
            gap_material = self._gap_analysis(round_num, topic, roster, ccu_cast_text)

            # CCU Final Synthesis & Executive Audit Report (P8, high tier)
            final_wrapup = self._ccu_call(
                self._render_p8(topic, tags, agent_hints, evidence_urls, ccu_cast_text, roster)
                + da_findings + gap_material + self._feedback_block("CCU", round_num),
                system=prompts.P1_CCU_FINAL, tier="high",
            )
            self._persist(round_num, "CCU", "final_synthesis", final_wrapup, doc="final_synthesis.md")
            self.emit("synthesis", {"text": final_wrapup, "tokens": self._tokens_used})
            self._log("Final Result summary Log")

            # -------- Agent verdicts on the CCU's synthesis (P9) --------------
            self._log("Collecting each agent's verdict and final say on the CCU synthesis")
            verdicts: List[Dict[str, str]] = []
            for i, agent in enumerate(roster, start=1):
                vprompt = prompts.render(prompts.P9_AGENT_FINAL_VERDICT, {
                    "{agent.name}": agent["name"],
                    "{i}": str(i),
                    "{agent.role}": agent["role"],
                    "{agent.directive}": agent["directive"],
                    "{Topic}": topic,
                    "{CcuFinalWrapup}": final_wrapup,
                    prompts.DIALECTIC_INPUT_MARKER: self._render_inputs(roster, round_num),
                }) + self._feedback_block(agent["name"], round_num)
                reply = self._agent_call(i, agent, vprompt)
                verdict = parse_verdict(reply)
                verdicts.append({"agent": agent["name"], "verdict": verdict, "text": reply})
                self._persist(round_num, agent["name"], "final_verdict", reply,
                              doc=f"final_verdict_agent{i}_{_safe_name(agent['name'])}.md", index=i)
                self.emit("agent_verdict", {"round": round_num, "agent": agent["name"],
                                            "index": i, "verdict": verdict})
                self._log(f"SubAgent {agent['name']} delivered its verdict: {verdict.upper()}")

            # -------- The single-document deliverable (P10) -------------------
            deliverable_text, format_ok = self._compose_deliverable(topic, roster, verdicts, final_wrapup)
            transcript_path = self._write_transcript(topic)
            deliverable_text += self._deliverable_footer(run_id, round_num, len(roster))
            self._persist(round_num, "CCU", "deliverable", deliverable_text, doc="deliverable.md")
            emailed = self._email_deliverable(run, topic, deliverable_text)
            self.emit("deliverable", {
                "format_ok": format_ok,
                "emailed": emailed,
                "file": str(self._folder / "deliverable.md") if self._folder else None,
                "transcript_file": transcript_path,
            })

            # -------- The 800-character Executive Summarizer log entry (P11) --
            log_summary = self._model_call(
                "SUMMARIZER", prompts.P11_SUMMARIZER_SYSTEM,
                prompts.render(prompts.P11_LOG_SUMMARY, {"{FinalDocument}": final_wrapup}),
                SUMMARY_MAX_TOKENS, tier="low", thread=False,
            )
            self._persist(round_num, "CCU", "log_summary", log_summary, doc="log_summary.md")
            self._log("Executive 800-character log summary recorded")
            self.emit("log_summary", {"text": log_summary})

            self._flush_rows()
            self.store.clear_checkpoint(run_id)
            self.store.set_status(run_id, "complete")
        except Exception:
            self.store.set_status(run_id, "error")
            raise

        return {
            "run_id": run_id,
            "agents": len(roster),
            "rounds_completed": round_num,
            "main_rounds": self.main_rounds,
            "final_rounds": self.final_rounds,
            "llm_calls": self._llm_calls,
            "call_ceiling": self._call_ceiling,
            "tokens_used": self._tokens_used,
            "token_budget": self.token_budget,
            "synthesis": final_wrapup,
            "verdicts": [{"agent": v["agent"], "verdict": v["verdict"]} for v in verdicts],
            "deliverable_path": str(self._folder / "deliverable.md") if self._folder else None,
            "transcript_path": transcript_path,
            "deliverable_emailed": emailed,
            "log_summary": log_summary,
            "threaded": self.threaded,
            "docs_folder": str(self._folder),
        }

    def _build_index(self) -> HybridIndex:
        """Build the hybrid retrieval index over the ContextMaterial and log
        which vector backend the run got (embeddings degrade to BM25-only
        rather than ever failing the run)."""
        index = HybridIndex(self._context_material, log=self._log)
        self._log(
            f"Hybrid retrieval index ready — {len(index.chunks)} chunk(s); "
            f"vector half: {index.vector_backend or 'off (BM25 only)'}"
        )
        return index

    # ------------------------------------------------------------ phases --
    def _phase_round1(self, run: dict, topic: str, tags: str, agent_hints: str,
                      evidence_urls: str):
        """Round 1: cast selection -> approval -> cascade -> two-part wrap-up."""
        run_id = self._run_id
        self.emit("round_started", {"round": 1, "phase": "main"})

        cast_prompt = prompts.render(prompts.P2_CCU_CAST_SELECTION, {
            "{Topic}": topic,
            "{Tags}": tags,
            "{AgentHints}": agent_hints,
            "{EvidenceUrls}": evidence_urls,
            "{ContextMaterial}": self._context_material,
        })
        self._folder = Path(self.docs_root) / run_id
        self._folder.mkdir(parents=True, exist_ok=True)

        ccu_cast_text = self._ccu_call(cast_prompt, system=prompts.P1_CCU_SYSTEM)
        try:
            roster = extract_roster(ccu_cast_text)
        except ValueError as exc:
            # One correction retry, for both failure modes: RosterCountError
            # (parsable but not exactly five cards) and a bare ValueError (no
            # parsable array at all — usually a reply truncated at the output
            # cap). The retry asks for the array ALONE so the corrected output
            # is small and cannot truncate again.
            self._log(f"Cast retry: {exc}")
            if isinstance(exc, RosterCountError):
                fault = f"contained {exc.found} role cards instead of exactly {AGENT_COUNT}"
            else:
                fault = "contained no parsable role-card JSON array"
            correction = (
                f"FORMAT CORRECTION — your previous Foundation Briefing {fault}. "
                f"Return ONLY the corrected JSON array of EXACTLY {AGENT_COUNT} role-card "
                "objects with keys {name, role, persona, directive, rubric}, inside a "
                "```json code fence. No briefing prose — the array alone."
            )
            if not self.threaded:
                # Stateless mode has no conversation memory: replay the faulty
                # response so the CCU knows what it is correcting.
                correction += "\n\n--- YOUR PREVIOUS RESPONSE ---\n" + ccu_cast_text
            fix_text = self._ccu_call(correction, system=prompts.P1_CCU_SYSTEM)
            try:
                roster = extract_roster(fix_text)
            except ValueError as exc2:
                # Keep the evidence: without this the raw replies are lost and
                # the failure cannot be diagnosed afterwards.
                self._persist(
                    1, "CCU", "ccu_cast_raw",
                    ccu_cast_text + "\n\n--- FORMAT-CORRECTION RETRY RESPONSE ---\n" + fix_text,
                    doc="round1_ccu_cast_raw.md",
                )
                raise RuntimeError(
                    f"CCU cast failed even after a format-correction retry ({exc2}). "
                    "The raw responses were saved to round1_ccu_cast_raw.md in the run "
                    "folder and to the transcript. Starting a new run usually succeeds; "
                    "if it keeps failing, raise HILCA_CCU_MAX_TOKENS in .env."
                ) from exc2
            ccu_cast_text += "\n\n### CORRECTED ROLE-CARD ARRAY (format retry)\n" + fix_text
        self.store.save_roster(run_id, roster)
        self._call_ceiling = self._ceiling(len(roster))

        for idx, agent in enumerate(roster):
            self.emit("agent_spawned", {
                "index": idx + 1, "name": agent["name"],
                "role": agent["role"], "directive": agent["directive"],
            })
        self._persist(1, "CCU", "ccu_cast", ccu_cast_text, doc="round1_ccu_cast.md")

        # Approval gate: hold here until the operator approves the cast. The
        # operator may edit the five cards (names/roles/personas/directives)
        # via POST /api/runs/{id}/approve before the run proceeds.
        if self.require_approval:
            self.store.set_status(run_id, "awaiting_approval")
            self.emit("awaiting_approval", {"roster": roster})
            controls.for_run(run_id).wait_for_approval()
            approved = self.store.get_roster(run_id)
            if approved:
                roster = approved
            run_after = self.store.get_run(run_id) or {}
            if run_after.get("main_rounds"):
                self.main_rounds = max(1, int(run_after["main_rounds"]))
            if run_after.get("final_rounds") is not None:
                self.final_rounds = max(0, int(run_after["final_rounds"]))
            self._call_ceiling = self._ceiling(len(roster))
            self.store.add_message(run_id, 1, "OPERATOR", json.dumps(roster), "cast_approved")
            self._pending_rows.append([
                self._run_id, "1", "OPERATOR", "cast_approved", json.dumps(roster),
                datetime.now(timezone.utc).isoformat(),
            ])
            self.emit("roster_approved", {"roster": roster})

        self.store.set_status(run_id, "dialectic")
        self._log("CCU created the roles and were saved in doc file")

        # Sequential cascade: agent i reads the theses of agents 1..i-1.
        prior: List[Tuple[int, str, str]] = []
        for i, agent in enumerate(roster, start=1):
            thesis_prompt = prompts.render(prompts.P4_AGENT_ROUND1_THESIS, {
                "{i}": str(i),
                "{agent.name}": agent["name"],
                "{agent.role}": agent["role"],
                "{agent.persona}": agent.get("persona") or "(as briefed by the CCU)",
                "{agent.directive}": agent["directive"],
                "{CcuBlueprint}": ccu_cast_text,
                "{PriorTheses}": prompts.render_prior_theses(prior),
                "{Topic}": topic,
                "{Tags}": tags,
                "{ContextMaterial}": self._context_material,
                "{PositionalTask}": prompts.POSITIONAL_TASKS_ROUND1[i],
                "{HandoffTarget}": prompts.handoff_for(i),
            }) + self._feedback_block(agent["name"], 1)
            thesis = self._agent_call(i, agent, thesis_prompt)
            self._record_agent_turn(1, i, agent["name"], thesis)
            self._persist(1, agent["name"], "thesis", thesis,
                          doc=f"round1_agent{i}_{_safe_name(agent['name'])}.md", index=i)
            prior.append((i, agent["name"], thesis))
            self._log(f"agent {agent['name']} created thesis and was saved")

        # CCU two-part wrap-up: SECTION 1 audit + SECTION 2 round-2 directives.
        self._log("First round is complete. Now CCU sums up Round 1")
        wrapup = self._ccu_call(
            self._render_p5(1, topic, tags, evidence_urls, ccu_cast_text, roster)
            + self._feedback_block("CCU", 1),
            system=prompts.P1_CCU_SUPERVISOR,
        )
        self._persist(1, "CCU", "ccu_wrapup", wrapup, doc="round1_ccu_wrapup.md")
        self._log("Round 1 Wrap Up Report Doc Saved")
        _, directives = split_wrapup_sections(wrapup)

        self._summarize_round(1, roster)

        self._csv_path = self._folder / "run_log.csv"
        with open(self._csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)
        self._flush_rows()
        self._log("Round 1 Ended And CCU Saved The Round Wrap Up Report Sheet")
        return roster, ccu_cast_text, directives

    def _run_main_round(self, round_num: int, roster, topic: str, tags: str,
                        evidence_urls: str, ccu_cast_text: str, directives: str) -> str:
        """One main-loop round: 5 agent turns then the CCU two-part wrap-up."""
        self._log("CCU Generating Prompt for the Next Round")
        self._persist(round_num, "CCU", "ccu_directives", directives,
                      doc=f"round{round_num}_ccu_directives.md")
        self._log("CCU created the round agenda and it was saved in doc file")

        for i, agent in enumerate(roster, start=1):
            reply = self._agent_call(i, agent, self._render_p6(
                agent, i, round_num, topic, tags, directives, ccu_cast_text, roster
            ) + self._feedback_block(agent["name"], round_num))
            self._record_agent_turn(round_num, i, agent["name"], reply)
            self._persist(round_num, agent["name"], "response", reply,
                          doc=f"round{round_num}_agent{i}_{_safe_name(agent['name'])}.md", index=i)
            self._log(f"SubAgent {agent['name']} Thesis created and Saved")

        wrapup = self._ccu_call(
            self._render_p5(round_num, topic, tags, evidence_urls, ccu_cast_text, roster)
            + self._feedback_block("CCU", round_num),
            system=prompts.P1_CCU_SUPERVISOR,
        )
        self._persist(round_num, "CCU", "ccu_wrapup", wrapup,
                      doc=f"round{round_num}_ccu_wrapup.md")
        self._log("CCU Wrapped Up the Past Round and Saved Doc")
        _, next_directives = split_wrapup_sections(wrapup)

        self._summarize_round(round_num, roster)
        self._flush_rows()
        self._log("Round Ended And Sheet Updated")
        return next_directives

    def _run_final_round(self, round_num: int, roster, topic: str, tags: str,
                         agent_hints: str, evidence_urls: str, ccu_cast_text: str) -> str:
        """One final-conclusion round: CCU directive then 5 closing statements."""
        self._log("CCU Generating Prompt for the Final Rounds")
        final_agenda = self._ccu_call(
            self._render_p7(round_num, topic, tags, agent_hints, evidence_urls, ccu_cast_text, roster)
            + self._feedback_block("CCU", round_num),
            system=prompts.P1_CCU_SUPERVISOR,
        )
        self._persist(round_num, "CCU", "ccu_final_agenda", final_agenda,
                      doc=f"round{round_num}_ccu_final_agenda.md")
        self._log("CCU created the final round agenda and it was saved in doc file")

        for i, agent in enumerate(roster, start=1):
            reply = self._agent_call(i, agent, prompts.render(prompts.P6F_AGENT_FINAL, {
                "{i}": str(i),
                "{agent.name}": agent["name"],
                "{agent.role}": agent["role"],
                "{agent.persona}": agent.get("persona") or "(as briefed by the CCU)",
                "{agent.directive}": agent["directive"],
                "{RoundNum}": str(round_num),
                "{CcuDirectives}": final_agenda,
                "{CcuBlueprint}": self._blueprint_value(ccu_cast_text),
                "{Topic}": topic,
                "{Tags}": tags,
                "{ContextMaterial}": self._context_value(final_agenda),
                "{PositionalTask}": prompts.POSITIONAL_TASKS_FINAL[i],
                "{HandoffTarget}": prompts.handoff_for(i),
                prompts.DIALECTIC_INPUT_MARKER: self._render_inputs(roster, round_num, viewer=i),
            }) + self._feedback_block(agent["name"], round_num))
            self._record_agent_turn(round_num, i, agent["name"], reply)
            self._persist(round_num, agent["name"], "response", reply,
                          doc=f"round{round_num}_agent{i}_{_safe_name(agent['name'])}.md", index=i)
            self._log(f"SubAgent {agent['name']} Thesis created and Saved Loop Final")

        self._summarize_round(round_num, roster)
        self._flush_rows()
        self._log("Round Ended And Sheet Updated")
        return final_agenda

    # ------------------------------------------- closing-phase engine steps --
    def _devils_advocate(self, round_num: int, topic: str, roster) -> str:
        """Adversarial validation before the final output (master file batch 1).
        Returns the block to append to the P8 prompt ('' when the check passes)."""
        report = self._model_call(
            "VALIDATOR", prompts.P1_CCU_SUPERVISOR,
            prompts.render(prompts.P_DA_VALIDATOR, {
                "{Topic}": topic,
                prompts.DIALECTIC_INPUT_MARKER: self._render_inputs(roster, round_num),
            }),
            CCU_MAX_TOKENS, tier="mid", thread=False,
        )
        self._persist(round_num, "CCU", "da_validation", report, doc="da_validation.md")
        if report.strip().upper().startswith("PASS"):
            self._log("Devil's Advocate validation passed — no critical issues")
            return ""
        issues = report.count("\n- ") + report.count("\n* ") or 1
        self._log(f"Devil's Advocate validation flagged issues ({issues} noted)")
        return (
            "\n\nDEVIL'S ADVOCATE FINDINGS — the self-correction layer reviewed the final "
            "contributions and reported the issues below. Address them explicitly in your "
            "SECTION 1 audit and correct for them in your SECTION 2 synthesis:\n" + report
        )

    def _gap_analysis(self, round_num: int, topic: str, roster, ccu_cast_text: str) -> str:
        """Gap-Analysis reflection with targeted retrieval loop-back (batch 3).
        Returns the gap-fill block to append to the P8 prompt ('' when clean)."""
        report = self._model_call(
            "VALIDATOR", prompts.P1_CCU_SUPERVISOR,
            prompts.render(prompts.P_GAP_ANALYSIS, {
                "{Topic}": topic,
                "{CcuBlueprint}": self._blueprint_value(ccu_cast_text),
                prompts.DIALECTIC_INPUT_MARKER: self._render_inputs(roster, round_num),
            }),
            CCU_MAX_TOKENS, tier="mid", thread=False,
        )
        self._persist(round_num, "CCU", "gap_analysis", report, doc="gap_analysis.md")
        gaps = GAP_RE.findall(report)
        if not gaps:
            self._log("Gap analysis found no critical gaps")
            return ""
        self._log(f"Gap analysis found {len(gaps)} gap(s) — retrieving targeted material")
        parts = []
        for gap in gaps[:3]:
            chunks = self._index.search_joined(gap, k=3) if self._index else ""
            parts.append(f"GAP: {gap}\nRetrieved material:\n{chunks}")
        return (
            "\n\nGAP-FILL MATERIAL — the Gap-Analysis reflection identified the gaps below; "
            "the following material was retrieved from the mission's grounding sources to "
            "close them. Incorporate it into your synthesis:\n\n" + "\n\n".join(parts)
        )

    # -------------------------------------------------------- prompt renders --
    def _blueprint_value(self, ccu_cast_text: str) -> str:
        """What {CcuBlueprint} carries after round 1: the full Foundation
        Briefing when stateless, a pointer when threaded (each participant's
        thread already holds it from round 1)."""
        return prompts.THREADED_BLUEPRINT_NOTE if self.threaded else ccu_cast_text

    def _context_value(self, query: str) -> str:
        """What {ContextMaterial} carries in rounds 2+: a thread pointer when
        threaded; the top-k chunks relevant to the current directives when
        stateless (the master file's targeted per-turn retrieval); the full
        material as the stateless fallback."""
        if self.threaded:
            return prompts.THREADED_CONTEXT_NOTE
        if _targeted_retrieval() and self._index is not None:
            return self._index.search_joined(
                query, k=5, header="(targeted retrieval — the chunks most relevant to this round)")
        return self._context_material

    def _summary_value(self) -> str:
        return "\n\n".join(self._running_summary) if self._running_summary else "(no earlier rounds yet)"

    def _render_p5(self, round_num, topic, tags, evidence_urls, ccu_cast_text, roster) -> str:
        return prompts.render(prompts.P5_CCU_ROUND_WRAPUP, {
            "{RoundNum}": str(round_num),
            "{NextRound}": str(round_num + 1),
            "{Topic}": topic,
            "{Tags}": tags,
            "{EvidenceUrls}": evidence_urls,
            "{CcuBlueprint}": self._blueprint_value(ccu_cast_text) if round_num > 1 else ccu_cast_text,
            "{RunningSummary}": self._summary_value(),
            prompts.DIALECTIC_INPUT_MARKER: self._render_inputs(roster, round_num),
        })

    def _render_p6(self, agent, i, round_num, topic, tags, directives, ccu_cast_text, roster) -> str:
        return prompts.render(prompts.P6_AGENT_ROUND, {
            "{i}": str(i),
            "{agent.name}": agent["name"],
            "{agent.role}": agent["role"],
            "{agent.persona}": agent.get("persona") or "(as briefed by the CCU)",
            "{agent.directive}": agent["directive"],
            "{RoundNum}": str(round_num),
            "{CcuDirectives}": directives,
            "{CcuBlueprint}": self._blueprint_value(ccu_cast_text),
            "{Topic}": topic,
            "{Tags}": tags,
            "{ContextMaterial}": self._context_value(directives),
            "{RunningSummary}": self._summary_value(),
            "{PositionalTask}": prompts.POSITIONAL_TASKS_LOOP[i],
            "{HandoffTarget}": prompts.handoff_for(i),
            prompts.DIALECTIC_INPUT_MARKER: self._render_inputs(roster, round_num, viewer=i),
        })

    def _render_p7(self, round_num, topic, tags, agent_hints, evidence_urls, ccu_cast_text, roster) -> str:
        return prompts.render(prompts.P7_CCU_FINAL_DIRECTIVE, {
            "{RoundNum}": str(round_num),
            "{FinalRounds}": str(self.final_rounds),
            "{Topic}": topic,
            "{Tags}": tags,
            "{AgentHints}": agent_hints,
            "{EvidenceUrls}": evidence_urls,
            "{CcuBlueprint}": self._blueprint_value(ccu_cast_text),
            "{RunningSummary}": self._summary_value(),
            prompts.DIALECTIC_INPUT_MARKER: self._render_inputs(roster, round_num),
        })

    def _render_p8(self, topic, tags, agent_hints, evidence_urls, ccu_cast_text, roster) -> str:
        return prompts.render(prompts.P8_CCU_WHOLE_DIALECT, {
            "{Topic}": topic,
            "{Tags}": tags,
            "{AgentHints}": agent_hints,
            "{EvidenceUrls}": evidence_urls,
            "{CcuBlueprint}": self._blueprint_value(ccu_cast_text),
            "{RunningSummary}": self._summary_value(),
            prompts.DIALECTIC_INPUT_MARKER: self._render_inputs(roster, 10**9),
        })

    def _render_inputs(self, roster, round_num: int, viewer: Optional[int] = None) -> str:
        """The labeled dialectic-input block: fresh-this-round contributions are
        labeled with their round; carried-over ones are labeled 'Previous'; the
        viewing agent's own entry is labeled as theirs (the refined pack's
        'Your Previous Response')."""
        entries = []
        for i, a in enumerate(roster, start=1):
            name = a["name"]
            text = self._last_responses.get(name, "(no contribution yet)")
            r = self._fresh_round.get(name)
            if viewer is not None and i == viewer:
                label = f"Your Previous Response (Round {r})" if r else "Your Previous Response"
            elif r == round_num:
                label = f"Thesis (Round {r})"
            elif r:
                label = f"Previous Thesis (Round {r})"
            else:
                label = "Previous Thesis"
            entries.append((i, name, label, text))
        return prompts.render_dialectic_input(entries)

    # ------------------------------------------------------------- LLM calls --
    def _ccu_call(self, user_prompt: str, system: str = prompts.P1_CCU_SYSTEM,
                  tier: str = "mid") -> str:
        return self._model_call("CCU", system, user_prompt, CCU_MAX_TOKENS, tier=tier)

    def _agent_call(self, i: int, agent: dict, user_prompt: str) -> str:
        system = prompts.render(prompts.P3_AGENT_SYSTEM, {
            "{i}": str(i),
            "{Audience}": prompts.audience_for(i),
            "{HandoffTarget}": prompts.handoff_for(i),
        })
        return self._model_call(agent["name"], system, user_prompt, AGENT_MAX_TOKENS)

    def _model_call(self, participant: str, system: str, user_prompt: str,
                    max_tokens: int, tier: str = "mid", thread: bool = True) -> str:
        self._checkpoint_pause()
        self._maybe_compact(participant, system, user_prompt, max_tokens, thread=thread)
        self._charge_call()
        self._last_prompt_chars = len(system) + len(user_prompt)
        if self.threaded and thread:
            th = self._threads.setdefault(participant, [])
            th.append({"role": "user", "content": user_prompt})
            self._trim_thread(th)
            text = self.llm.chat(system, th, max_tokens=max_tokens, tier=tier).strip()
            th.append({"role": "assistant", "content": text})
        else:
            text = self.llm.complete(system, user_prompt, max_tokens=max_tokens, tier=tier).strip()
        self._charge_tokens()
        self._calls_since_compact += 1
        self._emit_context_usage()
        return text

    def _trim_thread(self, thread: List[dict]) -> None:
        """Keep the round-1 grounding exchange (the first user/assistant pair
        carries the ContextMaterial/blueprint) plus the last `thread_keep`
        exchanges. The compressed macro-narrative of the dropped middle is
        preserved via the {RunningSummary} injection (the master file's sliding
        summarization), so trimming loses tokens, not information. Called right
        after appending the new user turn, so the deleted middle span is even
        and the kept tail always starts on a user turn."""
        keep_tail = 2 * self.thread_keep + 1
        if len(thread) > 2 + keep_tail:
            del thread[2:len(thread) - keep_tail]

    # -------------------------------------------- context compaction (/compact)
    def _estimate_request(self, participant: str, system: str, user_prompt: str,
                          max_tokens: int, thread: bool = True) -> int:
        """Estimated tokens of the NEXT request for this participant: system +
        thread so far + the new user turn (threaded), plus the reserved output.
        A standalone (thread=False) call never sends the thread, so its
        estimate must not carry it — the big P10 transcript request would
        otherwise false-trip an auto-compaction that cannot shrink it anyway.
        chars/4 mirrors llm._estimate_tokens."""
        chars = len(system) + len(user_prompt)
        if self.threaded and thread:
            chars += sum(len(m["content"]) for m in self._threads.get(participant, []))
        return chars // 4 + max_tokens

    def _context_estimate(self) -> int:
        """Current context pressure: the largest participant footprint plus the
        biggest output reserve a call can carry — what the usage ring shows."""
        if self.threaded and self._threads:
            worst = max(sum(len(m["content"]) for m in th) for th in self._threads.values())
        else:
            worst = self._last_prompt_chars
        return worst // 4 + CCU_MAX_TOKENS

    def _emit_context_usage(self) -> None:
        est = self._context_estimate()
        pct = est / self.context_window if self.context_window > 0 else 0.0
        self.emit("context_usage", {
            "est_tokens": est, "window": self.context_window,
            "pct": round(min(pct, 1.0), 3),
        })

    def _maybe_compact(self, participant: str, system: str, user_prompt: str,
                       max_tokens: int, thread: bool = True) -> None:
        """Run a compaction at this call boundary if the operator clicked the
        ring (manual) or the upcoming request would cross the auto threshold."""
        manual = controls.for_run(self._run_id).pop_compact_request()
        auto = (
            not manual
            and self.compact_at > 0
            and self.context_window > 0
            and self._calls_since_compact >= 5      # anti-thrash guard
            and (self._threads or len(self._running_summary) > 1)
            and self._estimate_request(participant, system, user_prompt, max_tokens, thread=thread)
                >= self.compact_at * self.context_window
        )
        if manual or auto:
            self._compact_context("manual" if manual else "auto")

    def _compact_context(self, reason: str) -> None:
        """Claude-style /compact: replace each participant's conversation thread
        with a high-density digest (low-tier summarizer) and collapse the
        running round summaries, then continue the dialectic from the digest.
        Maintenance calls charge the token budget but not the choreography call
        ceiling — they are not dialectic steps."""
        before = self._context_estimate()
        self._log(f"Context compaction ({reason}) — estimated {before} tokens in the window")
        digests: Dict[str, str] = {}
        for name, th in list(self._threads.items()):
            history = "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in th)
            digest = self._maintenance_call(
                prompts.P11_SUMMARIZER_SYSTEM,
                prompts.render(prompts.P_COMPACT_THREAD, {
                    "{Participant}": name,
                    "{History}": self._fit_history(history),
                }),
                COMPACT_MAX_TOKENS,
            )
            self._threads[name] = [
                {"role": "user",
                 "content": prompts.render(prompts.COMPACTED_CONTEXT, {"{Digest}": digest})},
                {"role": "assistant", "content": prompts.COMPACTED_ACK},
            ]
            digests[name] = digest
        if len(self._running_summary) > 1:
            merged = self._maintenance_call(
                prompts.P11_SUMMARIZER_SYSTEM,
                prompts.render(prompts.P_COMPACT_SUMMARIES, {
                    "{Summaries}": "\n\n".join(self._running_summary),
                }),
                SUMMARY_MAX_TOKENS,
            )
            self._running_summary = [f"(compacted) {merged}"]
        after = self._context_estimate()
        self._calls_since_compact = 0
        if digests:
            note = "\n\n".join(f"### {n}\n{d}" for n, d in digests.items())
            self._persist(
                self._round, "CCU", "context_compacted",
                f"Context compacted ({reason}): estimated {before} -> {after} tokens.\n\n" + note,
                doc=f"context_compaction_call{self._llm_calls}.md",
            )
        self.emit("compacted", {
            "reason": reason, "before_tokens": before, "after_tokens": after,
            "participants": sorted(digests),
        })
        self._emit_context_usage()
        self._log(f"Context compaction complete — {before} -> {after} estimated tokens")

    def _fit_history(self, history: str) -> str:
        """Keep the compaction request itself inside the window: if the joined
        history is too big, keep the head (grounding) and tail (recent turns)
        and elide the middle — the running summaries already carry it."""
        budget = max(4000, (self.context_window - COMPACT_MAX_TOKENS - 1000) * 4)
        if len(history) <= budget:
            return history
        head, tail = int(budget * 0.6), int(budget * 0.3)
        return (history[:head] + "\n\n[... older middle turns elided; see the "
                "running round summaries ...]\n\n" + history[-tail:])

    def _maintenance_call(self, system: str, user: str, max_tokens: int) -> str:
        """Engine-maintenance LLM call (compaction): charges the token budget
        but not the choreography call ceiling, and never threads."""
        text = self.llm.complete(system, user, max_tokens=max_tokens, tier="low").strip()
        self._charge_tokens()
        return text

    # ------------------------------------- convergence & sliding summary ------
    def _record_agent_turn(self, round_num: int, i: int, name: str, text: str) -> None:
        """Track the turn for fresh/previous labeling and the granular debate
        state (STANCE + CONVERGENCE), master file batches 2-3."""
        self._last_responses[name] = text
        self._fresh_round[name] = round_num
        stance = parse_stance(text)
        vote = parse_convergence(text)
        if stance:
            self._stances.setdefault(name, {})[round_num] = stance
        if vote is not None:
            self._votes.setdefault(round_num, {})[name] = vote
        self.store.add_debate_state(self._run_id, round_num, name, stance, vote)

    def _converged(self, round_num: int, roster) -> bool:
        """Early exit for the main loop: average convergence vote clears the
        threshold, or stances have plateaued for the configured streak."""
        if not _convergence_exit():
            return False
        threshold = _convergence_threshold()
        votes = self._votes.get(round_num, {})
        if votes and len(votes) >= len(roster) // 2 + 1:
            avg = sum(votes.values()) / len(votes)
            self.emit("convergence", {"round": round_num, "average": round(avg, 3),
                                      "threshold": threshold})
            if avg >= threshold:
                self._log(f"Convergence vote {avg:.2f} cleared threshold "
                          f"{threshold:.2f} — exiting the round loop early")
                return True
        # Stance stabilization: every agent's stance ~unchanged vs. last round.
        stable = True
        for a in roster:
            hist = self._stances.get(a["name"], {})
            cur, prev = hist.get(round_num), hist.get(round_num - 1)
            if not cur or not prev or difflib.SequenceMatcher(
                    None, cur.lower(), prev.lower()).ratio() < _stance_similarity():
                stable = False
                break
        self._stable_streak = self._stable_streak + 1 if stable else 0
        if self._stable_streak >= _stance_stable_rounds():
            self._log(f"Agent stances plateaued for {self._stable_streak} consecutive "
                      "rounds — exiting the round loop early")
            return True
        return False

    def _summarize_round(self, round_num: int, roster) -> None:
        """Sliding Summarization Layer: one low-tier call compresses the round
        into the running summary; older entries are dropped once past the cap
        (their information already flowed into later summaries)."""
        summary = self._model_call(
            "SUMMARIZER", prompts.P11_SUMMARIZER_SYSTEM,
            prompts.render(prompts.P_ROUND_SUMMARY, {
                "{RoundNum}": str(round_num),
                prompts.DIALECTIC_INPUT_MARKER: self._render_inputs(roster, round_num),
            }),
            SUMMARY_MAX_TOKENS, tier="low", thread=False,
        )
        self._running_summary.append(f"Round {round_num}: {summary}")
        self._persist(round_num, "CCU", "round_summary", summary,
                      doc=f"round{round_num}_summary.md")
        while sum(len(s) for s in self._running_summary) > RUNNING_SUMMARY_CAP \
                and len(self._running_summary) > 1:
            self._running_summary.pop(0)

    # ------------------------------------------------- human-in-the-loop hooks --
    def _checkpoint_pause(self) -> None:
        """Honor a pause before the next LLM call (never mid-call)."""
        ctl = controls.for_run(self._run_id)
        if ctl.paused:
            self.emit("paused", {"tokens": self._tokens_used})
            ctl.wait_until_resumed()
            self.emit("resumed", {})

    def _feedback_block(self, target: str, round_num: int) -> str:
        """Consume the target's pending human feedback and render it as an
        appended prompt section. The verbatim pack prompt stays untouched; the
        operator's note is added after it, clearly attributed."""
        notes = self.store.take_interventions(self._run_id, target)
        if not notes:
            return ""
        for n in notes:
            self._persist_feedback(round_num, target, n["message"])
        bullets = "\n".join(f"- {n['message']}" for n in notes)
        return (
            "\n\nHUMAN OPERATOR INTERVENTION — the human user supervising this run has "
            f"addressed the following feedback directly to you ({target}). Treat it as "
            "authoritative guidance from the mission owner and incorporate it into your "
            "response for this round:\n" + bullets
        )

    def _persist_feedback(self, round_num: int, target: str, text: str) -> None:
        self.store.add_message(self._run_id, round_num, target, text, "human_feedback")
        self._pending_rows.append([
            self._run_id, str(round_num), target, "human_feedback", text,
            datetime.now(timezone.utc).isoformat(),
        ])
        self.emit("human_feedback", {"round": round_num, "target": target, "text": text})

    # ------------------------------------------------ deliverable (P10) --------
    def _compose_deliverable(self, topic: str, roster: List[dict],
                             verdicts: List[Dict[str, str]], synthesis: str) -> tuple[str, bool]:
        """P10: the CCU composes the ONE user-facing document with the refined
        pack's sections, grounded in the complete run transcript (trimmed to
        the working window when needed). The call is standalone rather than
        threaded: the transcript already carries the whole dialectic, and the
        freed window is what lets it travel. The engine verifies the format,
        retries once with a correction, and finally assembles the document
        deterministically from the recorded pieces — the deliverable always
        exists in the right shape."""
        roster_json = json.dumps([{"name": a["name"], "role": a["role"]} for a in roster])
        verdict_text = "\n\n".join(
            f"{v['agent']} — {v['verdict'].upper()}:\n{v['text']}" for v in verdicts)
        fields = {
            "{Topic}": topic,
            "{RosterJson}": roster_json,
            "{Verdicts}": verdict_text,
            "{Synthesis}": synthesis,
        }
        base = prompts.render(prompts.P10_CCU_DELIVERABLE, {**fields, "{Transcript}": ""})
        transcript = render_transcript(
            self._run_id, topic, self.store.get_messages(self._run_id),
            # The synthesis and every verdict already appear verbatim in the
            # prompt; compaction notes are engine maintenance, not debate.
            skip=TRANSCRIPT_SKIP_TYPES | {"final_synthesis", "final_verdict", "context_compacted"},
        )
        prompt = prompts.render(prompts.P10_CCU_DELIVERABLE, {
            **fields,
            "{Transcript}": self._fit_transcript(
                transcript, len(base) + len(prompts.P1_CCU_FINAL)),
        })
        doc = _strip_doc_fences(self._model_call(
            "CCU", prompts.P1_CCU_FINAL, prompt, CCU_MAX_TOKENS, tier="high", thread=False))
        if self._deliverable_ok(doc, roster):
            self._log("CCU composed the single-document deliverable (format verified)")
            return doc, True
        retry = _strip_doc_fences(self._model_call(
            "CCU", prompts.P1_CCU_FINAL,
            prompt + "\n\nFORMAT CORRECTION — your previous attempt did not match the required "
            "structure. Return the corrected FULL markdown document only, with every required "
            "'##' section present and every agent named.",
            CCU_MAX_TOKENS, tier="high", thread=False,
        ))
        if self._deliverable_ok(retry, roster):
            self._log("CCU composed the single-document deliverable on retry (format verified)")
            return retry, True
        self._log("CCU deliverable failed the format check twice — engine assembled it from the recorded pieces")
        return self._fallback_deliverable(topic, roster, verdicts, synthesis), False

    def _fit_transcript(self, transcript: str, fixed_chars: int) -> str:
        """Trim the transcript block so the standalone P10 request fits the
        working window: keep the head (cast and early rounds) and the tail
        (the closing rounds), elide the middle. chars/4 mirrors the usage
        ring's estimate; 2000 chars of headroom absorbs the estimate error."""
        if self.context_window <= 0:
            return transcript
        budget = max(4000, (self.context_window - CCU_MAX_TOKENS) * 4 - fixed_chars - 2000)
        if len(transcript) <= budget:
            return transcript
        head, tail = int(budget * 0.6), int(budget * 0.3)
        return (transcript[:head] + "\n\n[... middle rounds elided to fit the context "
                "window; the full transcript is in transcript.md ...]\n\n" + transcript[-tail:])

    def _write_transcript(self, topic: str) -> Optional[str]:
        """Assemble the complete run record into one readable transcript.md in
        the run folder — the deliverable stays the single readable document;
        this is the full record, offered for deeper analysis."""
        if self._folder is None:
            return None
        path = self._folder / "transcript.md"
        path.write_text(
            render_transcript(self._run_id, topic, self.store.get_messages(self._run_id)),
            encoding="utf-8",
        )
        self._log("Full dialectic transcript assembled and saved (transcript.md)")
        return str(path)

    @staticmethod
    def _deliverable_footer(run_id: str, rounds: int, agents: int) -> str:
        """Engine-stamped closing note offering the full transcript. Stamped in
        code, not by the model, so it is always present and correct — on the
        fallback-assembly path too."""
        return (
            "\n\n---\n"
            f"*This report is the synthesized deliverable of HILCA run `{run_id}` "
            f"({agents} agents, {rounds} rounds). The complete round-by-round transcript "
            "is available for deeper analysis: `transcript.md` in the run folder, or "
            f"`GET /api/runs/{run_id}/transcript` on the HILCA server.*\n"
        )

    REQUIRED_SECTIONS = (
        "## Mission", "## The Dialectic Cast", "## System Audit & Performance Evaluation",
        "## Final Verdicts", "## Final Says", "## Final Master Synthesis & Roadmap",
        "## Conditional Nuances", "## Open Questions & Next Steps",
    )

    @classmethod
    def _deliverable_ok(cls, doc: str, roster: List[dict]) -> bool:
        if not doc.lstrip().startswith("#"):
            return False
        if not all(h in doc for h in cls.REQUIRED_SECTIONS):
            return False
        return all(a["name"] in doc for a in roster)

    @staticmethod
    def _fallback_deliverable(topic: str, roster: List[dict],
                              verdicts: List[Dict[str, str]], synthesis: str) -> str:
        audit, master = split_wrapup_sections(synthesis)
        parts = [
            "# HILCA Mission Deliverable", "",
            "## Mission", topic, "",
            "## The Dialectic Cast",
            *[f"- {a['name']} — {a['role']}" for a in roster], "",
            "## System Audit & Performance Evaluation",
            audit or "See the final synthesis below.", "",
            "## Final Verdicts",
            *[f"- {v['agent']} — {v['verdict'].upper()}" for v in verdicts], "",
            "## Final Says",
        ]
        for v in verdicts:
            parts += [f"### {v['agent']}", v["text"], ""]
        parts += ["## Final Master Synthesis & Roadmap", master or synthesis, "",
                  "## Conditional Nuances",
                  "See the conditions recorded in the synthesis above.", "",
                  "## Open Questions & Next Steps", "See the final synthesis above.", ""]
        return "\n".join(parts)

    def _email_deliverable(self, run: dict, topic: str, doc: str) -> bool:
        """Best-effort email of the deliverable to the intake address; the run
        never fails on delivery problems — the download endpoint always works."""
        import deliver

        email = (run.get("email") or "").strip()
        if not email:
            self._log("No intake email on the run — deliverable available for download only")
            return False
        if not deliver.smtp_configured():
            self._log("SMTP not configured — deliverable available for download only")
            return False
        path = str(self._folder / "deliverable.md") if self._folder else None
        ok = deliver.email_deliverable(email, f"HILCA deliverable — {topic[:80]}", doc, path)
        self._log(f"Deliverable emailed to {email}" if ok
                  else "Deliverable email failed — download remains available")
        return ok

    # ------------------------------------------------ checkpoints (resume) ----
    def _checkpoint(self, phase: str, round_num: int, final_round_num: int,
                    roster, ccu_cast_text: str, directives: str) -> None:
        """Round-boundary snapshot: everything needed to continue the run."""
        self.store.save_checkpoint(self._run_id, {
            "phase": phase,
            "round_num": round_num,
            "final_round_num": final_round_num,
            "roster": roster,
            "ccu_blueprint": ccu_cast_text,
            "directives": directives,
            "last_responses": self._last_responses,
            "fresh_round": self._fresh_round,
            "stances": {k: {str(r): s for r, s in v.items()} for k, v in self._stances.items()},
            "votes": {str(r): v for r, v in self._votes.items()},
            "running_summary": self._running_summary,
            "threads": self._threads,
            "context_material": self._context_material,
            "log_seq": self._log_seq,
            "llm_calls": self._llm_calls,
            "tokens_used": self._tokens_used,
            "main_rounds": self.main_rounds,
            "final_rounds": self.final_rounds,
            "stable_streak": self._stable_streak,
        })

    def _restore(self, ck: dict):
        """Rebuild engine state from a checkpoint (see _checkpoint)."""
        self._last_responses = dict(ck["last_responses"])
        self._fresh_round = {k: int(v) for k, v in ck["fresh_round"].items()}
        self._stances = {k: {int(r): s for r, s in v.items()} for k, v in ck["stances"].items()}
        self._votes = {int(r): dict(v) for r, v in ck["votes"].items()}
        self._running_summary = list(ck["running_summary"])
        self._threads = {k: list(v) for k, v in ck["threads"].items()}
        self._context_material = ck["context_material"]
        self._log_seq = int(ck["log_seq"])
        self._llm_calls = int(ck["llm_calls"])
        self._tokens_used = int(ck["tokens_used"])
        self.main_rounds = int(ck["main_rounds"])
        self.final_rounds = int(ck["final_rounds"])
        self._stable_streak = int(ck.get("stable_streak", 0))
        roster = ck["roster"]
        self._call_ceiling = self._ceiling(len(roster))
        self._folder = Path(self.docs_root) / self._run_id
        self._folder.mkdir(parents=True, exist_ok=True)
        self._csv_path = self._folder / "run_log.csv"
        if not self._csv_path.exists():
            with open(self._csv_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_HEADER)
        return (roster, int(ck["round_num"]), int(ck["final_round_num"]),
                ck["phase"], ck["ccu_blueprint"], ck["directives"])

    # --------------------------------------------------------- safety guards --
    def _ceiling(self, n: int) -> int:
        """Every remaining call is countable: the cast call (+1 count-retry),
        each round's n agent turns + 1 CCU boundary call + 1 round summary,
        then the closing phase: Devil's Advocate + Gap Analysis + P8 + n
        verdicts + P10 (with one format-retry) + P11."""
        per_round = n + 2
        closing = 2 + 1 + n + 2 + 1
        return 2 + (self.main_rounds + self.final_rounds) * per_round + closing

    def _charge_call(self) -> None:
        self._llm_calls += 1
        if self._call_ceiling is not None and self._llm_calls > self._call_ceiling:
            raise RuntimeError(
                f"LLM call ceiling exceeded ({self._llm_calls} > {self._call_ceiling}); "
                "aborting dialectic to prevent runaway spend."
            )

    def _charge_tokens(self) -> None:
        self._tokens_used += int(self.llm.last_usage.get("total_tokens", 0))
        self.llm.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if self._tokens_used > self.token_budget:
            raise RuntimeError(
                f"Token budget exceeded ({self._tokens_used} > {self.token_budget}); "
                "aborting dialectic to prevent unbounded spend."
            )

    # ------------------------------------------------------------ persistence --
    def _persist(self, round_num: int, agent_name: str, msg_type: str, text: str,
                 doc: Optional[str] = None, index: Optional[int] = None) -> None:
        """One message -> DB row + markdown doc + pending CSV row + stream event."""
        self.store.add_message(self._run_id, round_num, agent_name, text, msg_type)
        if doc and self._folder is not None:
            (self._folder / doc).write_text(text, encoding="utf-8")
        self._pending_rows.append([
            self._run_id, str(round_num), agent_name, msg_type, text,
            datetime.now(timezone.utc).isoformat(),
        ])
        if agent_name == "CCU":
            self.emit("ccu_message", {
                "round": round_num, "type": msg_type, "text": text, "tokens": self._tokens_used,
                "calls": self._llm_calls, "ceiling": self._call_ceiling,
            })
        else:
            self.emit("agent_message", {
                "round": round_num, "agent": agent_name, "index": index, "type": msg_type,
                "text": text, "tokens": self._tokens_used,
                "calls": self._llm_calls, "ceiling": self._call_ceiling,
            })

    def _flush_rows(self) -> None:
        """The per-round 'Write Row' step: append this round's rows to the run sheet."""
        if self._csv_path is None:
            return
        with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(self._pending_rows)
        self._pending_rows = []

    def _log(self, message: str) -> None:
        """The '====>' log markers: persisted, ordered, and streamed."""
        self._log_seq += 1
        self.store.add_log(self._run_id, self._log_seq, message)
        self.emit("log", {"seq": self._log_seq, "message": message})
