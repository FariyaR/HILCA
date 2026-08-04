"""HILCA Live — the wired frontend server.

Serves the single-page app and streams the FAITHFUL dialectic flow: the 1:1
port of Shahab's UiPath workflow (CCU cast selection -> opening theses -> main
round loop -> final conclusive rounds -> P8 synthesis), with the verbatim
prompts. See dialectic.py for the engine and prompts.py for P1-P8.

    uvicorn web:app --reload      # then open http://localhost:8000
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import controls
from db import Store
from dialectic import MAX_AGENTS, TOKEN_BUDGET, render_transcript
from schemas import IntakeRequest
from dialectic_stream import dialectic_event_stream, run_is_live

FRONTEND = Path(__file__).parent / "frontend"

app = FastAPI(title="HILCA Live")
store = Store(os.getenv("DB_PATH", "hilca_live.db"))

app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (FRONTEND / "index.html").read_text(encoding="utf-8")


class IntakeBody(BaseModel):
    topic: str = Field(min_length=1)  # match IntakeRequest so bad input 422s here, not 500s later
    tags: list[str] = []
    agent_hints: list[str] = []
    evidence_urls: list[str] = []
    email: str | None = None
    # Round cap (Shahab's phase-2 spec): up to 100, recommended 15-20.
    main_rounds: int | None = Field(default=None, ge=1, le=100)
    final_rounds: int | None = Field(default=None, ge=0, le=10)


@app.post("/api/runs")
def create_run(body: IntakeBody) -> JSONResponse:
    """Intake: store the run row, return its Run_ID. The dialectic runs on the stream."""
    req = IntakeRequest(
        topic=body.topic,
        tags=body.tags,
        agent_hints=body.agent_hints,
        evidence_urls=body.evidence_urls,
        email=body.email,
        main_rounds=body.main_rounds,
        final_rounds=body.final_rounds,
    )
    run_id = store.create_run(req)
    return JSONResponse({"run_id": run_id, "token_budget": TOKEN_BUDGET})


@app.get("/api/runs/{run_id}/stream")
def stream(run_id: str, request: Request, resume: bool = False, since: int = 0) -> StreamingResponse:
    """SSE: run the faithful dialectic flow, emitting every step as it resolves.

    A stream for a run that is already executing ATTACHES as a viewer (full
    replay, then live). Reconnecting browsers replay only what they missed:
    native EventSource retries send Last-Event-ID; manually recreated
    connections (after a fatal proxy close) pass ?since=<seq> instead.
    `?resume=1` restarts an interrupted run from its last round-boundary
    checkpoint (master file v2: stateful checkpointing) after a process
    restart — completed rounds are never re-billed."""
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        last_event_id = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        last_event_id = 0
    last_event_id = max(last_event_id, since)
    return StreamingResponse(
        dialectic_event_stream(run_id, store, resume=resume, last_event_id=last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/runs/{run_id}")
def run_json(run_id: str) -> JSONResponse:
    """Full run record: roster, ordered transcript, step logs, final synthesis."""
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run["roster"] = store.get_roster(run_id)
    run["messages"] = store.get_messages(run_id)
    run["logs"] = store.get_logs(run_id)
    run["interventions"] = store.get_interventions(run_id)
    # True while this process's engine is executing the run: the frontend
    # attaches to the live stream (full replay) instead of REST reconstruction.
    run["live"] = run_is_live(run_id)
    return JSONResponse(run)


# --- human-in-the-loop -------------------------------------------------------
class FeedbackBody(BaseModel):
    target: str = Field(min_length=1)   # 'CCU' or an agent name from the roster
    message: str = Field(min_length=1, max_length=8000)


@app.post("/api/runs/{run_id}/feedback")
def submit_feedback(run_id: str, body: FeedbackBody) -> JSONResponse:
    """Queue operator feedback for the CCU or a specific sub-agent.

    The note is injected, clearly attributed, into that target's next prompt
    (an agent's next round turn, or the CCU's next agenda/wrap-up/synthesis).
    """
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    target = body.target.strip()
    roster_names = {a["name"] for a in store.get_roster(run_id)}
    if target != "CCU" and roster_names and target not in roster_names:
        raise HTTPException(status_code=400, detail=f"Unknown target {target!r}; use 'CCU' or one of {sorted(roster_names)}")
    if target != "CCU" and not roster_names:
        raise HTTPException(status_code=400, detail="The cast is not defined yet; only 'CCU' can receive feedback before round 1 casting completes")
    store.add_intervention(run_id, target, body.message.strip())
    return JSONResponse({"queued": True, "target": target})


class RosterAgentBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=2000)
    directive: str = Field(min_length=1, max_length=4000)
    persona: str = Field(default="", max_length=2000)
    rubric: str = Field(default="", max_length=2000)


class ApproveBody(BaseModel):
    # Master file v2: the cast is EXACTLY five role cards. The operator may
    # edit any card but not change the count.
    roster: list[RosterAgentBody] = Field(min_length=MAX_AGENTS, max_length=MAX_AGENTS)
    # The operator may (re)choose the round caps while reviewing the cast.
    main_rounds: int | None = Field(default=None, ge=1, le=100)
    final_rounds: int | None = Field(default=None, ge=0, le=10)


@app.post("/api/runs/{run_id}/approve")
def approve_cast(run_id: str, body: ApproveBody) -> JSONResponse:
    """Approve (and optionally edit) the CCU's cast; the held run then proceeds.

    The submitted roster replaces the CCU's — the operator may rename agents or
    rewrite roles/personas/directives (the count stays at five per the master
    file) — and may adjust the debate round caps at the same time.
    """
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] != "awaiting_approval":
        raise HTTPException(status_code=409, detail=f"Run is not awaiting approval (status: {run['status']})")
    roster = [{"name": a.name.strip(), "role": a.role.strip(), "directive": a.directive.strip(),
               "persona": a.persona.strip(), "rubric": a.rubric.strip()}
              for a in body.roster]
    names = [a["name"] for a in roster]
    if len(set(names)) != len(names):
        raise HTTPException(status_code=400, detail="Agent names must be unique")
    store.save_roster(run_id, roster)
    if body.main_rounds is not None or body.final_rounds is not None:
        store.set_rounds(run_id, main_rounds=body.main_rounds, final_rounds=body.final_rounds)
    controls.for_run(run_id).approve()
    return JSONResponse({"approved": True, "agents": len(roster)})


@app.get("/api/runs/{run_id}/deliverable")
def download_deliverable(run_id: str) -> FileResponse:
    """Download the single-document deliverable the CCU composed for this run.

    Shahab's phase-2 note: the user's deliverable is one readable file — this
    endpoint serves it as a markdown download.
    """
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    path = Path(os.getenv("HILCA_DOCS_ROOT", "runs")) / run_id / "deliverable.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No deliverable yet — the run has not completed")
    return FileResponse(
        str(path), media_type="text/markdown",
        filename=f"HILCA_deliverable_{run_id[:8]}.md",
    )


@app.get("/api/runs/{run_id}/transcript")
def download_transcript(run_id: str) -> Response:
    """Download the complete round-by-round transcript for deeper analysis.

    The deliverable stays the one readable document the user is meant to read;
    this is the full record behind it. Completed runs serve the assembled
    transcript.md from the run folder; for older or still-running runs the
    transcript is assembled from the message store on the fly."""
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    filename = f"HILCA_transcript_{run_id[:8]}.md"
    path = Path(os.getenv("HILCA_DOCS_ROOT", "runs")) / run_id / "transcript.md"
    if path.exists():
        return FileResponse(str(path), media_type="text/markdown", filename=filename)
    messages = store.get_messages(run_id)
    if not messages:
        raise HTTPException(status_code=404, detail="No transcript yet — the dialectic has not started")
    return Response(
        content=render_transcript(run_id, run["topic"], messages),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/runs/{run_id}/compact")
def compact_run(run_id: str) -> JSONResponse:
    """Compact the run's conversation context before its next LLM call — a
    Claude-style /compact: every participant thread is replaced by a
    high-density digest and the dialectic continues from it."""
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    controls.for_run(run_id).request_compact()
    return JSONResponse({"compact_requested": True})


@app.post("/api/runs/{run_id}/pause")
def pause_run(run_id: str) -> JSONResponse:
    """Pause the run before its next LLM call (the in-flight call finishes)."""
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    controls.for_run(run_id).pause()
    return JSONResponse({"paused": True})


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str) -> JSONResponse:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    controls.for_run(run_id).resume()
    return JSONResponse({"paused": False})
