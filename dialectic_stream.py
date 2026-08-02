"""SSE streaming for the faithful HILCA dialectic flow.

Runs DialecticFlow in a background thread and yields its events to the browser
as they resolve — the engine's emit() callback is the only integration point,
so the flow logic itself stays byte-identical whether streamed or headless.

Two guards live here because this is the real-run entry point:
  - a run only starts from status 'intake' and only once per process (an
    EventSource reconnect or a second tab must NOT relaunch the whole flow:
    that would double the LLM spend and clobber the run's docs/CSV);
  - the mock provider is refused unless HILCA_ALLOW_MOCK=1 (the spec's DO NOT:
    never use the mock provider for a real run — tests/demos opt in explicitly).

Emitted events:
  log                {seq, message}                  — his '====>' trace markers
  context_ready      {chars}
  round_started      {round, phase: main|final}
  agent_spawned      {index, name, role, directive}  — the CCU's dynamic cast
  awaiting_approval  {roster}                        — run held for the operator
  roster_approved    {roster}                        — final (possibly edited) cast
  ccu_message        {round, type, text, tokens}
  agent_message      {round, agent, index, type, text, tokens}
  human_feedback     {round, target, text}
  paused / resumed   {}
  agent_verdict      {round, agent, index, verdict: agree|disagree|unclear}
  synthesis          {text, tokens}
  deliverable        {format_ok, emailed, file}
  done               {agents, rounds, calls, ceiling, tokens, budget,
                      docs_folder, verdicts, deliverable, emailed}
  error              {message}
"""
from __future__ import annotations

import json
import os
import queue
import threading

from db import Store
from dialectic import DialecticFlow, FINAL_ROUNDS, MAIN_ROUNDS
from llm import LLMClient

_inflight_lock = threading.Lock()
_inflight: set[str] = set()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def dialectic_event_stream(run_id: str, store: Store, resume: bool = False):
    """Yield SSE-formatted events for a full faithful dialectic run.

    `resume=True` continues an interrupted run from its last round-boundary
    checkpoint instead of refusing (master file v2: stateful checkpointing) —
    allowed only when a checkpoint exists and the run is not mid-flight."""
    llm = LLMClient()
    if llm.provider == "mock" and os.getenv("HILCA_ALLOW_MOCK") != "1":
        yield _sse("error", {"message": (
            "LLM provider is 'mock' — real runs require LLM_PROVIDER=openai or "
            "anthropic (plus its API key) in .env. Set HILCA_ALLOW_MOCK=1 only "
            "for offline demos/tests."
        )})
        return

    run = store.get_run(run_id)
    status = run["status"] if run else None
    with _inflight_lock:
        if resume:
            if run_id in _inflight:
                yield _sse("error", {"message": f"Run {run_id} is already running; cannot resume it twice."})
                return
            if status == "complete":
                yield _sse("error", {"message": f"Run {run_id} already completed; nothing to resume."})
                return
            if store.get_checkpoint(run_id) is None:
                yield _sse("error", {"message": (
                    f"Run {run_id} has no checkpoint to resume from; only runs that reached "
                    "at least one round boundary can be resumed."
                )})
                return
        elif status != "intake" or run_id in _inflight:
            already = "is already running" if (run_id in _inflight or status in ("gathering_context", "dialectic")) \
                else f"has already been processed (status: {status})"
            yield _sse("error", {"message": (
                f"Run {run_id} {already}; a stream cannot restart it. "
                "Its transcript is available at /api/runs/" + run_id +
                " — or reconnect with ?resume=1 if it was interrupted."
            )})
            return
        _inflight.add(run_id)

    q: "queue.Queue" = queue.Queue()

    def emit(event: str, data: dict) -> None:
        q.put((event, data))

    # Web runs wait for cast approval by default; HILCA_REQUIRE_APPROVAL=0
    # restores the straight-through behavior (headless/API use).
    require_approval = os.getenv("HILCA_REQUIRE_APPROVAL", "1") == "1"

    def worker() -> None:
        try:
            # Per-run round caps (Shahab's phase-2 loop cap): the intake's
            # choice — possibly adjusted again at cast approval — wins over
            # the env defaults. A resumed run restores its caps from the
            # checkpoint, and its cast is already approved.
            result = DialecticFlow(
                store, llm=llm, emit=emit,
                require_approval=require_approval and not resume,
                main_rounds=run.get("main_rounds") or MAIN_ROUNDS,
                final_rounds=run["final_rounds"] if run.get("final_rounds") is not None else FINAL_ROUNDS,
            ).run(run_id, resume=resume)
            emit("done", {
                "agents": result["agents"],
                "rounds": result["rounds_completed"],
                "main_rounds": result["main_rounds"],
                "final_rounds": result["final_rounds"],
                "calls": result["llm_calls"],
                "ceiling": result["call_ceiling"],
                "tokens": result["tokens_used"],
                "budget": result["token_budget"],
                "docs_folder": result["docs_folder"],
                "verdicts": result["verdicts"],
                "deliverable": bool(result["deliverable_path"]),
                "emailed": result["deliverable_emailed"],
                "log_summary": result.get("log_summary"),
            })
        except Exception as exc:  # surfaces guard aborts and parse failures honestly
            emit("error", {"message": str(exc)})
        finally:
            with _inflight_lock:
                _inflight.discard(run_id)
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    yield ": connected\n\n"
    while True:
        item = q.get()
        if item is None:
            break
        event, data = item
        yield _sse(event, data)
