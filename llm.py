"""Provider-agnostic LLM layer.

One interface, three backends:
  - mock      : deterministic, no API key, no spend. Default. Runs the demo and
                tests offline, and lets you show a working M1 without burning budget.
  - anthropic : Claude via the official SDK (lazy-imported).
  - openai    : GPT via the official SDK (lazy-imported, JSON mode).

The controller only ever calls `complete_json(...)`; swapping providers is an
env var, not a code change. This mirrors the provider-agnostic pattern so you're
never locked to one vendor (and the original prototype's hard dependency on a
single gpt-4o is removed).
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from collections import deque
from typing import Any, Dict


def _strip_fences(text: str) -> str:
    """LLMs love wrapping JSON in ```json fences. Remove them."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


# --- rate-limit resilience ----------------------------------------------------
def _is_rate_limit(exc: Exception) -> bool:
    """Provider-agnostic 429 detection (both SDKs raise a RateLimitError with
    status_code 429; checked structurally so the SDKs stay lazy imports)."""
    return exc.__class__.__name__ == "RateLimitError" or getattr(exc, "status_code", None) == 429


def _is_oversized(exc: Exception) -> bool:
    """A single request bigger than the org's per-minute token limit can NEVER
    succeed by retrying — it must shrink. Detect the providers' wording."""
    s = str(exc)
    return "Request too large" in s or "must be reduced" in s or "prompt is too long" in s


def _suggested_wait(exc: Exception) -> float | None:
    """Honor the provider's own hint: 'Please try again in 1.234s' (OpenAI
    message) or a Retry-After response header."""
    m = re.search(r"try again in ([\d.]+)\s*(ms|s)", str(exc), re.IGNORECASE)
    if m:
        val = float(m.group(1))
        return val / 1000 if m.group(2).lower() == "ms" else val
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        try:
            return float(headers.get("retry-after"))
        except (TypeError, ValueError):
            pass
    return None


# Tiered model routing (master file v2, batch 2: "Cost-Aware Orchestration").
# low  -> classification/formatting/log summaries; mid -> agent + CCU round
# work; high -> the final synthesis/deliverable layer. Each tier resolves from
# its env var, falling back to the provider's base model env var, then a
# sensible default — so a single-model setup keeps working unchanged.
_TIER_DEFAULTS = {
    "anthropic": {"low": "claude-haiku-4-5-20251001", "mid": "claude-sonnet-4-5", "high": "claude-sonnet-4-5"},
    "openai": {"low": "gpt-4o-mini", "mid": "gpt-4o", "high": "gpt-4o"},
}


class LLMClient:
    def __init__(self, provider: str | None = None):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "mock")).lower()
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        # Rolling (timestamp, total_tokens) of real calls, for the TPM pacer.
        self._usage_window: deque = deque()

    def _model_for(self, tier: str) -> str:
        """Resolve the model id for a tier: HILCA_MODEL_<TIER> beats the
        provider's base env var (ANTHROPIC_MODEL/OPENAI_MODEL) beats defaults."""
        tier = tier if tier in ("low", "mid", "high") else "mid"
        by_tier = os.getenv(f"HILCA_MODEL_{tier.upper()}")
        if by_tier:
            return by_tier
        base_env = "ANTHROPIC_MODEL" if self.provider == "anthropic" else "OPENAI_MODEL"
        base = os.getenv(base_env)
        if base:
            return base
        return _TIER_DEFAULTS.get(self.provider, _TIER_DEFAULTS["openai"])[tier]

    @staticmethod
    def _temperature() -> float | None:
        """HILCA_TEMPERATURE, default 0 — the original ran every call at
        temperature 0. Set to 'none' to fall back to provider defaults."""
        raw = os.getenv("HILCA_TEMPERATURE", "0").strip().lower()
        if raw in ("none", ""):
            return None
        try:
            return float(raw)
        except ValueError:
            return 0.0

    # --- rate-limit handling -------------------------------------------------
    def _call_with_retries(self, fn, est_tokens: int):
        """Run one provider call with TPM pacing and 429 retry/backoff.

        - HILCA_TPM_LIMIT (org tokens/min, 0=off): sleeps before the call until
          the rolling-minute spend leaves room for the estimated request.
        - Recoverable 429s (minute budget momentarily exhausted): exponential
          backoff with jitter, honoring the provider's suggested wait, up to
          HILCA_LLM_RETRIES attempts.
        - Unrecoverable 429s (single request over the org limit): fail fast
          with instructions — waiting can never fix an oversized prompt.
        """
        self._pace(est_tokens)
        retries = max(1, int(os.getenv("HILCA_LLM_RETRIES", "5")))
        delay = float(os.getenv("HILCA_LLM_BACKOFF_BASE", "2"))
        for attempt in range(1, retries + 1):
            try:
                return fn()
            except Exception as exc:
                if not _is_rate_limit(exc):
                    raise
                if _is_oversized(exc):
                    raise RuntimeError(
                        f"A single request exceeded the model's token limit: {exc}\n"
                        "Retrying cannot fix this — the prompt itself must shrink. Lower "
                        "HILCA_MAX_CONTEXT_CHARS / HILCA_MAX_REFERENCE_CHARS / "
                        "HILCA_MAX_SOURCE_CHARS in .env (smaller ContextMaterial), and/or "
                        "reduce HILCA_AGENT_MAX_TOKENS, or use a model/org tier with a "
                        "higher tokens-per-minute limit."
                    ) from exc
                if attempt == retries:
                    raise RuntimeError(
                        f"LLM still rate-limited after {retries} attempts: {exc}\n"
                        "Set HILCA_TPM_LIMIT to your org's tokens-per-minute limit in .env "
                        "so HILCA paces its calls instead of hitting the wall."
                    ) from exc
                wait = _suggested_wait(exc) or delay
                time.sleep(min(wait, 90) + random.uniform(0, 1))
                delay = min(delay * 2, 60)

    def _pace(self, est_tokens: int) -> None:
        limit = int(os.getenv("HILCA_TPM_LIMIT", "0"))
        if limit <= 0:
            return
        while True:
            now = time.time()
            while self._usage_window and now - self._usage_window[0][0] > 60:
                self._usage_window.popleft()
            used = sum(t for _, t in self._usage_window)
            if used + est_tokens <= limit or not self._usage_window:
                # An empty window with est > limit is an oversized single call:
                # pacing can't help — let the provider reject it and the
                # oversized handler explain the fix.
                return
            oldest = self._usage_window[0][0]
            time.sleep(min(60.0, 60.0 - (now - oldest) + 0.5))

    def complete_json(self, system: str, user: str, max_tokens: int | None = None) -> Dict[str, Any]:
        """Return the model's reply parsed as a JSON object."""
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if self.provider == "mock":
            raw = self._mock(system, user)
        elif self.provider == "anthropic":
            raw = self._anthropic(system, user, max_tokens=max_tokens, json_mode=True)
        elif self.provider == "openai":
            raw = self._openai(system, user, max_tokens=max_tokens, json_mode=True)
        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {self.provider}")
        return json.loads(_strip_fences(raw))

    def complete(self, system: str, user: str, max_tokens: int = 1000, tier: str = "mid") -> str:
        """Return the model's reply as plain text."""
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if self.provider == "mock":
            return self._mock_text(system, user)
        elif self.provider == "anthropic":
            return self._anthropic(system, user, max_tokens=max_tokens, json_mode=False, tier=tier)
        elif self.provider == "openai":
            return self._openai(system, user, max_tokens=max_tokens, json_mode=False, tier=tier)
        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {self.provider}")

    def chat(self, system: str, messages: list, max_tokens: int = 1000, tier: str = "mid") -> str:
        """Multi-turn completion: `messages` is a [{role, content}] thread ending
        with the new user turn. Powers HILCA's threaded (chat-like) dialectic
        mode, where each participant keeps one coherent conversation per run."""
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if self.provider == "mock":
            return self._mock_text(system, messages[-1]["content"])
        elif self.provider == "anthropic":
            return self._anthropic_chat(system, messages, max_tokens=max_tokens, tier=tier)
        elif self.provider == "openai":
            return self._openai_chat(system, messages, max_tokens=max_tokens, tier=tier)
        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {self.provider}")

    # --- backends -----------------------------------------------------------
    @staticmethod
    def _estimate_tokens(char_count: int, max_tokens: int | None) -> int:
        """Rough chars/4 input estimate + the reserved output, for the pacer."""
        return char_count // 4 + (max_tokens or 2000)

    def _anthropic(self, system: str, user: str, max_tokens: int | None = 2000,
                   json_mode: bool = False, tier: str = "mid") -> str:
        return self._anthropic_chat(system, [{"role": "user", "content": user}],
                                    max_tokens=max_tokens, tier=tier)

    def _anthropic_chat(self, system: str, messages: list, max_tokens: int | None = 2000,
                        tier: str = "mid") -> str:
        import anthropic  # lazy

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        model = self._model_for(tier)
        # Prompt caching (master file v2, batch 1: "Strict Prompt Caching
        # Window") — the static heavy assets sit at the start of the request
        # (system prompt; and in threaded mode the round-1 grounding turn), so
        # they are marked as cache breakpoints and reused across the run's
        # iterative rounds instead of re-billed every call.
        use_cache = os.getenv("HILCA_PROMPT_CACHE", "1") == "1"
        system_arg: Any = system
        msgs: list = messages
        if use_cache:
            system_arg = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if len(messages) > 1 and isinstance(messages[0].get("content"), str) \
                    and len(messages[0]["content"]) > 4096:
                msgs = [dict(m) for m in messages]
                msgs[0]["content"] = [{"type": "text", "text": msgs[0]["content"],
                                       "cache_control": {"type": "ephemeral"}}]
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or 2000,
            "system": system_arg,
            "messages": msgs,
        }
        temp = self._temperature()
        if temp is not None:
            kwargs["temperature"] = temp
        est = self._estimate_tokens(len(system) + sum(len(str(m["content"])) for m in messages), max_tokens)
        resp = self._call_with_retries(lambda: client.messages.create(**kwargs), est)
        self._record_usage(getattr(resp, "usage", None))
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    def _openai_chat(self, system: str, messages: list, max_tokens: int | None = None,
                     tier: str = "mid") -> str:
        from openai import OpenAI  # lazy

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        model = self._model_for(tier)
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + list(messages),
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        temp = self._temperature()
        if temp is not None:
            kwargs["temperature"] = temp
        est = self._estimate_tokens(len(system) + sum(len(str(m["content"])) for m in messages), max_tokens)
        resp = self._call_with_retries(lambda: client.chat.completions.create(**kwargs), est)
        self._record_usage(getattr(resp, "usage", None))
        return resp.choices[0].message.content or ""

    def _openai(self, system: str, user: str, max_tokens: int | None = None,
                json_mode: bool = True, tier: str = "mid") -> str:
        from openai import OpenAI  # lazy

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        model = self._model_for(tier)
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        temp = self._temperature()
        if temp is not None:
            kwargs["temperature"] = temp
        est = self._estimate_tokens(len(system) + len(user), max_tokens)
        resp = self._call_with_retries(lambda: client.chat.completions.create(**kwargs), est)
        self._record_usage(getattr(resp, "usage", None))
        return resp.choices[0].message.content or ("{}" if json_mode else "")

    def _mock(self, system: str, user: str) -> str:
        """Deterministic role-card generation derived from the intake.

        Good enough to exercise the full pipeline offline. If agent hints are
        supplied we honour them; otherwise we fall back to a sane Builder /
        Skeptic / Methodologist trio and always guarantee one critic.
        """
        hints = _extract_list(user, "Agent hints:")
        tags = _extract_list(user, "Tags:")
        topic_line = next((line for line in user.splitlines() if line.startswith("Topic:")), "")
        topic = topic_line.replace("Topic:", "").strip() if topic_line else "the stated topic"
        roles = hints or ["Builder", "Skeptic", "Methodologist"]

        domain = ", ".join(tags) if tags else "the stated topic"
        agents = []
        thesis_templates = {
            "builder": f"This approach is viable and offers significant advantages for {domain}.",
            "skeptic": f"This proposal has critical gaps and unexamined failure modes in {domain}.",
            "methodologist": f"The framework requires rigorous validation across multiple {domain} dimensions.",
            "architect": f"The system architecture needs careful redesign to handle {domain} constraints.",
            "analyst": f"The data strongly suggests that {domain} factors will drive success or failure.",
            "critic": f"Current assumptions about {domain} are insufficiently grounded.",
            "red": f"This approach will fail under real-world {domain} conditions.",
            "devil": f"The counterargument to this position is more compelling than recognized.",
        }

        for role in roles:
            critic = any(k in role.lower() for k in ("skeptic", "critic", "red", "devil"))
            role_lower = role.strip().lower()
            thesis = next(
                (v for k, v in thesis_templates.items() if k in role_lower),
                f"The {role.strip().lower()} perspective reveals critical insights about {domain}."
            )
            agents.append({
                "name": role.strip().title(),
                "expertise": f"{role.strip()} grounded in {domain}",
                "mandate": (
                    f"Argue the {role.strip().lower()} position on the topic; "
                    "advance concrete, evidence-backed points."
                ),
                "constraints": (
                    "Stay strictly within your assigned role. Ground every claim "
                    "in the provided context. Do not perform the Controller's "
                    "synthesis job. Structure output so the next agent can build on it."
                ),
                "tone": "Direct, analytical, concise.",
                "is_critic": critic,
                "thesis": thesis,
            })
        if not any(a["is_critic"] for a in agents):
            agents.append({
                "name": "Skeptic",
                "expertise": f"Adversarial review of {domain}",
                "mandate": "Stress-test the other agents' assumptions and surface failure modes.",
                "constraints": "Challenge claims; do not propose the final synthesis.",
                "tone": "Probing, rigorous.",
                "is_critic": True,
                "thesis": f"The proposed approach has critical gaps in {domain} that have not been adequately addressed.",
            })
        raw = json.dumps({"sub_agents": agents})
        self._estimate_usage(system + user, raw)
        return raw


    def _mock_text(self, system: str, user: str) -> str:
        """Generate mock plain text response for M2 debates and the faithful HILCA flow."""
        hilca = self._mock_hilca(user)
        if hilca is not None:
            self._estimate_usage(system + user, hilca)
            return hilca

        agent_name = system.split("You are ")[1].split(",")[0] if "You are " in system else "Agent"

        # Generate contextual responses based on user prompt
        if "Debate" in user or "previous" in user:
            responses = [
                f"I appreciate the points raised. {agent_name} believes we should consider the evidence more carefully.",
                f"While I understand that perspective, the data suggests a different conclusion.",
                f"Building on what was said, I think we're missing a critical dimension here.",
                f"That's a fair point, but we haven't adequately addressed the implementation challenges.",
                f"I respectfully disagree. The underlying assumptions need re-examination.",
            ]
        else:
            responses = [
                f"{agent_name} sees merit in this approach, particularly for implementation.",
                f"This proposal raises important considerations that deserve deeper analysis.",
                f"While promising, we need to address potential failure modes.",
                f"The foundation is sound, though some refinements would strengthen it.",
                f"This warrants careful consideration of both benefits and risks.",
            ]

        import hashlib
        seed = int(hashlib.md5((system + user).encode()).hexdigest(), 16) % len(responses)
        raw = responses[seed]
        self._estimate_usage(system + user, raw)
        return raw

    _MOCK_CAST = [
        {"name": "The Builder", "role": "Constructive architect of the solution space",
         "persona": "Optimistic, expansion-minded, solution-first.",
         "directive": "Open with a Thesis that proposes a concrete, evidence-grounded path forward. "
                      "Follow the Behavioral Protocol: avoid binary thinking, use \"Yes, And...\" logic.",
         "rubric": "A successful contribution proposes at least one actionable mechanism."},
        {"name": "The Skeptic", "role": "Adversarial stress-tester of every claim",
         "persona": "Probing, rigorous, never dismissive.",
         "directive": "Open with an Antithesis that surfaces the strongest failure modes, then a "
                      "\"What if...\" scenario per the Behavioral Protocol.",
         "rubric": "A successful contribution surfaces a real failure mode with conditions attached."},
        {"name": "The Methodologist", "role": "Guardian of rigor and validation",
         "persona": "Systematic, measurement-obsessed.",
         "directive": "Open with a Thesis on how the mission's claims must be measured and validated, "
                      "keeping every critique conditional per the Behavioral Protocol.",
         "rubric": "A successful contribution defines a testable validation path."},
        {"name": "The Data Architect", "role": "Structurer of the evidence base",
         "persona": "Precise, source-grounded, integrative.",
         "directive": "Map the grounded evidence to the mission's claims; flag knowledge gaps rather "
                      "than inventing facts, per the Behavioral Protocol.",
         "rubric": "A successful contribution ties every claim to a provided source."},
        {"name": "The Ethicist", "role": "Keeper of alignment and consequence",
         "persona": "Reflective, consequence-aware, constructive.",
         "directive": "Examine the mission's outcome for alignment and second-order effects, using "
                      "\"Maybe if...\" propositions per the Behavioral Protocol.",
         "rubric": "A successful contribution weighs at least one consequence trade-off."},
    ]

    def _mock_hilca(self, user: str) -> str | None:
        """Deterministic answers for the refined prompt pack (master file v2).

        Recognizes each prompt by its verbatim opening phrase and returns a
        response of the right SHAPE — the cast reply carries the required JSON
        role-card array; agent turns carry the FORMATTING MANDATE header,
        handoff line, and STANCE/CONVERGENCE footer; CCU wrap-ups carry the
        SECTION 1 / SECTION 2 split — so the whole flow is exercisable offline.
        """
        agent_m = re.match(r"(?:### ROUND 1: DIALECTIC INITIATION\s+)?You are Subagent (\d+) \(([^)]+)\)", user)

        if user.startswith("### MISSION INITIALIZATION"):
            return (
                "I am the Central Control Unit (CCU), the executive architect of HILCA. "
                "I am initiating Round 1 of the dialectical reasoning process for Subagents 1-5.\n\n"
                "Mission Synthesis: the core research problem and the desired AGI/ASI-level outcome "
                "have been analyzed from the intake form.\n\n"
                "Dialectic Cast Selection: the mission instantiates exactly five agents, each carrying "
                "the Behavioral Protocol in their directive.\n\n"
                "```json\n" + json.dumps(self._MOCK_CAST, indent=2) + "\n```\n\n"
                "This concludes the CCU Foundation Briefing for Subagents 1-5. Dialectic process now active."
            )

        if agent_m and "### ROUND 1: DIALECTIC INITIATION" in user:
            i, name = agent_m.group(1), agent_m.group(2)
            handoff = f"Subagent {int(i) + 1}" if int(i) < 5 else "the CCU"
            return (
                f"SUBAGENT {i} | ROLE: {name} | ROUND 1 THESIS\n\n"
                f"I am Subagent {i} ({name}). This contribution is for my fellow Subagents and the CCU. "
                f"Grounded in the provided material, my opening position is that the {name} perspective "
                "must anchor this mission. Yes, and — if a peer premise seems weak, I explore the "
                "conditions under which it could hold validity.\n\n"
                f"Subagent {i} Round 1 Thesis Complete. Handing off to {handoff}.\n"
                f"STANCE: The {name} position anchors round 1 as stated."
            )

        if agent_m and "final conclusion loop" in user[:200]:
            i, name = agent_m.group(1), agent_m.group(2)
            rm = re.search(r"final conclusion loop \(Round (\d+)\)", user)
            rnd = rm.group(1) if rm else "?"
            handoff = f"Subagent {int(i) + 1}" if int(i) < 5 else "the CCU for Final Synthesis Wrap-Up"
            return (
                f"SUBAGENT {i} | ROLE: {name} | FINAL ROUND {rnd} CLOSING STATEMENT\n\n"
                f"I am Subagent {i} ({name}). This is Round {rnd} of the HILCA Final Dialectic Process. "
                "My final closing statement consolidates the conditions established across the debate "
                "into an actionable takeaway for the user.\n\n"
                f"Subagent {i} Final Closing Statement Complete. Handing off to {handoff}.\n"
                f"STANCE: The {name} final position is consolidated as recorded."
            )

        if agent_m and "We are currently executing Round" in user[:200]:
            i, name = agent_m.group(1), agent_m.group(2)
            rm = re.search(r"executing Round (\d+)", user)
            rnd = rm.group(1) if rm else "?"
            handoff = f"Subagent {int(i) + 1}" if int(i) < 5 else "the CCU"
            conv = os.getenv("HILCA_MOCK_CONVERGENCE", "0.40")
            return (
                f"SUBAGENT {i} | ROLE: {name} | ROUND {rnd} THESIS\n\n"
                f"I am Subagent {i} ({name}). Following the CCU's Round {rnd} directives, I re-establish "
                "my stance and respond to my peers constructively — where I disagree, I propose the "
                "\"What if...\" conditions under which their premises could hold.\n\n"
                f"Subagent {i} Round {rnd} Thesis Complete. Handing off to {handoff}.\n"
                f"STANCE: The {name} position for round {rnd} holds with refinements.\n"
                f"CONVERGENCE: {conv}"
            )

        if user.startswith('You are the Central Control Unit (CCU), the executive "Prefrontal Cortex" of HILCA. We have completed Round'):
            rm = re.search(r"We have completed Round (\d+)", user)
            rnd = int(rm.group(1)) if rm else 1
            return (
                f"CCU EXECUTIVE REPORT & ROUND {rnd} SYNTHESIS | MISSION: (as stated)\n\n"
                "SECTION 1: AUDIT REPORT\n"
                "All five subagents delivered within persona and adhered to the Behavioral Protocol "
                "(\"Yes, And...\" constructive exploration, no binary dismissals). Supervisor assessment: "
                f"round {rnd} moved the board measurably toward an AGI/ASI-level synthesis.\n\n"
                f"SECTION 2: ROUND {rnd + 1} DIRECTIVES\n"
                "Subagent 1: elaborate your mechanism with granular data. Subagent 2: cross-examine "
                "Subagent 1's strongest claim. Subagent 3: bridge the friction between 1 and 2. "
                "Subagent 4: map remaining claims to evidence. Subagent 5: weave the round toward "
                "synthesis. All agents: remain grounded, non-dismissive, mission-aligned.\n\n"
                f"CCU Round {rnd} Evaluation Complete. Round {rnd + 1} Agenda Dispatched."
            )

        if user.startswith("You are the Central Control Unit (CCU), managing the executive flow of HILCA."):
            rm = re.search(r"FINAL CONCLUSION ROUND (\d+)", user)
            rnd = rm.group(1) if rm else "?"
            return (
                f"This is Round {rnd} of the HILCA Final Dialectic Process.\n\n"
                "Audit: contributions align with the mission. Directives — Subagent 1: Elaborate. "
                "Subagent 2: Pivot toward the reconciled mechanism. Subagent 3: Synthesize/Rebut. "
                "Subagent 4: Elaborate the evidence base. Subagent 5: Synthesize. All agents: deliver "
                "your final closing statements; unanimity is not required — agree to disagree "
                "constructively, grounded in conditions.\n\n"
                f"CCU Final Round {rnd} Directives Dispatched. Subagents, issue your final closing statements."
            )

        if user.startswith("You are the Central Control Unit (CCU), executive supervisor of HILCA."):
            return (
                "I am the CCU, executive orchestrator of HILCA, delivering the final AGI/ASI synthesis report.\n\n"
                "SECTION 1: SYSTEM AUDIT & PERFORMANCE EVALUATION\n"
                "The dialectic maintained persona fidelity and the Behavioral Protocol throughout. "
                "Quality rating: high rigor; overall confidence in the outcome: 85%.\n\n"
                "SECTION 2: FINAL MASTER SYNTHESIS & ROADMAP\n"
                "Unified Resolution: the five closing theses weave into a single reconciled position. "
                "Actionable Roadmap: (1) adopt the reconciled mechanism; (2) validate per the "
                "Methodologist's path; (3) monitor the Ethicist's trade-offs. "
                "Conditional Nuances: variations hold under the conditions the Skeptic recorded.\n\n"
                "I am the CCU. The HILCA dialectical reasoning process is officially complete and wrapped up."
            )

        if user.startswith("DEVIL'S ADVOCATE VALIDATION"):
            return "PASS — no critical issues found."

        if user.startswith("GAP ANALYSIS"):
            if os.getenv("HILCA_MOCK_GAPS") == "1":
                return "GAP: validation metrics for the proposed mechanism\nGAP: cost model under scaling"
            return "NO CRITICAL GAPS"

        if user.startswith("the dialectic has closed."):
            name = user.split('"')[1]
            verdict = "DISAGREE" if "skeptic" in name.lower() else "AGREE"
            return (
                f"VERDICT: {verdict}\n\n"
                f"I am subagent {name}. Final Say: across the dialectic I argued that the "
                f"{name.lower()} perspective is essential to the mission; the evidence I raised "
                "in each round supports my position as recorded. This is my concise, presentable "
                "final statement for the user."
            )

        if user.startswith("The mission is complete and you must now produce the single final deliverable"):
            return (
                "# HILCA Mission Deliverable\n\n"
                "## Mission\nThe topic and goal, restated concisely.\n\n"
                "## The Dialectic Cast\n" + self._mock_deliverable_cast(user) +
                "## System Audit & Performance Evaluation\nSupervisor audit with quality rating; "
                "overall confidence 85%.\n\n"
                "## Final Verdicts\n" + self._mock_deliverable_verdicts(user) +
                "## Final Says\nEach agent's presentable final say, consolidated.\n\n"
                "## Final Master Synthesis & Roadmap\nThe concluding synthesis and the actionable "
                "roadmap of practical steps.\n\n"
                "## Conditional Nuances\nEdge cases and the conditions under which variations hold.\n\n"
                "## Open Questions & Next Steps\nRemaining threads for the user.\n"
            )

        if user.startswith("Summarize the final HILCA CCU Master Synthesis"):
            return (
                "- Topic & Core Problem: the mission topic as recorded.\n"
                "- Primary AGI/ASI Synthesis: the reconciled position developed across the dialectic.\n"
                "- Key Action Items & Next Steps: adopt, validate, monitor per the roadmap."
            )

        if user.startswith("Compress round"):
            rm = re.search(r"Compress round (\d+)", user)
            rnd = rm.group(1) if rm else "?"
            return (
                f"Round {rnd}: all five agents held their positions with refinements; friction "
                "persists between the Builder and the Skeptic; agreement grew on validation needs."
            )

        if user.startswith("COMPACT THE CONVERSATION"):
            pm = re.search(r"The conversation history of (.+?) must be compressed", user)
            who = pm.group(1) if pm else "the participant"
            return (
                f"Digest for {who}: mission, role, persona, and directives retained; grounding "
                "facts from the reference preserved; positions — each agent holds its refined "
                "stance with live friction on validation and governance; agreements and open "
                "questions carried forward; continue from the current round."
            )

        if user.startswith("Merge these running round summaries"):
            return ("(merged) Across all rounds: positions held with refinements; validation "
                    "and governance remain the live friction points; agreements preserved.")

        return None

    @staticmethod
    def _mock_deliverable_cast(user: str) -> str:
        """Echo the injected roster names so the deliverable names every agent."""
        m = re.search(r"Approved cast: (\[.*?\])\n", user, re.DOTALL)
        if not m:
            return "- (cast unavailable)\n\n"
        try:
            roster = json.loads(m.group(1))
            return "".join(f"- {a['name']} — {a['role']}\n" for a in roster) + "\n"
        except (json.JSONDecodeError, KeyError, TypeError):
            return "- (cast unavailable)\n\n"

    @staticmethod
    def _mock_deliverable_verdicts(user: str) -> str:
        m = re.search(r"Each agent's verdict and final say:\n(.*?)\nYour Final Synthesis & Executive Audit Report:",
                      user, re.DOTALL)
        block = m.group(1) if m else ""
        lines = [l for l in block.splitlines() if " — " in l and l.endswith(":")]
        return "".join(f"- {l.rstrip(':')}\n" for l in lines) + "\n" if lines else "- (verdicts unavailable)\n\n"

    def _record_usage(self, usage) -> None:
        """Capture token usage from a provider response (OpenAI or Anthropic shapes)."""
        if usage is None:
            self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            return
        p = getattr(usage, "prompt_tokens", None)
        if p is None:
            p = getattr(usage, "input_tokens", 0)
        c = getattr(usage, "completion_tokens", None)
        if c is None:
            c = getattr(usage, "output_tokens", 0)
        p, c = int(p or 0), int(c or 0)
        self.last_usage = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
        if p + c:
            self._usage_window.append((time.time(), p + c))

    def _estimate_usage(self, prompt_text: str, completion_text: str) -> None:
        """Rough char/4 token estimate for the mock backend (deterministic, offline)."""
        p = max(1, len(prompt_text) // 4)
        c = max(1, len(completion_text) // 4)
        self.last_usage = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _extract_list(user_prompt: str, label: str) -> list[str]:
    """Pull a comma-separated line like 'Tags: a, b, c' out of the prompt."""
    for line in user_prompt.splitlines():
        if line.strip().startswith(label):
            rest = line.split(label, 1)[1].strip()
            return [x.strip() for x in rest.split(",") if x.strip()] if rest and rest != "(none)" else []
    return []


# Alias for compatibility
LLMProvider = LLMClient
_instance = None


def get_llm() -> LLMClient:
    """Get a singleton LLM client."""
    global _instance
    if _instance is None:
        _instance = LLMClient()
    return _instance
