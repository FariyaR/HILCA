"""Tests for the master-file-v2 flow (dialectic.py + prompts.py).

Three layers:
  1. Prompt fidelity — the templates carry the refined pack's verbatim wording
     (Behavioral Protocol, EXACTLY 5, two-part wrap-ups, formatting mandates).
  2. Unit — marker rendering, roster extraction, section splitting, retrieval,
     tier routing, convergence parsing.
  3. Flow — the full choreography on the mock provider: step order, message
     types per round, the sequential cascade, guards, convergence early exit,
     checkpoint resume, docs + CSV artifacts.
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time

import pytest

import prompts
from context_gathering import extract_docx_text
from db import Store
from dialectic import (
    AGENT_COUNT, DialecticFlow, RosterCountError, extract_roster,
    parse_convergence, parse_stance, parse_verdict, split_wrapup_sections,
)
from llm import LLMClient
from schemas import IntakeRequest


# --------------------------------------------------------------------------- #
#  fixtures                                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


@pytest.fixture()
def reference(tmp_path):
    ref = tmp_path / "ref.md"
    ref.write_text("HILCA Master Reference (test stub). Thesis-Antithesis-Synthesis.", encoding="utf-8")
    return str(ref)


@pytest.fixture()
def run_id(store):
    return store.create_run(IntakeRequest(
        topic="Should the team adopt event sourcing?",
        tags=["architecture", "data"],
        agent_hints=["Builder", "Skeptic"],
        evidence_urls=[],
        email="t@example.com",
    ))


class RecordingLLM:
    """Delegates to the mock provider and records every call.

    `calls` keeps (system, user-prompt, tier) for both the stateless complete()
    path and the threaded chat() path (where the user prompt is the thread's
    last message); `threads` additionally snapshots each chat() thread.
    """

    def __init__(self):
        self._llm = LLMClient("mock")
        self.calls: list[tuple[str, str, str]] = []
        self.threads: list[list[dict]] = []

    @property
    def last_usage(self):
        return self._llm.last_usage

    @last_usage.setter
    def last_usage(self, value):
        self._llm.last_usage = value

    def complete(self, system: str, user: str, max_tokens: int = 1000, tier: str = "mid") -> str:
        self.calls.append((system, user, tier))
        return self._llm.complete(system, user, max_tokens=max_tokens, tier=tier)

    def chat(self, system: str, messages: list, max_tokens: int = 1000, tier: str = "mid") -> str:
        self.calls.append((system, messages[-1]["content"], tier))
        self.threads.append([dict(m) for m in messages])
        return self._llm.chat(system, messages, max_tokens=max_tokens, tier=tier)


def run_flow(store, run_id, reference, tmp_path, llm=None, **kw):
    # Pin the round shape the flow tests assume (the env default FINAL_ROUNDS
    # is now 3, matching the original workflow's exactly-three final loop).
    kw.setdefault("main_rounds", 3)
    kw.setdefault("final_rounds", 2)
    events = []
    flow = DialecticFlow(
        store, llm=llm or LLMClient("mock"),
        emit=lambda e, d: events.append((e, d)),
        docs_root=str(tmp_path / "runs"), reference_path=reference, **kw,
    )
    result = flow.run(run_id)
    return result, events


# --------------------------------------------------------------------------- #
#  1. prompt fidelity (refined pack, master file v2)                           #
# --------------------------------------------------------------------------- #
class TestPromptFidelity:
    def test_ccu_system_prompts(self):
        assert 'executive "Prefrontal Cortex"' in prompts.P1_CCU_SYSTEM
        assert '"cognitive laboratory"' in prompts.P1_CCU_SYSTEM
        assert "You must instantiate exactly five agents." in prompts.P1_CCU_SYSTEM
        assert "You must engineer conflict." in prompts.P1_CCU_SYSTEM
        assert "lead research auditor" in prompts.P1_CCU_SUPERVISOR
        assert "Final Executive Synthesis" in prompts.P1_CCU_FINAL

    def test_p2_exactly_five_and_protocol(self):
        assert "STRICT CONSTRAINT: EXACTLY 5 AGENTS" in prompts.P2_CCU_CAST_SELECTION
        assert prompts.BEHAVIORAL_PROTOCOL in prompts.P2_CCU_CAST_SELECTION
        assert "{name, role, persona, directive, rubric}" in prompts.P2_CCU_CAST_SELECTION
        assert "I am the Central Control Unit (CCU), the executive architect of HILCA." in prompts.P2_CCU_CAST_SELECTION
        assert "This concludes the CCU Foundation Briefing for Subagents 1-5." in prompts.P2_CCU_CAST_SELECTION

    def test_behavioral_protocol_sentinels(self):
        assert '**The "Yes, And..." Constraint:**' in prompts.BEHAVIORAL_PROTOCOL
        assert "2+2=5" in prompts.BEHAVIORAL_PROTOCOL
        assert "**Avoid Binary Thinking:**" in prompts.BEHAVIORAL_PROTOCOL
        assert "**Non-Dismissive Engagement:**" in prompts.BEHAVIORAL_PROTOCOL

    def test_p3_agent_system(self):
        assert "You are Subagent {i}" in prompts.P3_AGENT_SYSTEM
        assert "Collaborative Tension" in prompts.P3_AGENT_SYSTEM
        assert '"knowledge gap"' in prompts.P3_AGENT_SYSTEM
        assert "{Audience}" in prompts.P3_AGENT_SYSTEM and "{HandoffTarget}" in prompts.P3_AGENT_SYSTEM

    def test_p5_two_part_wrapup(self):
        assert "PART 1: EXECUTIVE AUDIT & REPORT" in prompts.P5_CCU_ROUND_WRAPUP
        assert "PART 2: ROUND {NextRound} AGENDA & DIRECTIVES" in prompts.P5_CCU_ROUND_WRAPUP
        assert "opinionated supervisor evaluation" in prompts.P5_CCU_ROUND_WRAPUP
        assert "**SECTION 1: AUDIT REPORT**" in prompts.P5_CCU_ROUND_WRAPUP
        assert "**SECTION 2: ROUND {NextRound} DIRECTIVES**" in prompts.P5_CCU_ROUND_WRAPUP

    def test_p6_footer_and_mandate(self):
        assert "STANCE:" in prompts.P6_AGENT_ROUND
        assert "CONVERGENCE:" in prompts.P6_AGENT_ROUND
        assert 'SUBAGENT {i} | ROLE: {agent.name} | ROUND {RoundNum} THESIS' in prompts.P6_AGENT_ROUND
        assert "Handing off to {HandoffTarget}" in prompts.P6_AGENT_ROUND

    def test_p7_final_directive(self):
        assert "This is Round {RoundNum} of the HILCA Final Dialectic Process." in prompts.P7_CCU_FINAL_DIRECTIVE
        assert "**Elaborate**" in prompts.P7_CCU_FINAL_DIRECTIVE
        assert "**Pivot**" in prompts.P7_CCU_FINAL_DIRECTIVE
        assert "agree to disagree" in prompts.P7_CCU_FINAL_DIRECTIVE

    def test_p8_master_synthesis(self):
        assert "SECTION 1: SYSTEM AUDIT & PERFORMANCE EVALUATION" in prompts.P8_CCU_WHOLE_DIALECT
        assert "Actionable Roadmap" in prompts.P8_CCU_WHOLE_DIALECT
        assert "Conditional Nuances" in prompts.P8_CCU_WHOLE_DIALECT
        assert "overall confidence in the outcome as a percentage" in prompts.P8_CCU_WHOLE_DIALECT
        assert "officially complete and wrapped up" in prompts.P8_CCU_WHOLE_DIALECT

    def test_p9_p10_p11(self):
        assert '"VERDICT: AGREE" or "VERDICT: DISAGREE"' in prompts.P9_AGENT_FINAL_VERDICT
        for section in ("## System Audit & Performance Evaluation",
                        "## Final Master Synthesis & Roadmap", "## Conditional Nuances"):
            assert section in prompts.P10_CCU_DELIVERABLE
        assert "Maximum 800 characters" in prompts.P11_LOG_SUMMARY
        assert "Executive Summarizer and Audit Recorder" in prompts.P11_SUMMARIZER_SYSTEM

    def test_templates_keep_injection_markers(self):
        assert "{Topic}" in prompts.P2_CCU_CAST_SELECTION
        assert "{PriorTheses}" in prompts.P4_AGENT_ROUND1_THESIS
        assert "{RoundNum}" in prompts.P5_CCU_ROUND_WRAPUP
        assert "{CcuDirectives}" in prompts.P6_AGENT_ROUND
        assert "{FinalRounds}" in prompts.P7_CCU_FINAL_DIRECTIVE
        for t in (prompts.P5_CCU_ROUND_WRAPUP, prompts.P6_AGENT_ROUND,
                  prompts.P6F_AGENT_FINAL, prompts.P7_CCU_FINAL_DIRECTIVE,
                  prompts.P8_CCU_WHOLE_DIALECT, prompts.P9_AGENT_FINAL_VERDICT):
            assert prompts.DIALECTIC_INPUT_MARKER in t

    def test_positional_tasks_cover_all_five(self):
        for table in (prompts.POSITIONAL_TASKS_ROUND1, prompts.POSITIONAL_TASKS_LOOP,
                      prompts.POSITIONAL_TASKS_FINAL):
            assert set(table) == {1, 2, 3, 4, 5}
        assert "reconcile" in prompts.POSITIONAL_TASKS_ROUND1[3]  # S3 the bridge builder
        assert "weave together" in prompts.POSITIONAL_TASKS_ROUND1[5]  # S5 the weaver

    def test_audience_and_handoff(self):
        assert prompts.audience_for(3) == "Subagents 1, 2, 4, 5, and the CCU"
        assert prompts.handoff_for(1) == "Subagent 2"
        assert prompts.handoff_for(5) == "the CCU"


# --------------------------------------------------------------------------- #
#  2. units: render + roster + parsing + retrieval + tiers                     #
# --------------------------------------------------------------------------- #
class TestRender:
    def test_basic_substitution(self):
        assert prompts.render("A {Topic} B", {"{Topic}": "x"}) == "A x B"

    def test_literal_braces_survive(self):
        out = prompts.render(prompts.P2_CCU_CAST_SELECTION, {
            "{Topic}": "t", "{Tags}": "a", "{AgentHints}": "h",
            "{EvidenceUrls}": "u", "{ContextMaterial}": "c",
        })
        assert "{name, role, persona, directive, rubric}" in out  # literal braces reach the model
        assert "{Topic}" not in out

    def test_dialectic_input_marker_coexists_with_agent_name(self):
        out = prompts.render(prompts.P6_AGENT_ROUND, {
            "{agent.name}": "Builder", "{i}": "1", "{agent.role}": "r",
            "{agent.persona}": "p", "{agent.directive}": "d",
            "{RoundNum}": "2", "{Topic}": "t", "{CcuDirectives}": "directives", "{Tags}": "x",
            "{CcuBlueprint}": "bp", "{ContextMaterial}": "ctx", "{RunningSummary}": "rs",
            "{PositionalTask}": "task", "{HandoffTarget}": "Subagent 2",
            prompts.DIALECTIC_INPUT_MARKER: "- Subagent 1 (Builder) — Thesis (Round 2):\nthesis-1",
        })
        assert "You are Subagent 1 (Builder)" in out
        assert "thesis-1" in out
        assert "{dialectic input" not in out
        assert "{agent.name}" not in out

    def test_injected_values_not_rescanned(self):
        out = prompts.render("X {A} Y {B} Z", {"{A}": "contains {B} literally", "{B}": "b"})
        assert out == "X contains {B} literally Y b Z"

    def test_render_prior_theses(self):
        assert "first subagent to speak" in prompts.render_prior_theses([])
        block = prompts.render_prior_theses([(1, "Builder", "t1"), (2, "Skeptic", "t2")])
        assert "Subagent 1 (Builder) Thesis:\nt1" in block and "t2" in block


def _cards(n, **extra):
    return [dict({"name": f"A{i}", "role": "r", "directive": "d"}, **extra) for i in range(n)]


class TestRosterExtraction:
    def test_fenced_json_five_cards(self):
        text = "prose\n```json\n" + json.dumps(_cards(5)) + "\n```\nmore"
        roster = extract_roster(text)
        assert len(roster) == AGENT_COUNT == 5
        assert roster[0] == {"name": "A0", "role": "r", "directive": "d", "persona": "", "rubric": ""}

    def test_persona_and_rubric_carried(self):
        text = json.dumps(_cards(5, persona="mindset", rubric="measure"))
        roster = extract_roster(text)
        assert all(a["persona"] == "mindset" and a["rubric"] == "measure" for a in roster)

    def test_wrong_count_raises_roster_count_error(self):
        with pytest.raises(RosterCountError, match="exactly 5"):
            extract_roster(json.dumps(_cards(4)))
        with pytest.raises(RosterCountError):
            extract_roster(json.dumps(_cards(50)))

    def test_no_array_raises(self):
        with pytest.raises(ValueError):
            extract_roster("no json here")

    def test_brackets_inside_strings_do_not_break_unfenced_scan(self):
        cards = _cards(5)
        cards[0]["directive"] = "See section 3] of [the doc"
        text = "The cast follows: " + json.dumps(cards) + " done."
        assert extract_roster(text)[0]["directive"] == "See section 3] of [the doc"

    def test_fenced_roster_beats_trailing_schema_example(self):
        text = (
            "Here is the roster:\n```json\n" + json.dumps(_cards(5)) + "\n```\n"
            'Remember the shape: [{"name": "<AgentName>", "role": "<Role>", "directive": "<D>"}]'
        )
        assert extract_roster(text)[0]["name"] == "A0"

    def test_duplicate_names_uniquified(self):
        cards = _cards(5)
        cards[1]["name"] = "A0"
        names = [a["name"] for a in extract_roster(json.dumps(cards))]
        assert names[0] == "A0" and names[1] == "A0 (2)"

    def test_truncated_array_salvages_complete_cards(self):
        # A reply cut off at the output-token cap: the fence and the array
        # bracket never close, but all five card objects are complete.
        text = ("I am the CCU. Briefing prose.\n```json\n[\n"
                + ",\n".join(json.dumps(c) for c in _cards(5)) + ",")
        roster = extract_roster(text)
        assert [a["name"] for a in roster] == [f"A{i}" for i in range(5)]

    def test_truncated_mid_card_becomes_count_retry(self):
        blob = "[\n" + ",\n".join(json.dumps(c) for c in _cards(5))
        cut = blob[: blob.rfind('{"name"')]  # lose the fifth card mid-stream
        with pytest.raises(RosterCountError) as ei:
            extract_roster("prose\n```json\n" + cut)
        assert ei.value.found == 4


class NoArrayCastLLM(RecordingLLM):
    """First cast reply carries no JSON array (a real-model failure seen in
    production); the format-correction retry returns the bare fenced array —
    or keeps failing when `hopeless`."""

    CARDS = [
        {"name": f"Agent {i}", "role": f"Role {i}", "persona": "steady",
         "directive": "Contribute.", "rubric": "clarity"}
        for i in range(1, 6)
    ]

    def __init__(self, hopeless: bool = False):
        super().__init__()
        self.hopeless = hopeless
        self.corrections = 0

    def _intercept(self, user):
        if user.startswith("### MISSION INITIALIZATION"):
            return "I am the CCU. The five agents are described in prose only — no array."
        if user.startswith("FORMAT CORRECTION"):
            self.corrections += 1
            if self.hopeless:
                return "still prose, still no array"
            return "```json\n" + json.dumps(self.CARDS) + "\n```"
        return None

    def complete(self, system, user, max_tokens=1000, tier="mid"):
        r = self._intercept(user)
        if r is not None:
            self.calls.append((system, user, tier))
            return r
        return super().complete(system, user, max_tokens=max_tokens, tier=tier)

    def chat(self, system, messages, max_tokens=1000, tier="mid"):
        r = self._intercept(messages[-1]["content"])
        if r is not None:
            self.calls.append((system, messages[-1]["content"], tier))
            self.threads.append([dict(m) for m in messages])
            return r
        return super().chat(system, messages, max_tokens=max_tokens, tier=tier)


class TestCastFormatRetry:
    def test_unparsable_cast_recovers_via_array_only_retry(self, store, run_id, reference, tmp_path):
        llm = NoArrayCastLLM()
        result, events = run_flow(store, run_id, reference, tmp_path, llm=llm)
        assert llm.corrections == 1
        assert result["agents"] == 5
        spawned = [d["name"] for e, d in events if e == "agent_spawned"]
        assert spawned == [f"Agent {i}" for i in range(1, 6)]
        # The retry spent one of the ceiling's two spare calls.
        assert result["llm_calls"] == result["call_ceiling"] - 1
        # The persisted cast keeps the original briefing prose (the blueprint)
        # plus the corrected array.
        cast_doc = (tmp_path / "runs" / run_id / "round1_ccu_cast.md").read_text(encoding="utf-8")
        assert "prose only" in cast_doc
        assert "CORRECTED ROLE-CARD ARRAY" in cast_doc
        # Threaded mode relies on the CCU thread's memory — the correction
        # must not replay the previous response inline.
        corr = [u for _, u, _ in llm.calls if u.startswith("FORMAT CORRECTION")]
        assert len(corr) == 1 and "YOUR PREVIOUS RESPONSE" not in corr[0]

    def test_stateless_retry_replays_previous_response(self, store, run_id, reference, tmp_path):
        llm = NoArrayCastLLM()
        result, _ = run_flow(store, run_id, reference, tmp_path, llm=llm, threaded=False)
        assert result["agents"] == 5
        corr = [u for _, u, _ in llm.calls if u.startswith("FORMAT CORRECTION")]
        assert len(corr) == 1
        assert "--- YOUR PREVIOUS RESPONSE ---" in corr[0]
        assert "prose only" in corr[0]

    def test_double_failure_persists_raw_cast_evidence(self, store, run_id, reference, tmp_path):
        llm = NoArrayCastLLM(hopeless=True)
        with pytest.raises(RuntimeError, match="format-correction retry"):
            run_flow(store, run_id, reference, tmp_path, llm=llm)
        assert store.get_run(run_id)["status"] == "error"
        raw = [m for m in store.get_messages(run_id) if m["message_type"] == "ccu_cast_raw"]
        assert len(raw) == 1
        assert "prose only" in raw[0]["message"]
        assert "FORMAT-CORRECTION RETRY RESPONSE" in raw[0]["message"]
        doc = tmp_path / "runs" / run_id / "round1_ccu_cast_raw.md"
        assert doc.exists() and "still no array" in doc.read_text(encoding="utf-8")


class TestCompaction:
    def test_manual_compact_rewrites_threads(self, store, run_id, reference, tmp_path):
        """Clicking the ring: the request is honored at the next call boundary,
        every live thread is replaced by a digest, and the choreography's call
        count is untouched (maintenance calls bypass the ceiling)."""
        import controls as controls_mod
        events = []

        def emit(e, d):
            events.append((e, d))
            if e == "round_started" and d["round"] == 2:
                controls_mod.for_run(run_id).request_compact()

        flow = DialecticFlow(
            store, llm=LLMClient("mock"), emit=emit,
            docs_root=str(tmp_path / "runs"), reference_path=reference,
            main_rounds=3, final_rounds=1, compact_at=0,  # auto off: manual only
        )
        result = flow.run(run_id)
        comp = [d for e, d in events if e == "compacted"]
        assert len(comp) == 1 and comp[0]["reason"] == "manual"
        # CCU + the five agents all had threads by round 2.
        assert "CCU" in comp[0]["participants"] and len(comp[0]["participants"]) == 6
        assert comp[0]["after_tokens"] <= comp[0]["before_tokens"]
        # Threads now start from the compacted exchange (trim keeps it forever).
        assert flow._threads["CCU"][0]["content"].startswith("[CONTEXT COMPACTED")
        # The digest row is in the transcript for replay.
        rows = [m for m in store.get_messages(run_id) if m["message_type"] == "context_compacted"]
        assert len(rows) == 1 and "Context compacted (manual)" in rows[0]["message"]
        # Maintenance calls never charge the choreography ceiling.
        assert result["llm_calls"] == result["call_ceiling"] - 2

    def test_auto_compact_when_window_crossed(self, store, run_id, reference, tmp_path):
        """A tiny window forces the auto path: compaction fires without any
        operator action, the run completes, and usage events stream throughout."""
        result, events = run_flow(store, run_id, reference, tmp_path,
                                  context_window=4000, compact_at=0.5)
        auto = [d for e, d in events if e == "compacted" and d["reason"] == "auto"]
        assert auto, "expected at least one automatic compaction"
        usage = [d for e, d in events if e == "context_usage"]
        assert usage and all(0 <= d["pct"] <= 1 for d in usage)
        assert all(d["window"] == 4000 for d in usage)
        assert result["llm_calls"] == result["call_ceiling"] - 2

    def test_no_compaction_at_default_window(self, store, run_id, reference, tmp_path):
        """Mock-sized runs stay far under a 30k window: no auto compaction."""
        result, events = run_flow(store, run_id, reference, tmp_path)
        assert not [d for e, d in events if e == "compacted"]
        assert [d for e, d in events if e == "context_usage"]


class TestParsing:
    def test_parse_verdict(self):
        assert parse_verdict("VERDICT: AGREE\n\nrest") == "agree"
        assert parse_verdict("verdict:  disagree — because…") == "disagree"
        assert parse_verdict("I broadly concur.") == "unclear"

    def test_parse_stance_and_convergence(self):
        text = "body...\n\nDone. Handing off.\nSTANCE: We should proceed.\nCONVERGENCE: 0.75"
        assert parse_stance(text) == "We should proceed."
        assert parse_convergence(text) == 0.75
        assert parse_convergence("CONVERGENCE: 1.5") is None
        assert parse_convergence("no vote") is None
        assert parse_stance("no stance line") is None

    def test_split_wrapup_sections(self):
        text = "header\nSECTION 1: AUDIT REPORT\naudit text\n\nSECTION 2: ROUND 3 DIRECTIVES\ndirectives text"
        audit, directives = split_wrapup_sections(text)
        assert "audit text" in audit and "SECTION 2" not in audit
        assert directives.startswith("SECTION 2") and "directives text" in directives

    def test_split_wrapup_fallback(self):
        audit, directives = split_wrapup_sections("no headers at all")
        assert audit == "" and directives == "no headers at all"


class TestRetrieval:
    def test_chunking_and_search(self):
        from retrieval import HybridIndex, chunk_text
        text = "\n\n".join(
            [f"Paragraph about topic {i}: " + ("lorem ipsum " * 30) for i in range(10)]
            + ["The microservices migration requires a strangler-fig pattern and careful cost analysis."]
        )
        chunks = chunk_text(text)
        assert len(chunks) > 1
        idx = HybridIndex(text)
        hits = idx.search("microservices migration cost", k=2)
        assert any("strangler-fig" in h for h in hits)

    def test_empty_and_joined(self):
        from retrieval import HybridIndex
        idx = HybridIndex("only one short paragraph here")
        assert idx.search("anything unrelated zzz", k=3)  # falls back to first chunk
        joined = idx.search_joined("paragraph", k=1, header="(header)")
        assert joined.startswith("(header)")

    def test_hash_embedder_deterministic_unit_vectors(self):
        from retrieval import HashEmbedder
        e = HashEmbedder()
        v1, v2 = e.embed(["the same text twice"]), e.embed(["the same text twice"])
        assert v1 == v2                                   # crc32, not salted hash()
        norm = sum(x * x for x in v1[0]) ** 0.5
        assert abs(norm - 1.0) < 1e-9
        (a,), (b,) = e.embed(["alpha beta gamma"]), e.embed(["delta epsilon zeta"])
        assert a != b

    def test_vector_backend_active_in_mock_mode(self):
        """conftest pins LLM_PROVIDER=mock, so auto resolves to the offline
        hash embedder — the vector half is exercised with no key, no network."""
        from retrieval import HybridIndex
        idx = HybridIndex("one paragraph about dialectic architectures and governance")
        assert idx.vector_backend == "hash"

    def test_embeddings_off_restores_bm25_only(self, monkeypatch):
        from retrieval import HybridIndex
        monkeypatch.setenv("HILCA_EMBEDDINGS", "off")
        idx = HybridIndex("one paragraph about dialectic architectures")
        assert idx.vector_backend is None
        assert idx.search("dialectic", k=1)               # keyword half still works

    def test_vector_half_drives_ranking_at_alpha_one(self, monkeypatch):
        """A stub embedder makes chunk B the semantic match while BM25 sees a
        tie; with alpha=1 the vector half must decide the winner."""
        from retrieval import HybridIndex

        class StubEmbedder:
            def name(self):
                return "stub"

            def embed(self, texts):
                # queries and 'birds' content share a direction; 'fish' is orthogonal
                return [[0.0, 1.0] if "bird" in t or "query" in t else [1.0, 0.0]
                        for t in texts]

        text = ("common words fish swim ocean\n\n" + "x" * 1300 + "\n\n"
                "common words bird fly sky")
        monkeypatch.setenv("HILCA_HYBRID_ALPHA", "1.0")
        idx = HybridIndex(text, embedder=StubEmbedder())
        assert idx.vector_backend == "stub"
        top = idx.search("query common words", k=1)[0]
        assert "bird" in top

    def test_corpus_embed_failure_degrades_to_bm25(self):
        from retrieval import HybridIndex

        class BrokenEmbedder:
            def name(self):
                return "broken"

            def embed(self, texts):
                raise RuntimeError("no service")

        logs = []
        idx = HybridIndex("a paragraph about strangler-fig migration patterns",
                          embedder=BrokenEmbedder(), log=logs.append)
        assert idx.vector_backend is None
        assert idx.search("strangler-fig migration", k=1)
        assert any("BM25-only" in m for m in logs)

    def test_query_embed_failure_degrades_that_search(self):
        from retrieval import HybridIndex

        class FlakyEmbedder:
            """Embeds the corpus fine, then fails on query embedding."""
            def __init__(self):
                self.calls = 0

            def name(self):
                return "flaky"

            def embed(self, texts):
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("query time failure")
                return [[1.0, 0.0] for _ in texts]

        idx = HybridIndex("a paragraph about cost analysis for migrations",
                          embedder=FlakyEmbedder())
        assert idx.vector_backend == "flaky"
        assert idx.search("cost analysis", k=1)           # BM25 carries the search

    def test_run_logs_the_vector_backend(self, store, run_id, reference, tmp_path):
        run_flow(store, run_id, reference, tmp_path)
        logs = [l["message"] for l in store.get_logs(run_id)]
        assert any("Hybrid retrieval index ready" in m and "vector half: hash" in m
                   for m in logs)


class TestTierRouting:
    def test_model_for_env_precedence(self, monkeypatch):
        client = LLMClient("openai")
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        monkeypatch.delenv("HILCA_MODEL_LOW", raising=False)
        assert client._model_for("low") == "gpt-4o-mini"     # tier default
        monkeypatch.setenv("OPENAI_MODEL", "my-base")
        assert client._model_for("low") == "my-base"         # base env beats default
        monkeypatch.setenv("HILCA_MODEL_LOW", "my-low")
        assert client._model_for("low") == "my-low"          # tier env beats base
        assert client._model_for("high") == "my-base"

    def test_temperature_default_zero(self, monkeypatch):
        monkeypatch.delenv("HILCA_TEMPERATURE", raising=False)
        assert LLMClient._temperature() == 0.0
        monkeypatch.setenv("HILCA_TEMPERATURE", "none")
        assert LLMClient._temperature() is None
        monkeypatch.setenv("HILCA_TEMPERATURE", "0.7")
        assert LLMClient._temperature() == 0.7

    def test_tiers_reach_the_llm(self, store, run_id, reference, tmp_path):
        llm = RecordingLLM()
        run_flow(store, run_id, reference, tmp_path, llm=llm)
        tiers = {u[:40]: t for _, u, t in llm.calls}
        by_tier = {}
        for _, u, t in llm.calls:
            by_tier.setdefault(t, []).append(u)
        assert any(u.startswith("Compress round") for u in by_tier.get("low", []))
        assert any(u.startswith("Summarize the final HILCA") for u in by_tier.get("low", []))
        assert any(u.startswith("The mission is complete") for u in by_tier.get("high", []))
        assert any(u.startswith("You are the Central Control Unit (CCU), executive supervisor")
                   for u in by_tier.get("high", []))


# --------------------------------------------------------------------------- #
#  3. the full flow (mock provider)                                            #
# --------------------------------------------------------------------------- #
class TestFlow:
    def test_round_structure_and_message_types(self, store, run_id, reference, tmp_path):
        result, _ = run_flow(store, run_id, reference, tmp_path)
        assert result["agents"] == 5
        assert result["rounds_completed"] == 5  # MainRounds=3 (incl. round 1) + FinalRounds=2

        by_round = {}
        for m in store.get_messages(run_id):
            by_round.setdefault(m["round_num"], []).append(m["message_type"])

        n = 5
        assert by_round[1] == ["ccu_cast"] + ["thesis"] * n + ["ccu_wrapup", "round_summary"]
        for r in (2, 3):
            assert by_round[r] == ["ccu_directives"] + ["response"] * n + ["ccu_wrapup", "round_summary"]
        assert by_round[4] == ["ccu_final_agenda"] + ["response"] * n + ["round_summary"]
        assert by_round[5] == (["ccu_final_agenda"] + ["response"] * n + ["round_summary"]
                               + ["da_validation", "gap_analysis", "final_synthesis"]
                               + ["final_verdict"] * n + ["deliverable", "log_summary"])

    def test_call_count_matches_ceiling(self, store, run_id, reference, tmp_path):
        result, _ = run_flow(store, run_id, reference, tmp_path)
        n = result["agents"]
        # Per round: n agents + 1 CCU boundary call + 1 round summary; plus the
        # cast call; closing: DA + gap + P8 + n verdicts + P10 + P11. The
        # ceiling allows a cast count-retry and a deliverable format-retry that
        # a clean run never spends.
        expected = 1 + (result["main_rounds"] + result["final_rounds"]) * (n + 2) + (2 + 1 + n + 1 + 1)
        assert result["llm_calls"] == expected
        assert result["call_ceiling"] == expected + 2

    def test_round1_sequential_cascade(self, store, run_id, reference, tmp_path):
        llm = RecordingLLM()
        run_flow(store, run_id, reference, tmp_path, llm=llm)
        p4_calls = [u for _, u, _ in llm.calls if "### ROUND 1: DIALECTIC INITIATION" in u]
        assert len(p4_calls) == 5
        theses = [m["message"] for m in store.get_messages(run_id, round_num=1)
                  if m["message_type"] == "thesis"]
        # Agent 1 sees no prior theses; agent 3 sees agents 1-2; agent 5 sees 1-4.
        assert "first subagent to speak" in p4_calls[0]
        assert theses[0] in p4_calls[2] and theses[1] in p4_calls[2]
        for t in theses[:4]:
            assert t in p4_calls[4]
        # The cascade never leaks forward: agent 1's prompt has no peer thesis.
        assert all(t not in p4_calls[0] for t in theses[1:])

    def test_checklist_log_order(self, store, run_id, reference, tmp_path):
        run_flow(store, run_id, reference, tmp_path)
        logs = [l["message"] for l in store.get_logs(run_id)]
        anchors = [
            "new row read and masterreference downloaded",
            "supportive intake websites and files were scraped and downloaded",
            "CCU created the roles and were saved in doc file",
            "First round is complete. Now CCU sums up Round 1",
            "Round 1 Wrap Up Report Doc Saved",
            "Round 1 Ended And CCU Saved The Round Wrap Up Report Sheet",
            "Initiating Round Loop",
            "CCU Generating Prompt for the Next Round",
            "Loop Round Ended and Final Round Begins",
            "CCU Generating Prompt for the Final Rounds",
            "The whole Dialectic Reasoning is Completed",
            "Final Result summary Log",
            "Executive 800-character log summary recorded",
        ]
        positions = [logs.index(a) for a in anchors]
        assert positions == sorted(positions), "checklist log markers out of order"

    def test_five_card_roster_persisted(self, store, run_id, reference, tmp_path):
        result, events = run_flow(store, run_id, reference, tmp_path)
        roster = store.get_roster(run_id)
        assert len(roster) == result["agents"] == AGENT_COUNT
        assert all({"name", "role", "directive", "persona", "rubric"} <= set(a) for a in roster)
        assert all(a["persona"] for a in roster)  # the mock cast carries personas
        spawned = [d for e, d in events if e == "agent_spawned"]
        assert [a["name"] for a in spawned] == [a["name"] for a in roster]

    def test_docs_and_csv_artifacts(self, store, run_id, reference, tmp_path):
        result, _ = run_flow(store, run_id, reference, tmp_path)
        folder = tmp_path / "runs" / run_id
        files = {f.name for f in folder.iterdir()}
        for expected in ("round1_ccu_cast.md", "round1_ccu_wrapup.md", "round2_ccu_directives.md",
                         "da_validation.md", "gap_analysis.md", "final_synthesis.md",
                         "deliverable.md", "transcript.md", "log_summary.md", "run_log.csv"):
            assert expected in files, f"missing artifact {expected}"
        with open(folder / "run_log.csv", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["RunID", "round", "agent_name", "message_type", "text", "created_at"]
        assert len(rows) - 1 == len(store.get_messages(run_id))  # every message is a sheet row

    def test_status_lifecycle_and_checkpoint_cleared(self, store, run_id, reference, tmp_path):
        run_flow(store, run_id, reference, tmp_path)
        assert store.get_run(run_id)["status"] == "complete"
        assert store.get_checkpoint(run_id) is None  # cleared on completion

    def test_token_budget_guard_aborts(self, store, run_id, reference, tmp_path):
        with pytest.raises(RuntimeError, match="Token budget exceeded"):
            run_flow(store, run_id, reference, tmp_path, token_budget=1)
        assert store.get_run(run_id)["status"] == "error"

    def test_debate_state_recorded(self, store, run_id, reference, tmp_path):
        run_flow(store, run_id, reference, tmp_path)
        rows = store.get_debate_state(run_id)
        assert len(rows) == 5 * 5  # every agent turn in every round
        r1 = [r for r in rows if r["round_num"] == 1]
        assert all(r["stance"] for r in r1)          # STANCE parsed in round 1
        assert all(r["convergence"] is None for r in r1)  # no vote in round 1
        r2 = [r for r in rows if r["round_num"] == 2]
        assert all(r["convergence"] == 0.40 for r in r2)  # the mock's default vote

    def test_800_char_log_summary(self, store, run_id, reference, tmp_path):
        result, events = run_flow(store, run_id, reference, tmp_path)
        assert result["log_summary"] and len(result["log_summary"]) <= 800
        assert any(e == "log_summary" for e, _ in events)
        msgs = [m for m in store.get_messages(run_id) if m["message_type"] == "log_summary"]
        assert len(msgs) == 1


class TestConvergence:
    def test_vote_early_exit(self, store, run_id, reference, tmp_path, monkeypatch):
        monkeypatch.setenv("HILCA_MOCK_CONVERGENCE", "0.95")
        result, events = run_flow(store, run_id, reference, tmp_path,
                                  main_rounds=8, final_rounds=1)
        # Round 2's unanimous 0.95 clears the 0.85 threshold -> exit after
        # round 2, then the single final round.
        assert result["rounds_completed"] == 3
        conv = [d for e, d in events if e == "convergence"]
        assert conv and conv[-1]["average"] == 0.95
        logs = [l["message"] for l in store.get_logs(run_id)]
        assert any("cleared threshold" in m for m in logs)

    def test_stance_plateau_exit(self, store, run_id, reference, tmp_path, monkeypatch):
        # Votes stay low (0.40); the mock's per-round stances differ only by
        # the round number, so they plateau: streak hits 2 at round 4.
        monkeypatch.setenv("HILCA_MOCK_CONVERGENCE", "0.10")
        result, _ = run_flow(store, run_id, reference, tmp_path,
                             main_rounds=8, final_rounds=0)
        assert result["rounds_completed"] == 4
        logs = [l["message"] for l in store.get_logs(run_id)]
        assert any("plateaued" in m for m in logs)

    def test_convergence_exit_can_be_disabled(self, store, run_id, reference, tmp_path, monkeypatch):
        monkeypatch.setenv("HILCA_CONVERGENCE_EXIT", "0")
        monkeypatch.setenv("HILCA_MOCK_CONVERGENCE", "0.99")
        result, _ = run_flow(store, run_id, reference, tmp_path,
                             main_rounds=4, final_rounds=0)
        assert result["rounds_completed"] == 4  # ran to the cap despite the votes


class TestCheckpointResume:
    def test_crash_then_resume_completes(self, store, run_id, reference, tmp_path):
        flow = DialecticFlow(store, llm=LLMClient("mock"), main_rounds=3, final_rounds=1,
                             docs_root=str(tmp_path / "runs"), reference_path=reference)
        orig = flow._run_main_round
        state = {"n": 0}

        def boom(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("simulated crash in round 2")
            return orig(*a, **k)

        flow._run_main_round = boom
        with pytest.raises(RuntimeError, match="simulated crash"):
            flow.run(run_id)
        assert store.get_run(run_id)["status"] == "error"
        ck = store.get_checkpoint(run_id)
        assert ck and ck["phase"] == "main" and ck["round_num"] == 1

        # A fresh engine resumes from the round-1 checkpoint and completes.
        flow2 = DialecticFlow(store, llm=LLMClient("mock"), main_rounds=3, final_rounds=1,
                              docs_root=str(tmp_path / "runs"), reference_path=reference)
        result = flow2.run(run_id, resume=True)
        assert result["rounds_completed"] == 4
        assert store.get_run(run_id)["status"] == "complete"
        assert store.get_checkpoint(run_id) is None
        # Round 1 was not re-run: exactly one cast message and 5 theses exist.
        msgs = store.get_messages(run_id)
        assert sum(1 for m in msgs if m["message_type"] == "ccu_cast") == 1
        assert sum(1 for m in msgs if m["message_type"] == "thesis") == 5

    def test_resume_without_checkpoint_raises(self, store, run_id, reference, tmp_path):
        flow = DialecticFlow(store, llm=LLMClient("mock"),
                             docs_root=str(tmp_path / "runs"), reference_path=reference)
        with pytest.raises(ValueError, match="no checkpoint"):
            flow.run(run_id, resume=True)


class TestPromptInjection:
    """Verify what actually reaches the model, call by call."""

    def test_call_sequence_and_injections(self, store, run_id, reference, tmp_path):
        llm = RecordingLLM()
        result, _ = run_flow(store, run_id, reference, tmp_path, llm=llm)
        n = result["agents"]
        calls = llm.calls

        # Call 0: cast — P1 system + P2 user with intake + context injected.
        system, user, _ = calls[0]
        assert system == prompts.P1_CCU_SYSTEM
        assert user.startswith("### MISSION INITIALIZATION")
        assert "Should the team adopt event sourcing?" in user
        assert "HILCA Master Reference (test stub)" in user

        # Calls 1..n: per-position agent system + P4 cascade prompts.
        roster = store.get_roster(run_id)
        for i, agent in enumerate(roster, start=1):
            system, user, _ = calls[i]
            assert system.startswith(f"Identity & Role: You are Subagent {i},")
            assert f"You are Subagent {i} ({agent['name']})" in user
            assert agent["directive"] in user

        # Call n+1: round-1 two-part wrap-up under the supervisor system prompt.
        system, user, _ = calls[n + 1]
        assert system == prompts.P1_CCU_SUPERVISOR
        assert "We have completed Round 1" in user
        assert "PART 1: EXECUTIVE AUDIT & REPORT" in user
        assert f"({roster[0]['name']})" in user

        # Call n+2: the round-1 sliding summary (low tier, summarizer persona).
        system, user, tier = calls[n + 2]
        assert system == prompts.P11_SUMMARIZER_SYSTEM
        assert user.startswith("Compress round 1") and tier == "low"

        # Round 2 agents receive the SECTION 2 directives from the wrap-up.
        wrapup_r1 = next(m["message"] for m in store.get_messages(run_id, round_num=1)
                         if m["message_type"] == "ccu_wrapup")
        _, directives = split_wrapup_sections(wrapup_r1)
        _, agent1_r2_user, _ = calls[n + 3]
        assert "We are currently executing Round 2" in agent1_r2_user
        assert directives[:60] in agent1_r2_user

        # Within-round freshness: agent 2's round-2 prompt carries agent 1's
        # ROUND-2 reply, labeled as fresh.
        _, agent2_r2_user, _ = calls[n + 4]
        r2_msgs = [m for m in store.get_messages(run_id, round_num=2) if m["message_type"] == "response"]
        agent1_r2 = next(m["message"] for m in r2_msgs if m["agent_name"] == roster[0]["name"])
        assert agent1_r2 in agent2_r2_user
        assert "Thesis (Round 2)" in agent2_r2_user

        # Final rounds: P7 directives + P6F closing statements.
        p7_calls = [u for _, u, _ in calls if u.startswith(
            "You are the Central Control Unit (CCU), managing the executive flow of HILCA.")]
        assert len(p7_calls) == 2  # one directive per final round
        assert "FINAL CONCLUSION ROUND 4" in p7_calls[0]
        assert "FINAL CONCLUSION ROUND 5" in p7_calls[1]
        p6f_calls = [u for _, u, _ in calls
                     if u.startswith("You are Subagent") and "final conclusion loop (Round" in u[:200]]
        assert len(p6f_calls) == 2 * n

        # Closing: DA -> GAP -> P8 -> P9×n -> P10 -> P11, in order (n+5 calls).
        tail = [u for _, u, _ in calls[-(n + 5):]]
        assert tail[0].startswith("DEVIL'S ADVOCATE VALIDATION")
        assert tail[1].startswith("GAP ANALYSIS")
        assert tail[2].startswith("You are the Central Control Unit (CCU), executive supervisor")
        for u in tail[3:3 + n]:
            assert u.startswith("the dialectic has closed.")
        assert tail[3 + n].startswith("The mission is complete and you must now produce")
        assert tail[4 + n].startswith("Summarize the final HILCA CCU Master Synthesis")


# --------------------------------------------------------------------------- #
#  context gathering                                                           #
# --------------------------------------------------------------------------- #
class TestContextGathering:
    def test_master_reference_v2_is_default_and_loads(self):
        from context_gathering import DEFAULT_REFERENCE_PATH, load_master_reference
        assert DEFAULT_REFERENCE_PATH.endswith("HILCA_master_reference_v2.txt")
        text = load_master_reference()
        assert "Behavioral Protocol" in text
        assert "EXACTLY 5" in text

    def test_old_docx_still_extracts(self):
        path = os.path.join(os.path.dirname(__file__), "reference", "Copy of HILCA master reference .docx")
        if not os.path.exists(path):
            pytest.skip("old master reference not present")
        text = extract_docx_text(path)
        assert len(text) > 10000 and "HILCA" in text

    def test_file_urls_rejected(self):
        from context_gathering import fetch_url_text
        with pytest.raises(ValueError, match="scheme not allowed"):
            fetch_url_text("file:///C:/Windows/win.ini")
        with pytest.raises(ValueError, match="scheme not allowed"):
            fetch_url_text("ftp://example.com/x")


# --------------------------------------------------------------------------- #
#  real-run entry-point guards (dialectic_stream)                              #
# --------------------------------------------------------------------------- #
class TestStreamGuards:
    @staticmethod
    def _events(gen):
        events = []
        name = None
        for chunk in gen:
            for line in chunk.splitlines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: ") and name:
                    events.append((name, json.loads(line[6:])))
        return events

    def test_mock_provider_refused_for_real_runs(self, store, run_id, monkeypatch):
        from dialectic_stream import dialectic_event_stream
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.delenv("HILCA_ALLOW_MOCK", raising=False)
        events = self._events(dialectic_event_stream(run_id, store))
        assert len(events) == 1 and events[0][0] == "error"
        assert "mock" in events[0][1]["message"]

    def test_stream_refuses_to_restart_processed_run(self, store, run_id, monkeypatch):
        from dialectic_stream import dialectic_event_stream
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setenv("HILCA_ALLOW_MOCK", "1")
        store.set_status(run_id, "complete")
        events = self._events(dialectic_event_stream(run_id, store))
        assert len(events) == 1 and events[0][0] == "error"
        assert "cannot restart" in events[0][1]["message"]

    def test_resume_stream_requires_checkpoint(self, store, run_id, monkeypatch):
        from dialectic_stream import dialectic_event_stream
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setenv("HILCA_ALLOW_MOCK", "1")
        store.set_status(run_id, "error")
        events = self._events(dialectic_event_stream(run_id, store, resume=True))
        assert len(events) == 1 and events[0][0] == "error"
        assert "no checkpoint" in events[0][1]["message"]

    def test_resume_stream_refuses_completed_run(self, store, run_id, monkeypatch):
        from dialectic_stream import dialectic_event_stream
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setenv("HILCA_ALLOW_MOCK", "1")
        store.set_status(run_id, "complete")
        store.save_checkpoint(run_id, {"phase": "main"})
        events = self._events(dialectic_event_stream(run_id, store, resume=True))
        assert len(events) == 1 and events[0][0] == "error"
        assert "already completed" in events[0][1]["message"]


class TestHumanIntervention:
    def test_feedback_reaches_target_prompts_once(self, store, run_id, reference, tmp_path):
        store.add_intervention(run_id, "CCU", "Focus round 2 on migration costs.")
        store.add_intervention(run_id, "The Skeptic", "Challenge the latency numbers specifically.")
        llm = RecordingLLM()
        run_flow(store, run_id, reference, tmp_path, llm=llm)

        # Each note is INJECTED exactly once, at its target's next prompt. The
        # persisted note may echo once more inside the P10 transcript block —
        # that is the record riding along, not a re-injection.
        ccu_hits = [u for s, u, _ in llm.calls
                    if "HUMAN OPERATOR INTERVENTION" in u and "Focus round 2 on migration costs." in u]
        agent_hits = [u for s, u, _ in llm.calls
                      if "HUMAN OPERATOR INTERVENTION" in u
                      and "Challenge the latency numbers specifically." in u]
        assert len(ccu_hits) == 1
        assert len(agent_hits) == 1 and "(The Skeptic)" in agent_hits[0]

        fb = [m for m in store.get_messages(run_id) if m["message_type"] == "human_feedback"]
        assert {m["agent_name"] for m in fb} == {"CCU", "The Skeptic"}
        assert not store.take_interventions(run_id, "CCU")  # nothing left unconsumed

    def test_feedback_untouched_prompts_have_no_marker(self, store, run_id, reference, tmp_path):
        llm = RecordingLLM()
        run_flow(store, run_id, reference, tmp_path, llm=llm)
        assert all("HUMAN OPERATOR INTERVENTION" not in u for _, u, _ in llm.calls)

    def test_pause_controls(self):
        import controls
        ctl = controls.for_run("test-run-xyz")
        assert not ctl.paused
        ctl.pause()
        assert ctl.paused
        assert controls.for_run("test-run-xyz") is ctl  # same registry entry
        ctl.resume()
        assert not ctl.paused

    def test_feedback_api_validation(self):
        from fastapi.testclient import TestClient
        from web import app, store as webstore
        client = TestClient(app)
        run_id = client.post("/api/runs", json={"topic": "t"}).json()["run_id"]
        # Before the cast exists, only the CCU can receive feedback.
        ok = client.post(f"/api/runs/{run_id}/feedback", json={"target": "CCU", "message": "hi"})
        assert ok.status_code == 200 and ok.json()["queued"]
        bad = client.post(f"/api/runs/{run_id}/feedback", json={"target": "Ghost", "message": "hi"})
        assert bad.status_code == 400
        # After a roster exists, only roster names (or CCU) are accepted.
        webstore.save_roster(run_id, [{"name": "Builder", "role": "r", "directive": "d"}])
        assert client.post(f"/api/runs/{run_id}/feedback", json={"target": "Builder", "message": "m"}).status_code == 200
        assert client.post(f"/api/runs/{run_id}/feedback", json={"target": "Ghost", "message": "m"}).status_code == 400
        assert client.post("/api/runs/nope/feedback", json={"target": "CCU", "message": "m"}).status_code == 404

    def test_pause_api(self):
        from fastapi.testclient import TestClient
        from web import app
        client = TestClient(app)
        run_id = client.post("/api/runs", json={"topic": "t"}).json()["run_id"]
        assert client.post(f"/api/runs/{run_id}/pause").json()["paused"] is True
        assert client.post(f"/api/runs/{run_id}/resume").json()["paused"] is False
        assert client.post("/api/runs/nope/pause").status_code == 404


class TestCastApproval:
    def test_flow_waits_then_uses_edited_roster(self, store, run_id, reference, tmp_path):
        import controls
        llm = RecordingLLM()
        events = []
        flow = DialecticFlow(
            store, llm=llm, emit=lambda e, d: events.append((e, d)),
            docs_root=str(tmp_path / "runs"), reference_path=reference,
            require_approval=True,
        )
        box = {}
        t = threading.Thread(target=lambda: box.update(flow.run(run_id)), daemon=True)
        t.start()

        for _ in range(200):  # wait for the hold (mock is fast, but be patient)
            if any(e == "awaiting_approval" for e, _ in events):
                break
            time.sleep(0.05)
        assert any(e == "awaiting_approval" for e, _ in events), "flow never held for approval"
        assert store.get_run(run_id)["status"] == "awaiting_approval"

        # Operator edits the cast (renames, rewrites) — the count stays 5.
        edited = [
            {"name": f"Edited Agent {i}", "role": f"edited role {i}",
             "directive": f"edited directive {i}", "persona": "edited persona", "rubric": ""}
            for i in range(1, 6)
        ]
        store.save_roster(run_id, edited)
        controls.for_run(run_id).approve()
        t.join(timeout=30)
        assert not t.is_alive(), "flow did not finish after approval"

        # The edited cast drives everything downstream.
        assert box["agents"] == 5
        p4 = [u for _, u, _ in llm.calls if "### ROUND 1: DIALECTIC INITIATION" in u]
        assert any("Edited Agent 2" in u and "edited directive 2" in u for u in p4)
        # The replaced cast never speaks: no P4 identity line uses an original
        # mock name (the CCU's blueprint text may still mention them — that is
        # the briefing the operator edited against).
        assert not any(f"You are Subagent {i} (The" in u for u in p4 for i in range(1, 6))
        assert any(e == "roster_approved" for e, _ in events)
        assert any(m["message_type"] == "cast_approved" for m in store.get_messages(run_id))
        assert store.get_run(run_id)["status"] == "complete"

    def test_approve_endpoint_requires_exactly_five(self):
        import controls
        from fastapi.testclient import TestClient
        from web import app, store as webstore
        client = TestClient(app)
        run_id = client.post("/api/runs", json={"topic": "t"}).json()["run_id"]
        agents = [{"name": f"A{i}", "role": "r", "directive": "d"} for i in range(5)]

        # Not awaiting approval yet -> 409.
        assert client.post(f"/api/runs/{run_id}/approve", json={"roster": agents}).status_code == 409

        webstore.set_status(run_id, "awaiting_approval")
        # Wrong count -> 422 (the master file mandates exactly five).
        assert client.post(f"/api/runs/{run_id}/approve", json={"roster": agents[:1]}).status_code == 422
        assert client.post(f"/api/runs/{run_id}/approve", json={"roster": []}).status_code == 422
        # Duplicate names -> 400.
        dup = [dict(a) for a in agents]
        dup[1]["name"] = "A0"
        assert client.post(f"/api/runs/{run_id}/approve", json={"roster": dup}).status_code == 400

        ok = client.post(f"/api/runs/{run_id}/approve", json={"roster": agents})
        assert ok.status_code == 200 and ok.json() == {"approved": True, "agents": 5}
        saved = webstore.get_roster(run_id)
        assert [a["name"] for a in saved] == [a["name"] for a in agents]
        assert all("persona" in a and "rubric" in a for a in saved)
        assert controls.for_run(run_id).approved
        assert client.post("/api/runs/nope/approve", json={"roster": agents}).status_code == 404


# --------------------------------------------------------------------------- #
#  verdicts + deliverable (P9/P10, merged with the refined sections)           #
# --------------------------------------------------------------------------- #
class TestVerdictsAndDeliverable:
    def test_every_agent_records_a_verdict(self, store, run_id, reference, tmp_path):
        result, events = run_flow(store, run_id, reference, tmp_path)
        n = result["agents"]
        assert len(result["verdicts"]) == n
        # The mock provider makes the Skeptic dissent, everyone else agree —
        # both paths are exercised.
        by_agent = {v["agent"]: v["verdict"] for v in result["verdicts"]}
        assert by_agent["The Skeptic"] == "disagree"
        assert all(v == "agree" for a, v in by_agent.items() if a != "The Skeptic")
        emitted = [d for e, d in events if e == "agent_verdict"]
        assert [(d["agent"], d["verdict"]) for d in emitted] == \
               [(v["agent"], v["verdict"]) for v in result["verdicts"]]
        rows = [m for m in store.get_messages(run_id) if m["message_type"] == "final_verdict"]
        assert len(rows) == n and all(m["message"].startswith("VERDICT:") for m in rows)

    def test_deliverable_carries_the_refined_sections(self, store, run_id, reference, tmp_path):
        result, events = run_flow(store, run_id, reference, tmp_path)
        doc = (tmp_path / "runs" / run_id / "deliverable.md").read_text(encoding="utf-8")
        assert doc.lstrip().startswith("#")
        for section in DialecticFlow.REQUIRED_SECTIONS:
            assert section in doc, f"missing deliverable section {section}"
        roster = store.get_roster(run_id)
        assert all(a["name"] in doc for a in roster)
        deliv = [d for e, d in events if e == "deliverable"]
        assert len(deliv) == 1 and deliv[0]["format_ok"] is True
        assert deliv[0]["emailed"] is False  # no SMTP configured in tests
        assert result["deliverable_path"].endswith("deliverable.md")

    def test_fallback_assembly_when_model_fails_format(self, store, run_id, reference, tmp_path):
        """If the CCU twice returns a malformed deliverable, the engine
        assembles it deterministically — the file always exists, well-formed.
        P10 is a standalone call, so the interception rides the complete() path."""
        class BadDeliverableLLM(RecordingLLM):
            def complete(self, system, user, max_tokens=1000, tier="mid"):
                if user.startswith("The mission is complete and you must now produce"):
                    self.calls.append((system, user, tier))
                    return "sorry, no document"
                return super().complete(system, user, max_tokens=max_tokens, tier=tier)

        result, events = run_flow(store, run_id, reference, tmp_path, llm=BadDeliverableLLM())
        doc = (tmp_path / "runs" / run_id / "deliverable.md").read_text(encoding="utf-8")
        assert doc.startswith("# HILCA Mission Deliverable")
        for section in DialecticFlow.REQUIRED_SECTIONS:
            assert section in doc
        deliv = [d for e, d in events if e == "deliverable"][0]
        assert deliv["format_ok"] is False
        # The format-retry was spent; only the cast count-retry stays unspent.
        assert result["llm_calls"] == result["call_ceiling"] - 1
        # The engine-stamped transcript offer survives the fallback path too.
        assert "complete round-by-round transcript" in doc

    def test_p10_receives_the_full_transcript(self, store, run_id, reference, tmp_path):
        """The whole debate record rides into the P10 prompt so the report can
        cite what was actually said — minus the pieces the prompt already
        carries verbatim (synthesis, verdicts) and engine maintenance."""
        llm = RecordingLLM()
        run_flow(store, run_id, reference, tmp_path, llm=llm)
        p10 = [u for _, u, _ in llm.calls
               if u.startswith("The mission is complete and you must now produce")]
        assert len(p10) == 1
        prompt = p10[0]
        assert "The complete transcript of the dialectic" in prompt
        assert "# HILCA Dialectic — Complete Transcript" in prompt
        assert "### Round 1 · CCU — cast selection & mission blueprint" in prompt
        assert "opening thesis" in prompt          # the agents' round-1 turns
        assert "round wrap-up" in prompt           # the CCU's per-round wrap-ups
        # Deduplicated: verdicts and the synthesis appear once (as prompt
        # fields), not again as transcript entries.
        assert "final verdict & final say" not in prompt
        transcript_block = prompt.split("The complete transcript of the dialectic")[1]
        assert "— Final Synthesis & Executive Audit Report" not in transcript_block

    def test_transcript_file_and_footer(self, store, run_id, reference, tmp_path):
        result, events = run_flow(store, run_id, reference, tmp_path)
        # transcript.md is the complete record, one readable file.
        tpath = tmp_path / "runs" / run_id / "transcript.md"
        assert result["transcript_path"] == str(tpath)
        text = tpath.read_text(encoding="utf-8")
        assert text.startswith("# HILCA Dialectic — Complete Transcript")
        assert f"- Run: `{run_id}`" in text
        assert "### Round 1 · CCU — cast selection & mission blueprint" in text
        assert "final verdict & final say" in text  # the full record keeps the verdicts
        deliv = [d for e, d in events if e == "deliverable"][0]
        assert deliv["transcript_file"] == str(tpath)
        # The deliverable ends with the engine-stamped offer of the transcript.
        doc = (tmp_path / "runs" / run_id / "deliverable.md").read_text(encoding="utf-8")
        assert "complete round-by-round transcript" in doc
        assert f"/api/runs/{run_id}/transcript" in doc

    def test_fit_transcript_elides_the_middle(self, store):
        flow = DialecticFlow(store, llm=LLMClient("mock"), context_window=9000)
        transcript = "x" * 20000
        fitted = flow._fit_transcript(transcript, fixed_chars=1000)
        assert len(fitted) < len(transcript)
        assert "middle rounds elided" in fitted
        assert fitted.startswith("x") and fitted.endswith("x")
        # A window large enough passes the transcript through untouched.
        flow_big = DialecticFlow(store, llm=LLMClient("mock"), context_window=100000)
        assert flow_big._fit_transcript(transcript, fixed_chars=1000) == transcript


class TestClosingEngine:
    def test_devils_advocate_pass_recorded(self, store, run_id, reference, tmp_path):
        run_flow(store, run_id, reference, tmp_path)
        msgs = [m for m in store.get_messages(run_id) if m["message_type"] == "da_validation"]
        assert len(msgs) == 1 and msgs[0]["message"].startswith("PASS")
        logs = [l["message"] for l in store.get_logs(run_id)]
        assert any("Devil's Advocate validation passed" in m for m in logs)

    def test_gap_loopback_feeds_retrieved_material_into_p8(self, store, run_id, reference,
                                                           tmp_path, monkeypatch):
        monkeypatch.setenv("HILCA_MOCK_GAPS", "1")
        llm = RecordingLLM()
        run_flow(store, run_id, reference, tmp_path, llm=llm)
        p8_calls = [u for _, u, _ in llm.calls if u.startswith(
            "You are the Central Control Unit (CCU), executive supervisor")]
        assert len(p8_calls) == 1
        assert "GAP-FILL MATERIAL" in p8_calls[0]
        assert "GAP: validation metrics" in p8_calls[0]
        logs = [l["message"] for l in store.get_logs(run_id)]
        assert any("Gap analysis found 2 gap(s)" in m for m in logs)


class TestThreadedDialectic:
    def test_threads_replace_bulk_reinjection(self, store, run_id, reference, tmp_path):
        llm = RecordingLLM()
        run_flow(store, run_id, reference, tmp_path, llm=llm, threaded=True)
        p6_calls = [u for _, u, _ in llm.calls if "We are currently executing Round" in u[:200]]
        assert p6_calls, "no later-round agent prompts recorded"
        # Rounds 2+ carry the pointers, not the big material again…
        assert all(prompts.THREADED_CONTEXT_NOTE in u for u in p6_calls)
        assert all(prompts.THREADED_BLUEPRINT_NOTE in u for u in p6_calls)
        assert all("HILCA Master Reference (test stub)" not in u for u in p6_calls)
        # …because round 1 put the material into each participant's thread.
        p4_calls = [u for _, u, _ in llm.calls if "### ROUND 1: DIALECTIC INITIATION" in u]
        assert all("HILCA Master Reference (test stub)" in u for u in p4_calls)
        # And the threads really grow as a conversation (alternating turns).
        deepest = max(llm.threads, key=len)
        assert len(deepest) > 2
        assert all(m["role"] == ("user" if i % 2 == 0 else "assistant") for i, m in enumerate(deepest))

    def test_stateless_mode_uses_targeted_retrieval(self, store, run_id, reference, tmp_path):
        llm = RecordingLLM()
        run_flow(store, run_id, reference, tmp_path, llm=llm, threaded=False)
        assert llm.threads == []  # never used the chat path
        p6_calls = [u for _, u, _ in llm.calls if "We are currently executing Round" in u[:200]]
        # The reference stub is a single chunk, so targeted retrieval still
        # surfaces it — under the retrieval header, not as a bulk re-injection.
        assert all("(targeted retrieval" in u for u in p6_calls)
        assert all("HILCA Master Reference (test stub)" in u for u in p6_calls)

    def test_thread_trimming_keeps_grounding_and_alternation(self, store):
        flow = DialecticFlow(store, llm=LLMClient("mock"))
        flow.thread_keep = 2
        thread = []
        for i in range(9):
            thread.append({"role": "user", "content": f"u{i}"})
            thread.append({"role": "assistant", "content": f"a{i}"})
        thread.append({"role": "user", "content": "u9"})  # the just-appended turn
        flow._trim_thread(thread)
        # First exchange survives (the grounding), then the last keep_tail=5
        # messages ending with the new user turn.
        assert [m["content"] for m in thread] == ["u0", "a0", "u7", "a7", "u8", "a8", "u9"]
        assert all(m["role"] == ("user" if i % 2 == 0 else "assistant") for i, m in enumerate(thread))

    def test_running_summary_reaches_later_rounds(self, store, run_id, reference, tmp_path):
        llm = RecordingLLM()
        run_flow(store, run_id, reference, tmp_path, llm=llm)
        p6_r3 = [u for _, u, _ in llm.calls if "We are currently executing Round 3" in u[:200]]
        assert p6_r3
        # The sliding summary of rounds 1-2 is injected into round 3 prompts.
        assert all("Round 1:" in u and "Round 2:" in u for u in p6_r3)


class TestRoundCaps:
    def test_per_run_round_cap_drives_the_loop(self, store, reference, tmp_path):
        run_id = store.create_run(IntakeRequest(topic="capped run", main_rounds=4, final_rounds=1))
        result, _ = run_flow(store, run_id, reference, tmp_path, main_rounds=4, final_rounds=1)
        assert result["rounds_completed"] == 5
        assert result["main_rounds"] == 4 and result["final_rounds"] == 1

    def test_intake_persists_round_choice(self, store):
        run_id = store.create_run(IntakeRequest(topic="t", main_rounds=20, final_rounds=2))
        run = store.get_run(run_id)
        assert run["main_rounds"] == 20 and run["final_rounds"] == 2
        store.set_rounds(run_id, main_rounds=40)
        run = store.get_run(run_id)
        assert run["main_rounds"] == 40 and run["final_rounds"] == 2  # untouched value survives

    def test_api_validates_round_cap(self):
        from fastapi.testclient import TestClient
        from web import app, store as webstore
        client = TestClient(app)
        assert client.post("/api/runs", json={"topic": "t", "main_rounds": 150}).status_code == 422
        assert client.post("/api/runs", json={"topic": "t", "main_rounds": 0}).status_code == 422
        run_id = client.post("/api/runs", json={"topic": "t", "main_rounds": 20}).json()["run_id"]
        assert webstore.get_run(run_id)["main_rounds"] == 20
        # The operator can re-choose the cap while approving the cast.
        webstore.set_status(run_id, "awaiting_approval")
        agents = [{"name": f"A{i}", "role": "r", "directive": "d"} for i in range(5)]
        ok = client.post(f"/api/runs/{run_id}/approve", json={"roster": agents, "main_rounds": 40})
        assert ok.status_code == 200
        assert webstore.get_run(run_id)["main_rounds"] == 40


class TestDeliverableDownload:
    def test_download_endpoint(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from web import app
        monkeypatch.setenv("HILCA_DOCS_ROOT", str(tmp_path / "runs"))
        client = TestClient(app)
        run_id = client.post("/api/runs", json={"topic": "t"}).json()["run_id"]
        # Not complete yet -> 404 with a helpful message.
        r = client.get(f"/api/runs/{run_id}/deliverable")
        assert r.status_code == 404 and "No deliverable yet" in r.json()["detail"]
        folder = tmp_path / "runs" / run_id
        folder.mkdir(parents=True)
        (folder / "deliverable.md").write_text("# Doc\n\n## Mission\nx", encoding="utf-8")
        r = client.get(f"/api/runs/{run_id}/deliverable")
        assert r.status_code == 200
        assert r.text.startswith("# Doc")
        assert "text/markdown" in r.headers["content-type"]
        assert client.get("/api/runs/nope/deliverable").status_code == 404

    def test_transcript_endpoint(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from web import app, store as webstore
        monkeypatch.setenv("HILCA_DOCS_ROOT", str(tmp_path / "runs"))
        client = TestClient(app)
        assert client.get("/api/runs/nope/transcript").status_code == 404
        run_id = client.post("/api/runs", json={"topic": "t"}).json()["run_id"]
        # No messages yet -> 404 with a helpful message.
        r = client.get(f"/api/runs/{run_id}/transcript")
        assert r.status_code == 404 and "No transcript yet" in r.json()["detail"]
        # No file yet, but messages in the store -> assembled on the fly.
        webstore.add_message(run_id, 1, "CCU", "the cast text", "ccu_cast")
        r = client.get(f"/api/runs/{run_id}/transcript")
        assert r.status_code == 200
        assert r.text.startswith("# HILCA Dialectic — Complete Transcript")
        assert "cast selection & mission blueprint" in r.text and "the cast text" in r.text
        assert "text/markdown" in r.headers["content-type"]
        # A completed run serves the assembled transcript.md verbatim.
        folder = tmp_path / "runs" / run_id
        folder.mkdir(parents=True)
        (folder / "transcript.md").write_text("# HILCA Dialectic — Complete Transcript\nfile wins",
                                              encoding="utf-8")
        r = client.get(f"/api/runs/{run_id}/transcript")
        assert r.status_code == 200 and "file wins" in r.text


class FakeRateLimitError(Exception):
    status_code = 429


class TestRateLimitResilience:
    def test_recoverable_429_retries_then_succeeds(self, monkeypatch):
        import llm as llm_mod
        sleeps = []
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: sleeps.append(s))
        client = LLMClient("mock")
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise FakeRateLimitError(
                    "Rate limit reached for gpt-4o on tokens per min (TPM). Please try again in 1.2s.")
            return "ok"

        assert client._call_with_retries(fn, est_tokens=100) == "ok"
        assert calls["n"] == 3 and len(sleeps) == 2
        assert sleeps[0] >= 1.2  # honors the provider's suggested wait

    def test_oversized_request_fails_fast_with_guidance(self, monkeypatch):
        import llm as llm_mod
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: pytest.fail("must not retry an oversized request"))
        client = LLMClient("mock")
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise FakeRateLimitError(
                "Request too large for gpt-4o on tokens per min (TPM): Limit 30000, Requested 31284. "
                "The input or output tokens must be reduced in order to run successfully.")

        with pytest.raises(RuntimeError, match="HILCA_MAX_CONTEXT_CHARS"):
            client._call_with_retries(fn, est_tokens=31284)
        assert calls["n"] == 1  # no futile retries

    def test_exhausted_retries_raise_with_pacing_hint(self, monkeypatch):
        import llm as llm_mod
        monkeypatch.setenv("HILCA_LLM_RETRIES", "2")
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
        client = LLMClient("mock")
        with pytest.raises(RuntimeError, match="HILCA_TPM_LIMIT"):
            client._call_with_retries(
                lambda: (_ for _ in ()).throw(FakeRateLimitError("Rate limit reached")), est_tokens=10)

    def test_non_rate_limit_errors_pass_through(self):
        client = LLMClient("mock")
        with pytest.raises(ValueError, match="boom"):
            client._call_with_retries(lambda: (_ for _ in ()).throw(ValueError("boom")), est_tokens=10)

    def test_pacer_waits_for_the_rolling_minute(self, monkeypatch):
        import llm as llm_mod
        monkeypatch.setenv("HILCA_TPM_LIMIT", "1000")

        class FakeTime:
            def __init__(self):
                self.t, self.slept = 1000.0, []
            def time(self):
                return self.t
            def sleep(self, s):
                self.slept.append(s)
                self.t += s

        ft = FakeTime()
        monkeypatch.setattr(llm_mod, "time", ft)
        client = LLMClient("mock")
        client._usage_window.append((ft.t - 30, 900))  # 900 tokens spent 30s ago
        assert client._call_with_retries(lambda: "ok", est_tokens=200) == "ok"
        assert ft.slept and 29 <= sum(ft.slept) <= 62  # waited for the window to free

    def test_pacer_lets_oversized_single_call_through_to_the_provider(self, monkeypatch):
        import llm as llm_mod
        monkeypatch.setenv("HILCA_TPM_LIMIT", "1000")
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: pytest.fail("must not sleep on an empty window"))
        client = LLMClient("mock")
        # est > limit with nothing in flight: pacing can't help; the provider's
        # own 'Request too large' then produces the actionable error.
        assert client._call_with_retries(lambda: "ok", est_tokens=5000) == "ok"


class TestWebValidation:
    def test_empty_topic_is_422_not_500(self):
        from fastapi.testclient import TestClient
        from web import app
        client = TestClient(app)
        resp = client.post("/api/runs", json={"topic": ""})
        assert resp.status_code == 422

    def test_intake_returns_token_budget(self, tmp_path):
        from fastapi.testclient import TestClient
        from web import app
        client = TestClient(app)
        resp = client.post("/api/runs", json={"topic": "t"})
        assert resp.status_code == 200
        body = resp.json()
        assert "run_id" in body and body["token_budget"] > 0
