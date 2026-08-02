"""Frontend streaming layer for the HILCA dialectic engine.

Observes a REAL debate run and emits SSE events as each real round / message /
verdict resolves. It does NOT modify the debate logic, the referee, or any safety
guard: it composes thin wrappers around the Store and LLMClient that the existing
DebateLoop drives, so the run behaves byte-for-byte identically — the wrappers
only listen.

Emitted events: agent_spawned, round_started, agent_message, round_verdict,
converged, done (and error / aborted on failure).
"""
from __future__ import annotations

import json
import queue
import threading
from typing import Callable

from db import Store
from llm import LLMClient
from controller import MainController
from debate import DebateLoop, _verdict_developed


class _EmittingStore:
    """Delegates to a real Store; emits UI events on the calls that matter."""

    def __init__(self, store: Store, emit: Callable, ctx: dict):
        self._store = store
        self._emit = emit
        self._ctx = ctx

    def start_round(self, run_id: str, round_num: int) -> str:
        self._ctx["round"] = round_num
        round_id = self._store.start_round(run_id, round_num)
        self._emit("round_started", {"round": round_num})
        return round_id

    def add_message(self, run_id, round_num, agent_name, message, msg_type="response") -> str:
        msg_id = self._store.add_message(run_id, round_num, agent_name, message, msg_type)
        self._emit("agent_message", {
            "round": round_num,
            "agent": agent_name,
            "type": msg_type,
            "text": message,
            "tokens": self._ctx.get("tokens", 0),
        })
        return msg_id

    def __getattr__(self, name):
        # Everything else (get_run, get_sub_agents, complete_round, set_status,
        # get_messages, ...) delegates unchanged to the real store.
        return getattr(self._store, name)


class _EmittingLLM:
    """Delegates to a real LLMClient; captures referee verdicts + real token usage.

    During a debate run, complete() is only called for agent turns and
    complete_json() only by the referee, so each complete_json is exactly one
    round verdict. last_usage is proxied for read AND write so the debate's own
    token guard keeps resetting/reading it precisely as before.
    """

    def __init__(self, llm: LLMClient, emit: Callable, ctx: dict):
        self._llm = llm
        self._emit = emit
        self._ctx = ctx

    @property
    def last_usage(self):
        return self._llm.last_usage

    @last_usage.setter
    def last_usage(self, value):
        self._llm.last_usage = value

    def complete(self, system: str, user: str, max_tokens: int = 1000) -> str:
        out = self._llm.complete(system, user, max_tokens=max_tokens)
        self._ctx["tokens"] = self._ctx.get("tokens", 0) + int(self._llm.last_usage.get("total_tokens", 0))
        return out

    def complete_json(self, system: str, user: str, max_tokens=None) -> dict:
        res = self._llm.complete_json(system, user, max_tokens=max_tokens)
        self._ctx["tokens"] = self._ctx.get("tokens", 0) + int(self._llm.last_usage.get("total_tokens", 0))
        verdicts = res.get("verdicts", []) if isinstance(res, dict) else []
        if isinstance(verdicts, list) and verdicts:
            total = self._ctx.get("num_agents", len(verdicts))
            developed = sum(1 for v in verdicts if isinstance(v, dict) and _verdict_developed(v))
            self._emit("round_verdict", {
                "round": self._ctx.get("round"),
                "developed": developed,
                "total": total,
                "verdict": "developed" if developed * 2 > total else "stalled",
                "tokens": self._ctx.get("tokens", 0),
            })
        return res

    def __getattr__(self, name):
        return getattr(self._llm, name)


def debate_event_stream(run_id: str, store: Store):
    """Yield SSE-formatted events for a full spawn + debate lifecycle on run_id.

    The debate runs in a background thread; events are pushed onto a queue and
    yielded to the client as they resolve, at the real cadence of the LLM calls.
    """
    q: "queue.Queue" = queue.Queue()

    def emit(event: str, data: dict) -> None:
        q.put((event, data))

    def worker() -> None:
        ctx = {"tokens": 0, "round": None, "num_agents": 0}
        try:
            # --- M1 spawn (real controller / real LLM), announced per agent ---
            agents = MainController(store).spawn_sub_agents(run_id)
            ctx["num_agents"] = len(agents)
            for a in agents:
                emit("agent_spawned", {
                    "name": a.name,
                    "expertise": a.expertise,
                    "mandate": a.mandate,
                    "tone": a.tone,
                    "is_critic": a.is_critic,
                    "thesis": a.thesis,
                })

            # --- M2 debate through the listening wrappers (logic untouched) ---
            em_store = _EmittingStore(store, emit, ctx)
            em_llm = _EmittingLLM(LLMClient(), emit, ctx)
            result = DebateLoop(em_store, em_llm).run(run_id)

            if result["stop_reason"] == "converged":
                emit("converged", {"stop_reason": "converged"})
            emit("done", {
                "rounds": result["rounds_completed"],
                "stop_reason": result["stop_reason"],
                "calls": result["llm_calls"],
                "tokens": result["tokens_used"],
                "budget": result["token_budget"],
            })
        except Exception as exc:  # surfaces guard aborts and spawn/parse failures honestly
            emit("error", {"message": str(exc)})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    # Prime the connection so the browser opens the stream immediately.
    yield ": connected\n\n"
    while True:
        item = q.get()
        if item is None:
            break
        event, data = item
        yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
