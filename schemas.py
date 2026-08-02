"""Typed data contracts for HILCA Milestone 1.

Everything that crosses a boundary (HTTP -> controller -> DB -> LLM) is a
validated Pydantic model, so a malformed intake or a malformed LLM response
fails loudly here instead of corrupting a run downstream.
"""
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


class IntakeRequest(BaseModel):
    """What a user submits (the code equivalent of Shahab's Google Form)."""
    topic: str = Field(..., min_length=1, description="The problem / hypothesis to debate.")
    tags: List[str] = Field(default_factory=list, description="Domain keywords used to calibrate agents.")
    agent_hints: List[str] = Field(default_factory=list, description="Suggested roles, e.g. 'Skeptic', 'Economist'.")
    evidence_urls: List[str] = Field(default_factory=list, description="Optional grounding sources (used in M3).")
    email: Optional[str] = Field(default=None, description="Where results would be sent.")
    main_rounds: Optional[int] = Field(
        default=None, ge=1, le=100,
        description="Cap on the main debate loop (Shahab's phase-2 spec: up to 100, recommended 15-20). None = server default.")
    final_rounds: Optional[int] = Field(
        default=None, ge=0, le=10,
        description="Number of conclusive wrap-up rounds. None = server default.")


class RoleCard(BaseModel):
    """One of the CCU's five dialectic role cards (master file v2).

    The refined cast-selection prompt asks the CCU for a JSON array of exactly
    five cards with Name & Epistemic Role, Attitude/Persona, and the Round-1
    directive (with the Behavioral Protocol embedded). `persona` and `rubric`
    are the master file's Structured Identity Contract extras — validated when
    present, defaulted when the model omits them so a card never fails on the
    optional fields.
    """
    name: str = Field(..., min_length=1, description="Display name, e.g. 'The Skeptic'.")
    role: str = Field(..., min_length=1, description="Epistemic role in the dialectic.")
    directive: str = Field(..., min_length=1, description="Round-1 directive incl. the Behavioral Protocol.")
    persona: str = Field(default="", description="Attitude/Persona: the agent's specific mindset (bias/stance).")
    rubric: str = Field(default="", description="Objective rubric: what a successful contribution looks like.")


class SubAgent(BaseModel):
    """A dynamically spawned agent identity (a 'role card').

    This is the unit Milestone 2 will instantiate into the dialectic loop.
    """
    name: str = Field(..., description="Display name, e.g. 'Systems Architect'.")
    expertise: str = Field(..., description="Domain the agent speaks from.")
    mandate: str = Field(..., description="What this agent is responsible for contributing/arguing.")
    constraints: str = Field(..., description="Guardrails: stay in lane, ground claims in evidence, etc.")
    tone: str = Field(..., description="How the agent communicates.")
    is_critic: bool = Field(default=False, description="True for skeptic/red-team roles.")
    thesis: str = Field(..., description="This agent's initial position/argument on the topic.")


class SpawnResult(BaseModel):
    run_id: str
    topic: str
    sub_agents: List[SubAgent]
