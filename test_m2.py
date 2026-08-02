"""Offline M2 loop mechanics (mock provider — no real API calls).

Verifies the round-table loop's structure and all three safety guards without
spending: the real round counter/cap, per-round storage, both stop paths, the
call ceiling, the cumulative token counter, and the token-budget abort.
"""
import os
import tempfile

import pytest

os.environ["LLM_PROVIDER"] = "mock"

from controller import MainController
from db import Store
from llm import get_llm, LLMClient
from schemas import IntakeRequest
from debate import DebateLoop


@pytest.fixture
def temp_db():
    """Temporary DB file per test (mirrors test_m1.py; avoids pytest tmp_path)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    if os.path.exists(db_path):
        for _ in range(3):
            try:
                os.unlink(db_path)
                break
            except PermissionError:
                pass  # SQLite may still hold a lock on Windows; leave for cleanup


def _spawned_run(store: Store) -> str:
    controller = MainController(store)
    req = IntakeRequest(
        topic="A neutral test topic",
        tags=["a", "b"],
        agent_hints=["Builder", "Skeptic"],
        evidence_urls=[],
        email=None,
    )
    run_id = store.create_run(req)
    controller.spawn_sub_agents(run_id)
    return run_id


def test_max_rounds_path(temp_db):
    store = Store(temp_db)
    run_id = _spawned_run(store)
    agents = store.get_sub_agents(run_id)

    loop = DebateLoop(store, get_llm(), max_rounds=3)
    result = loop.run(run_id)

    # Real counter actually advanced to the cap (mock never converges).
    assert result["rounds_completed"] == 3
    assert result["stop_reason"] == "max_rounds"
    assert store.get_run(run_id)["status"] == "max_rounds"

    # Round 1 is the verbatim M1 theses, not regenerated.
    r1 = store.get_messages(run_id, round_num=1)
    assert [m["message_type"] for m in r1] == ["thesis"] * len(agents)
    assert {m["message"] for m in r1} == {a.thesis for a in agents}

    # Rounds 2..3 are one response row per agent.
    for rn in (2, 3):
        rows = store.get_messages(run_id, round_num=rn)
        assert len(rows) == len(agents)
        assert all(m["message_type"] == "response" for m in rows)

    # Call-ceiling guard: calls = agents*(rounds-1) + (rounds-1) convergence checks.
    assert result["llm_calls"] == len(agents) * 2 + 2
    assert result["llm_calls"] <= result["call_ceiling"]

    # Token counter is tracked, positive, and within the (default) budget.
    assert result["tokens_used"] > 0
    assert result["tokens_used"] <= result["token_budget"]
    assert result["token_budget"] == 1_000_000


def test_two_strikes_required_to_stop(temp_db):
    store = Store(temp_db)
    run_id = _spawned_run(store)

    loop = DebateLoop(store, get_llm(), max_rounds=10)
    loop._has_new_arguments = lambda *a, **k: False  # every round: no development

    result = loop.run(run_id)

    # A single 'no development' verdict (after round 2) must NOT stop the debate;
    # two consecutive (after rounds 2 and 3) do -> stops at round 3, not round 2.
    assert result["rounds_completed"] == 3
    assert result["stop_reason"] == "converged"
    assert store.get_run(run_id)["status"] == "converged"


def test_true_verdict_resets_stable_streak(temp_db):
    store = Store(temp_db)
    run_id = _spawned_run(store)

    loop = DebateLoop(store, get_llm(), max_rounds=10)
    # Verdicts evaluated after rounds 2,3,4,5,6:
    #   R2 True (streak->0)  R3 False (1)  R4 True (streak->0)  R5 False (1)  R6 False (2 -> stop)
    verdicts = iter([True, False, True, False, False])
    loop._has_new_arguments = lambda *a, **k: next(verdicts)

    result = loop.run(run_id)

    # The intervening True at round 4 resets the streak, so it takes until round 6.
    assert result["rounds_completed"] == 6
    assert result["stop_reason"] == "converged"
    assert store.get_run(run_id)["status"] == "converged"


def test_referee_requires_majority_developing(temp_db):
    """A round develops only if a strict majority of agents advanced — not just any one."""
    store = Store(temp_db)
    loop = DebateLoop(store, LLMClient())  # fresh client; we stub only its referee call
    names = ["A", "B", "C", "D"]

    def verdicts(flags):
        return {"verdicts": [{"agent": n, "developed": f} for n, f in zip(names, flags)]}

    # 1 of 4 developed -> minority -> NOT developing
    loop.llm.complete_json = lambda *a, **k: verdicts([True, False, False, False])
    assert loop._has_new_arguments("t", "p", "c", names) is False

    # 2 of 4 developed -> a tie is NOT a majority -> NOT developing
    loop.llm.complete_json = lambda *a, **k: verdicts([True, True, False, False])
    assert loop._has_new_arguments("t", "p", "c", names) is False

    # 3 of 4 developed -> majority -> developing
    loop.llm.complete_json = lambda *a, **k: verdicts([True, True, True, False])
    assert loop._has_new_arguments("t", "p", "c", names) is True

    # malformed referee output -> default to developing (keep debating)
    loop.llm.complete_json = lambda *a, **k: {"unexpected": 1}
    assert loop._has_new_arguments("t", "p", "c", names) is True

    # string-encoded booleans are read correctly: 3 "true" of 4 -> majority -> developing
    loop.llm.complete_json = lambda *a, **k: verdicts(["true", "true", "true", "false"])
    assert loop._has_new_arguments("t", "p", "c", names) is True

    # a string "false" must NOT be counted as developed: 1 real true of 4 -> minority -> False
    loop.llm.complete_json = lambda *a, **k: verdicts([True, "false", "false", "false"])
    assert loop._has_new_arguments("t", "p", "c", names) is False


def test_referee_criterion_requires_genuinely_new(temp_db):
    """The referee prompt must demand genuinely NEW content and exclude mere elaboration."""
    store = Store(temp_db)
    loop = DebateLoop(store, LLMClient())
    seen = {}

    def spy(system, user, max_tokens=None):
        seen["user"] = user
        return {"verdicts": [{"agent": "A", "developed": True}]}

    loop.llm.complete_json = spy
    loop._has_new_arguments("topic", "prev", "curr", ["A", "B"])

    prompt = seen["user"].lower()
    assert "genuinely new" in prompt      # development requires new content
    assert "elaborat" in prompt           # elaboration explicitly excluded
    assert "rephras" in prompt            # rephrasing explicitly excluded
    assert "re-frame" in prompt           # re-framing explicitly excluded
    assert "not development" in prompt    # reworded/deeper delivery is NOT development


def test_token_budget_guard(temp_db):
    store = Store(temp_db)
    run_id = _spawned_run(store)

    # Absurdly small, explicitly-configured budget: the first real turn blows it,
    # so the debate aborts with a clear error and the run is marked 'error'.
    loop = DebateLoop(store, get_llm(), max_rounds=5, token_budget=10)
    with pytest.raises(RuntimeError, match="Token budget exceeded"):
        loop.run(run_id)

    assert store.get_run(run_id)["status"] == "error"
