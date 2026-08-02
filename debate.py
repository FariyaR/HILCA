"""M2: bounded round-table dialectic loop.

Consumes the sub-agent panel spawned in M1 and runs a real-LLM debate across
multiple rounds until convergence or a hard round cap. Feeds each agent (and the
referee) the FULL previous round — no lossy condensation — so debates can be
rich and large-context, while staying bounded by three independent safety guards:

  1. a real round counter with a hard cap (round_num < MAX_ROUNDS),
  2. a total LLM-call ceiling, and
  3. a cumulative token budget (prompt + completion) across the whole debate.

Deliberately does NOT produce a synthesis (that is M4) and does NOT alter how
agents are spawned (M1).
"""
from __future__ import annotations

from typing import List, Optional

from db import Store
from llm import LLMClient
from schemas import SubAgent

MAX_ROUNDS = 20                 # hard circuit breaker (a real counter enforces it)
AGENT_TURN_MAX_TOKENS = 2000    # per-turn output cap (configurable)
TOKEN_BUDGET = 1_000_000        # cumulative prompt+completion tokens per debate
CONVERGENCE_MAX_TOKENS = 200    # per-agent JSON verdict list from the referee
STABLE_ROUNDS_TO_STOP = 2       # consecutive 'no development' verdicts required to stop


def _verdict_developed(verdict: dict) -> bool:
    """Robustly read a referee verdict's 'developed' flag (JSON bool or common string forms)."""
    val = verdict.get("developed")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"true", "yes", "1"}
    return False


