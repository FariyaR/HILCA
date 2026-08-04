"""SSE streaming for the faithful HILCA dialectic flow.

Runs DialecticFlow in a background thread and yields its events to the browser
as they resolve — the engine's emit() callback is the only integration point,
so the flow logic itself stays byte-identical whether streamed or headless.

The engine's lifecycle is DECOUPLED from any one connection: each run gets an
in-process event hub (full history + live subscribers). Every event carries an
SSE `id:`, so a browser that loses the connection (mobile screen lock, network
blip) auto-reconnects with Last-Event-ID and replays only what it missed; a
fresh page (or second tab) attaches from the start and catches up. Attaching
NEVER relaunches the engine — the double-spend guard of old, now without
locking the viewer out.

Guards (this is the real-run entry point):
  - a run only STARTS from status 'intake', once per process; anything else
    attaches to the live hub, or is refused when there is nothing to attach to
    (?resume=1 restarts an interrupted run from its checkpoint after a process
    restart — completed rounds are never re-billed);
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
  deliverable        {format_ok, emailed, file, transcript_file}
  done               {agents, rounds, calls, ceiling, tokens, budget,
                      docs_folder, verdicts, deliverable, emailed}
  error              {message}
"""
from __future__ import annotations

import json
import os
import queue
import threading
from typing import List, Optional, Tuple

from db import Store
from dialectic import DialecticFlow, FINAL_ROUNDS, MAIN_ROUNDS
from llm import LLMClient

# Seconds between keepalive comments on a quiet stream. LLM calls (plus TPM
# pacing sleeps) can go minutes without an event; proxies kill idle responses.
HEARTBEAT_SECS = 15

# Completed hubs kept around so late viewers can still replay; oldest done
# hubs are pruned past this count (a process restart clears them anyway).
MAX_HUBS = 40

_Event = Tuple[int, str, dict]


class _RunHub:
    """One run's event history plus its live subscribers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._history: List[_Event] = []
        self._subscribers: List["queue.Queue[_Event]"] = []
        self.done = False

    def publish(self, event: str, data: dict) -> None:
        with self._lock:
            seq = len(self._history) + 1
            item = (seq, event, data)
            self._history.append(item)
            if event in ("done", "error"):
                self.done = True
            subs = list(self._subscribers)
        for q in subs:
            q.put(item)

    def subscribe(self, after_seq: int):
        """Missed history after `after_seq` plus, for live runs, a queue that
        receives everything published from this moment on (no gap: the
        snapshot and the subscription happen under one lock)."""
        q: "queue.Queue[_Event]" = queue.Queue()
        with self._lock:
            missed = [e for e in self._history if e[0] > after_seq]
            done = self.done
            if not done:
                self._subscribers.append(q)
        return q, missed, done

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


_hubs_lock = threading.Lock()
_hubs: dict[str, _RunHub] = {}


def run_is_live(run_id: str) -> bool:
    """True while this process's engine is executing the run — the signal the
    frontend uses to attach as a viewer instead of replaying REST history."""
    with _hubs_lock:
        hub = _hubs.get(run_id)
    return bool(hub and not hub.done)


def _prune_hubs_locked() -> None:
    if len(_hubs) <= MAX_HUBS:
        return
    for rid in [r for r, h in _hubs.items() if h.done]:
        del _hubs[rid]
        if len(_hubs) <= MAX_HUBS:
            break


def _sse(event: str, data: dict, seq: Optional[int] = None) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n"


def dialectic_event_stream(run_id: str, store: Store, resume: bool = False,
                           last_event_id: int = 0):
    """Yield SSE-formatted events for a faithful dialectic run.

    Starts the engine when the run is fresh (or `resume=True` continues an
    interrupted run from its checkpoint); otherwise attaches to the already-
    running (or just-finished) hub, replaying everything after
    `last_event_id`. Disconnecting a viewer never stops the engine."""
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

    # Guard decisions happen under the lock; the error frame is yielded AFTER
    # releasing it — a yield suspends the generator, and a slow client must
    # never suspend it while holding the lock every other stream needs.
    err: Optional[str] = None
    with _hubs_lock:
        hub = _hubs.get(run_id)
        start_engine = hub is None
        if start_engine:
            # No hub in this process — decide whether we may launch the engine.
            if resume:
                if status == "complete":
                    err = f"Run {run_id} already completed; nothing to resume."
                elif store.get_checkpoint(run_id) is None:
                    err = (f"Run {run_id} has no checkpoint to resume from; only runs that reached "
                           "at least one round boundary can be resumed.")
            elif status != "intake":
                already = "is already running" if status in ("gathering_context", "dialectic") \
                    else f"has already been processed (status: {status})"
                err = (f"Run {run_id} {already}; a stream cannot restart it. "
                       "Its transcript is available at /api/runs/" + run_id +
                       " — or reconnect with ?resume=1 if it was interrupted.")
            if err is None:
                hub = _RunHub()
                _hubs[run_id] = hub
                _prune_hubs_locked()

    if err is not None:
        yield _sse("error", {"message": err})
        return

    if start_engine:
        _launch_engine(run_id, run, store, llm, hub, resume)

    q, missed, hub_done = hub.subscribe(last_event_id)
    try:
        yield ": connected\n\n"
        for seq, event, data in missed:
            yield _sse(event, data, seq)
            if event in ("done", "error"):
                return
        if hub_done:
            return  # finished hub with nothing terminal after last_event_id
        while True:
            try:
                seq, event, data = q.get(timeout=HEARTBEAT_SECS)
            except queue.Empty:
                yield ": ping\n\n"  # keepalive — proxies drop idle streams
                continue
            yield _sse(event, data, seq)
            if event in ("done", "error"):
                return
    finally:
        hub.unsubscribe(q)


def _launch_engine(run_id: str, run: dict, store: Store, llm: LLMClient,
                   hub: _RunHub, resume: bool) -> None:
    """Start the DialecticFlow worker thread publishing into the hub."""
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
                store, llm=llm, emit=hub.publish,
                require_approval=require_approval and not resume,
                main_rounds=run.get("main_rounds") or MAIN_ROUNDS,
                final_rounds=run["final_rounds"] if run.get("final_rounds") is not None else FINAL_ROUNDS,
            ).run(run_id, resume=resume)
            hub.publish("done", {
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
            hub.publish("error", {"message": str(exc)})

    threading.Thread(target=worker, daemon=True).start()
