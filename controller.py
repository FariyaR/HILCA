"""The Main Controller Agent (the 'CCU' / 'Brain').

Milestone 1 responsibility: given an intake, dynamically define and spawn the
custom sub-agent identities that will later debate the topic. This is the only
'thinking' step in M1 — the round-table dialectic loop that consumes these
agents is Milestone 2 and is intentionally not implemented here.
"""
from __future__ import annotations

from typing import List

from db import Store
from llm import LLMClient
from schemas import SubAgent


CONTROLLER_SYSTEM = """You are the Main Controller Agent of a multi-agent \
dialectic system. Your sole job in this step is to assemble the panel of \
specialist sub-agents who will later debate the user's topic.

Define a focused panel (3-5 agents) calibrated to the topic and tags. Each \
agent must have a distinct, non-overlapping role. You MUST include at least one \
adversarial 'critic'/'skeptic' role whose job is to stress-test the others.

For each agent, also generate their initial THESIS: a concise, specific statement \
of their opening position or argument on the topic. This thesis should be grounded \
in their expertise and reflect their role's bias (e.g., a skeptic's thesis should \
highlight risks, a builder's should highlight opportunities).

Return STRICT JSON ONLY, no prose, in exactly this shape:
{
  "sub_agents": [
    {
      "name": "...",
      "expertise": "...",
      "mandate": "what this agent argues / contributes",
      "constraints": "guardrails: stay in lane, ground claims, don't synthesize",
      "tone": "...",
      "is_critic": true|false,
      "thesis": "This agent's initial position/argument on the topic"
    }
  ]
}"""


def _build_user_prompt(topic: str, tags: List[str], hints: List[str]) -> str:
    return (
        f"Topic: {topic}\n"
        f"Tags: {', '.join(tags) if tags else '(none)'}\n"
        f"Agent hints: {', '.join(hints) if hints else '(none)'}\n\n"
        "Assemble the sub-agent panel for this topic."
    )


class MainController:
    """Spawns sub-agent identities for a run and persists them."""

    def __init__(self, store: Store, llm: LLMClient | None = None):
        self.store = store
        self.llm = llm or LLMClient()

    def spawn_sub_agents(self, run_id: str) -> List[SubAgent]:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"No run found with id {run_id}")

        prompt = _build_user_prompt(run["topic"], run["tags"], run["agent_hints"])
        try:
            data = self.llm.complete_json(CONTROLLER_SYSTEM, prompt)
            raw_agents = data.get("sub_agents", [])
            if not raw_agents:
                raise ValueError("Controller returned an empty panel.")
            agents = [SubAgent(**a) for a in raw_agents]  # validates each
        except Exception:
            self.store.set_status(run_id, "error")
            raise

        # Safety net: guarantee an adversarial voice even if the model forgot.
        if not any(a.is_critic for a in agents):
            agents.append(SubAgent(
                name="Skeptic",
                expertise="Adversarial review of the topic",
                mandate="Stress-test the panel's assumptions and surface failure modes.",
                constraints="Challenge claims; do not produce the final synthesis.",
                tone="Probing, rigorous.",
                is_critic=True,
                thesis="The proposed framework has critical gaps and failure modes that have not been adequately addressed.",
            ))

        self.store.save_sub_agents(run_id, agents)
        self.store.set_status(run_id, "spawned")
        return agents