class DebateLoop:
    """Runs the dialectic loop over an already-spawned run."""

    def __init__(
        self,
        store: Store,
        llm: LLMClient,
        max_rounds: int = MAX_ROUNDS,
        turn_max_tokens: int = AGENT_TURN_MAX_TOKENS,
        token_budget: int = TOKEN_BUDGET,
        stable_rounds_to_stop: int = STABLE_ROUNDS_TO_STOP,
    ):
        self.store = store
        self.llm = llm
        self.max_rounds = max_rounds
        self.turn_max_tokens = turn_max_tokens
        self.token_budget = token_budget
        self.stable_rounds_to_stop = stable_rounds_to_stop

    def run(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"No run found with id {run_id}")
        if run["status"] != "spawned":
            raise ValueError(
                f"Run {run_id} must be in 'spawned' status to debate (got {run['status']!r})."
            )

        agents = self.store.get_sub_agents(run_id)
        if not agents:
            raise ValueError(f"No sub-agents spawned for run {run_id}")

        topic = run["topic"]
        num_agents = len(agents)
        call_ceiling = num_agents * self.max_rounds + self.max_rounds
        llm_calls = 0
        tokens_used = 0

        self.store.set_status(run_id, "debating")

        round_num = 0
        stable_streak = 0
        stop_reason: Optional[str] = None

        while round_num < self.max_rounds:          # hard cap: real counter, real condition
            round_num += 1
            round_id = self.store.start_round(run_id, round_num)

            if round_num == 1:
                # Round 1: reuse each agent's M1 opening thesis verbatim (no LLM call).
                for agent in agents:
                    self.store.add_message(run_id, round_num, agent.name, agent.thesis, "thesis")
                self.store.complete_round(round_id)
                continue  # nothing to compare against yet -> no convergence check

            # Rounds 2+: every agent takes one real LLM turn off the FULL previous round.
            prev_round = self._render_round(run_id, round_num - 1)
            for agent in agents:
                llm_calls = self._charge_call(llm_calls, call_ceiling, run_id)
                reply = self._agent_turn(agent, topic, prev_round)
                tokens_used = self._charge_tokens(tokens_used, run_id)
                self.store.add_message(run_id, round_num, agent.name, reply, "response")
            self.store.complete_round(round_id)

            # Convergence (primary stop): the referee judges whether this round *developed*
            # the debate. Stop only after `stable_rounds_to_stop` consecutive 'no' verdicts —
            # a single stalled round must not end the debate.
            llm_calls = self._charge_call(llm_calls, call_ceiling, run_id)
            curr_round = self._render_round(run_id, round_num)
            developed = self._has_new_arguments(
                topic, prev_round, curr_round, [a.name for a in agents]
            )
            tokens_used = self._charge_tokens(tokens_used, run_id)
            if developed:
                stable_streak = 0
            else:
                stable_streak += 1
                if stable_streak >= self.stable_rounds_to_stop:
                    stop_reason = "converged"
                    break

        if stop_reason is None:
            stop_reason = "max_rounds"

        self.store.set_status(run_id, stop_reason)  # 'converged' | 'max_rounds'

        return {
            "run_id": run_id,
            "rounds_completed": round_num,
            "stop_reason": stop_reason,
            "llm_calls": llm_calls,
            "call_ceiling": call_ceiling,
            "tokens_used": tokens_used,
            "token_budget": self.token_budget,
        }

    # --- safety guards ------------------------------------------------------
    def _charge_call(self, llm_calls: int, ceiling: int, run_id: str) -> int:
        """Increment the call counter and enforce the runaway-call backstop."""
        llm_calls += 1
        if llm_calls > ceiling:
            self.store.set_status(run_id, "error")
            raise RuntimeError(
                f"LLM call ceiling exceeded ({llm_calls} > {ceiling}); "
                "aborting debate to prevent runaway spend."
            )
        return llm_calls

    def _charge_tokens(self, tokens_used: int, run_id: str) -> int:
        """Accumulate the last call's tokens and enforce the total-token budget."""
        tokens_used += int(self.llm.last_usage.get("total_tokens", 0))
        self.llm.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if tokens_used > self.token_budget:
            self.store.set_status(run_id, "error")
            raise RuntimeError(
                f"Token budget exceeded ({tokens_used} > {self.token_budget}); "
                "aborting debate to prevent unbounded spend."
            )
        return tokens_used

    # --- turns --------------------------------------------------------------
    def _agent_turn(self, agent: SubAgent, topic: str, prev_round: str) -> str:
        system = (
            f"You are {agent.name}, one participant in a structured debate.\n"
            f"Expertise: {agent.expertise}\n"
            f"Mandate: {agent.mandate}\n"
            f"Constraints: {agent.constraints}\n"
            f"Speak in a {agent.tone} tone. Stay strictly in your role."
        )
        user = (
            f"Debate topic: {topic}\n\n"
            f"Full transcript of the previous round:\n{prev_round}\n\n"
            "Take your turn with a focused, substantive response. Do exactly one of: "
            "challenge a specific claim above, refine your own position, or add one new "
            "argument — and develop it with concrete reasoning. Do NOT summarize the whole "
            "debate and do NOT offer a final conclusion."
        )
        return self.llm.complete(system, user, max_tokens=self.turn_max_tokens).strip()

    def _render_round(self, run_id: str, round_num: int) -> str:
        """Full, verbatim transcript of a round (no truncation)."""
        return "\n\n".join(
            f"{m['agent_name']}: {m['message']}"
            for m in self.store.get_messages(run_id, round_num=round_num)
        )

    def _has_new_arguments(
        self, topic: str, prev_round: str, curr_round: str, agent_names: List[str]
    ) -> bool:
        """True only if a MAJORITY of agents introduced something genuinely NEW this round.

        The referee judges each agent individually against the previous round. An agent
        counts as developing only if it introduces a genuinely new argument, new evidence,
        or a new objection — merely elaborating, rephrasing, or re-framing an already-made
        point does NOT count. The round develops only when strictly more than half the
        agents did so.
        """
        num_agents = len(agent_names)
        names = ", ".join(agent_names)
        system = "You are a neutral debate referee. Respond with strict JSON only."
        user = (
            f"Topic: {topic}\n\n"
            f"PREVIOUS round:\n{prev_round}\n\n"
            f"CURRENT round:\n{curr_round}\n\n"
            f"For EACH of these agents in the current round — {names} — judge that agent "
            "individually against the previous round. Mark developed=true ONLY if that agent "
            "introduces a genuinely NEW argument, NEW evidence, or a NEW objection that was not "
            "already present in the previous round. Mark developed=false if the agent merely "
            "elaborates, rephrases, re-frames, adds detail to, or restates a point already made "
            "in the previous round — reworded or deeper delivery of an existing point is NOT "
            "development.\n"
            'Respond with strict JSON only in exactly this shape: '
            '{"verdicts": [{"agent": "<name>", "developed": true}, ...]} — one entry per agent.'
        )
        try:
            result = self.llm.complete_json(system, user, max_tokens=CONVERGENCE_MAX_TOKENS)
            verdicts = result.get("verdicts", [])
            if not isinstance(verdicts, list) or not verdicts:
                return True  # couldn't assess -> keep debating (the hard cap still bounds us)
            developed = sum(1 for v in verdicts if isinstance(v, dict) and _verdict_developed(v))
            # Strict majority: the round develops only if MORE than half the agents did.
            return developed * 2 > num_agents
        except Exception:
            # If the check fails/parses badly, treat as 'developed' and keep debating.
            return True
