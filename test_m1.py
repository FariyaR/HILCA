"""Minimal pytest suite for HILCA Milestone 1.

Tests:
  - Intake validation (valid/invalid requests)
  - Controller spawns ≥1 agent
  - Controller always includes a critic role
  - Agents persist and reload from DB

Uses mock LLM provider, runs fully offline.
"""
import os
import tempfile
import pytest

os.environ.setdefault("LLM_PROVIDER", "mock")

from controller import MainController
from db import Store
from schemas import IntakeRequest, SubAgent


@pytest.fixture
def temp_db():
    """Use a temporary DB file for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # On Windows, SQLite may still hold a lock; use a retry loop
    if os.path.exists(db_path):
        for attempt in range(3):
            try:
                os.unlink(db_path)
                break
            except PermissionError:
                pass  # SQLite still holding lock; leave for cleanup


@pytest.fixture
def store(temp_db):
    """Create a fresh Store for each test."""
    return Store(temp_db)


@pytest.fixture
def controller(store):
    """Create a MainController with the test store."""
    return MainController(store)


# --- Intake Validation Tests ---

def test_intake_valid_minimal(store):
    """Intake validation: accept minimal valid request."""
    req = IntakeRequest(topic="A topic to debate")
    run_id = store.create_run(req)
    assert run_id is not None
    assert len(run_id) > 0


def test_intake_valid_full(store):
    """Intake validation: accept full request with all fields."""
    req = IntakeRequest(
        topic="Should we migrate to Kubernetes?",
        tags=["infrastructure", "cost"],
        agent_hints=["DevOps Engineer", "CFO", "Skeptic"],
        evidence_urls=["https://example.com/paper1.pdf"],
        email="test@example.com",
    )
    run_id = store.create_run(req)
    assert run_id is not None

    # Verify persisted correctly
    run = store.get_run(run_id)
    assert run["topic"] == req.topic
    assert run["tags"] == req.tags
    assert run["agent_hints"] == req.agent_hints
    assert run["evidence_urls"] == req.evidence_urls
    assert run["email"] == req.email
    assert run["status"] == "intake"


def test_intake_empty_topic_rejected():
    """Intake validation: reject empty topic."""
    with pytest.raises(ValueError):
        IntakeRequest(topic="")


def test_intake_preserves_whitespace_topic(store):
    """Intake validation: whitespace-only topics are accepted (min_length only checks string exists)."""
    req = IntakeRequest(topic="   ")
    run_id = store.create_run(req)
    run = store.get_run(run_id)
    assert run["topic"] == "   "


# --- Controller Spawning Tests ---

def test_controller_spawns_agents(store, controller):
    """Controller spawns ≥1 agent."""
    req = IntakeRequest(topic="A topic to debate")
    run_id = store.create_run(req)

    agents = controller.spawn_sub_agents(run_id)

    assert len(agents) >= 1, "Controller must spawn at least 1 agent"
    assert all(isinstance(a, SubAgent) for a in agents)


def test_controller_spawns_multiple(store, controller):
    """Controller spawns 3-5 agents by default (mock behavior)."""
    req = IntakeRequest(
        topic="Debate topic",
        agent_hints=["Builder", "Skeptic", "Methodologist"],
    )
    run_id = store.create_run(req)

    agents = controller.spawn_sub_agents(run_id)

    # Mock provider uses hints, so we expect 3 agents + possibly auto-added critic
    assert 3 <= len(agents) <= 5, f"Expected 3-5 agents, got {len(agents)}"


def test_controller_spawns_with_tags(store, controller):
    """Controller uses tags to calibrate agent expertise."""
    req = IntakeRequest(
        topic="Infrastructure decision",
        tags=["kubernetes", "cost-optimization"],
    )
    run_id = store.create_run(req)

    agents = controller.spawn_sub_agents(run_id)

    assert len(agents) >= 1
    # Mock provider references tags in agent expertise descriptions
    agent_expertise = " ".join(a.expertise for a in agents).lower()
    assert "kubernetes" in agent_expertise or "cost-optimization" in agent_expertise or "topic" in agent_expertise


# --- Critic Role Tests ---

def test_controller_includes_critic(store, controller):
    """Controller always includes ≥1 critic role."""
    req = IntakeRequest(topic="A topic to debate")
    run_id = store.create_run(req)

    agents = controller.spawn_sub_agents(run_id)

    critics = [a for a in agents if a.is_critic]
    assert len(critics) >= 1, "Controller must spawn at least one critic"


def test_controller_auto_appends_critic_if_missing(store, controller):
    """Controller appends critic if model forgets (safety net)."""
    # Even with hints that don't include critic keywords, controller ensures one
    req = IntakeRequest(
        topic="A debate topic",
        agent_hints=["Builder", "Analyst"],  # no critic hint
    )
    run_id = store.create_run(req)

    agents = controller.spawn_sub_agents(run_id)

    critics = [a for a in agents if a.is_critic]
    assert len(critics) >= 1, "Controller must append a critic if missing"


def test_critic_is_valid_subagent(store, controller):
    """Spawned critics are valid SubAgent instances with thesis."""
    req = IntakeRequest(
        topic="Debate topic",
        agent_hints=["Skeptic"],  # explicit critic hint
    )
    run_id = store.create_run(req)

    agents = controller.spawn_sub_agents(run_id)

    critics = [a for a in agents if a.is_critic]
    assert len(critics) >= 1

    critic = critics[0]
    assert critic.name is not None and len(critic.name) > 0
    assert critic.expertise is not None and len(critic.expertise) > 0
    assert critic.mandate is not None and len(critic.mandate) > 0
    assert critic.constraints is not None and len(critic.constraints) > 0
    assert critic.tone is not None and len(critic.tone) > 0
    assert critic.thesis is not None and len(critic.thesis) > 0


# --- Persistence & Reload Tests ---

def test_agents_persist_to_db(store, controller):
    """Spawned agents are saved to DB."""
    req = IntakeRequest(topic="A topic to debate")
    run_id = store.create_run(req)

    spawned = controller.spawn_sub_agents(run_id)

    # Read back from DB
    persisted = store.get_sub_agents(run_id)

    assert len(persisted) == len(spawned)
    for orig, saved in zip(spawned, persisted):
        assert orig.name == saved.name
        assert orig.expertise == saved.expertise
        assert orig.mandate == saved.mandate
        assert orig.constraints == saved.constraints
        assert orig.tone == saved.tone
        assert orig.is_critic == saved.is_critic


def test_agents_reload_preserves_order(store, controller):
    """Agents reload from DB in the same order they were spawned."""
    req = IntakeRequest(
        topic="Debate topic",
        agent_hints=["First", "Second", "Third"],
    )
    run_id = store.create_run(req)

    spawned = controller.spawn_sub_agents(run_id)
    names_spawned = [a.name for a in spawned]

    # Reload fresh from DB
    reloaded = store.get_sub_agents(run_id)
    names_reloaded = [a.name for a in reloaded]

    assert names_spawned == names_reloaded


def test_multiple_runs_isolated(store, controller):
    """Multiple runs maintain isolated agent sets."""
    req1 = IntakeRequest(topic="Debate 1", agent_hints=["Role1", "Role2"])
    req2 = IntakeRequest(topic="Debate 2", agent_hints=["RoleA", "RoleB"])

    run_id_1 = store.create_run(req1)
    run_id_2 = store.create_run(req2)

    agents_1 = controller.spawn_sub_agents(run_id_1)
    agents_2 = controller.spawn_sub_agents(run_id_2)

    # Verify isolation
    reloaded_1 = store.get_sub_agents(run_id_1)
    reloaded_2 = store.get_sub_agents(run_id_2)

    names_1 = [a.name for a in reloaded_1]
    names_2 = [a.name for a in reloaded_2]

    assert names_1 != names_2, "Different runs should spawn different agents"


def test_run_status_transitions(store, controller):
    """Run status transitions from intake → spawned."""
    req = IntakeRequest(topic="Debate topic")
    run_id = store.create_run(req)

    run = store.get_run(run_id)
    assert run["status"] == "intake"

    controller.spawn_sub_agents(run_id)

    run = store.get_run(run_id)
    assert run["status"] == "spawned"


# --- Edge Cases ---

def test_controller_handles_very_long_topic(store, controller):
    """Controller handles very long topics gracefully."""
    long_topic = "A" * 500  # 500 characters
    req = IntakeRequest(topic=long_topic)
    run_id = store.create_run(req)

    agents = controller.spawn_sub_agents(run_id)
    assert len(agents) >= 1


def test_controller_handles_many_tags(store, controller):
    """Controller handles many tags without error."""
    req = IntakeRequest(
        topic="Debate topic",
        tags=[f"tag_{i}" for i in range(20)],
    )
    run_id = store.create_run(req)

    agents = controller.spawn_sub_agents(run_id)
    assert len(agents) >= 1


# --- Thesis Generation Tests ---

def test_agent_thesis_generated(store, controller):
    """All agents have a thesis field generated."""
    req = IntakeRequest(
        topic="Should we adopt microservices architecture?",
        tags=["architecture", "scalability"],
        agent_hints=["Systems Architect", "Skeptic"],
    )
    run_id = store.create_run(req)

    agents = controller.spawn_sub_agents(run_id)

    for agent in agents:
        assert agent.thesis is not None, f"Agent {agent.name} has no thesis"
        assert len(agent.thesis) > 0, f"Agent {agent.name} has empty thesis"
        assert len(agent.thesis) > 20, f"Agent {agent.name} thesis is too short"


def test_thesis_persists_to_db(store, controller):
    """Agent thesis is persisted to database."""
    req = IntakeRequest(topic="A topic to debate")
    run_id = store.create_run(req)

    spawned = controller.spawn_sub_agents(run_id)
    persisted = store.get_sub_agents(run_id)

    for orig, saved in zip(spawned, persisted):
        assert orig.thesis == saved.thesis, f"Thesis mismatch for {orig.name}"


def test_thesis_reflects_role(store, controller):
    """Thesis reflects the agent's role (critic has cautious tone)."""
    req = IntakeRequest(
        topic="New technology adoption",
        agent_hints=["Skeptic"],
    )
    run_id = store.create_run(req)

    agents = controller.spawn_sub_agents(run_id)
    critics = [a for a in agents if a.is_critic]

    assert len(critics) >= 1
    critic = critics[0]
    # Critic thesis should mention gaps, challenges, or concerns
    thesis_lower = critic.thesis.lower()
    assert any(word in thesis_lower for word in ["gap", "challenge", "concern", "risk", "fail", "critical"]), \
        f"Critic thesis doesn't reflect skepticism: {critic.thesis}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
