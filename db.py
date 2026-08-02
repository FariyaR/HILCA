"""Persistence for HILCA Milestone 1.

Replaces the original prototype's habit of dumping everything into Google
Docs/Sheets with a real, queryable store. SQLite keeps M1 dependency-free and
file-based; the same interface swaps to Postgres later by changing only this
module.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from schemas import IntakeRequest, SubAgent


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    topic         TEXT NOT NULL,
    tags          TEXT NOT NULL,          -- JSON array
    agent_hints   TEXT NOT NULL,          -- JSON array
    evidence_urls TEXT NOT NULL,          -- JSON array
    email         TEXT,
    status        TEXT NOT NULL,          -- intake | spawned | error
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sub_agents (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    position   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    expertise  TEXT NOT NULL,
    mandate    TEXT NOT NULL,
    constraints TEXT NOT NULL,
    tone       TEXT NOT NULL,
    is_critic  INTEGER NOT NULL,
    thesis     TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS debate_rounds (
    id        TEXT PRIMARY KEY,
    run_id    TEXT NOT NULL,
    round_num INTEGER NOT NULL,
    status    TEXT NOT NULL,          -- active | complete
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    round_num  INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    message    TEXT NOT NULL,
    message_type TEXT NOT NULL,      -- thesis | response | critique | synthesis
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS debate_synthesis (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL UNIQUE,
    synthesis  TEXT NOT NULL,
    consensus_level REAL,             -- 0.0 to 1.0
    converged_at INTEGER,             -- round number
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

-- Faithful UiPath-reproduction flow: the CCU's dynamic dialectic cast
-- ({name, role, directive} per agent, size decided by the CCU each run).
CREATE TABLE IF NOT EXISTS dialectic_roster (
    id        TEXT PRIMARY KEY,
    run_id    TEXT NOT NULL,
    position  INTEGER NOT NULL,
    name      TEXT NOT NULL,
    role      TEXT NOT NULL,
    directive TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

-- The original workflow's '====>' trace-log markers, kept as ordered rows so
-- the exact step sequence of a run is reconstructable.
CREATE TABLE IF NOT EXISTS run_logs (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

-- Human-in-the-loop interventions: feedback the supervising user addresses to
-- the CCU or to a specific sub-agent mid-run. Each note is injected once into
-- its target's next prompt (consumed_at marks the hand-off).
CREATE TABLE IF NOT EXISTS interventions (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    target      TEXT NOT NULL,           -- 'CCU' or an agent name from the roster
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    consumed_at TEXT,                    -- NULL until injected into a prompt
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

-- Granular debate state (master file v2, batch 3): per round and agent, the
-- machine-readable STANCE line (revised position) and CONVERGENCE vote
-- (0.0-1.0 confidence the board is near agreement). Powers the
-- Critique-Refine-Vote early exit and the stance-stabilization check.
CREATE TABLE IF NOT EXISTS debate_state (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    round_num   INTEGER NOT NULL,
    agent_name  TEXT NOT NULL,
    stance      TEXT,
    convergence REAL,                    -- NULL when the agent omitted the vote
    created_at  TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

-- Round-boundary checkpoints (master file v2, batch 2: stateful checkpointing
-- for replayability). One JSON snapshot per run, overwritten at each boundary;
-- a crashed run resumes from the last completed round instead of restarting.
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id     TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,            -- JSON snapshot of the engine state
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str = "hilca.db"):
        self.path = path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Round-cap columns (Shahab's phase-2 loop cap: user picks the number
            # of debate rounds per run). ALTER-if-missing keeps existing DBs valid.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
            if "main_rounds" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN main_rounds INTEGER")
            if "final_rounds" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN final_rounds INTEGER")
            # Role-card extras (master file v2 identity contracts): persona and
            # rubric ride along with name/role/directive on the roster.
            rcols = {r["name"] for r in conn.execute("PRAGMA table_info(dialectic_roster)")}
            if "persona" not in rcols:
                conn.execute("ALTER TABLE dialectic_roster ADD COLUMN persona TEXT NOT NULL DEFAULT ''")
            if "rubric" not in rcols:
                conn.execute("ALTER TABLE dialectic_roster ADD COLUMN rubric TEXT NOT NULL DEFAULT ''")

    # --- runs ---------------------------------------------------------------
    def create_run(self, intake: IntakeRequest) -> str:
        run_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO runs (id, topic, tags, agent_hints, evidence_urls, email, status, created_at,"
                " main_rounds, final_rounds)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    intake.topic,
                    json.dumps(intake.tags),
                    json.dumps(intake.agent_hints),
                    json.dumps(intake.evidence_urls),
                    intake.email,
                    "intake",
                    _now(),
                    intake.main_rounds,
                    intake.final_rounds,
                ),
            )
        return run_id

    def set_status(self, run_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))

    def set_rounds(self, run_id: str, main_rounds: Optional[int] = None,
                   final_rounds: Optional[int] = None) -> None:
        """Update the run's round caps (only the provided values change)."""
        with self._conn() as conn:
            if main_rounds is not None:
                conn.execute("UPDATE runs SET main_rounds = ? WHERE id = ?", (main_rounds, run_id))
            if final_rounds is not None:
                conn.execute("UPDATE runs SET final_rounds = ? WHERE id = ?", (final_rounds, run_id))

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("tags", "agent_hints", "evidence_urls"):
            d[k] = json.loads(d[k])
        return d

    # --- sub-agents ---------------------------------------------------------
    def save_sub_agents(self, run_id: str, agents: List[SubAgent]) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM sub_agents WHERE run_id = ?", (run_id,))
            for i, a in enumerate(agents):
                conn.execute(
                    "INSERT INTO sub_agents (id, run_id, position, name, expertise, mandate, constraints, tone, is_critic, thesis)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), run_id, i, a.name, a.expertise, a.mandate,
                     a.constraints, a.tone, int(a.is_critic), a.thesis),
                )

    def get_sub_agents(self, run_id: str) -> List[SubAgent]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sub_agents WHERE run_id = ? ORDER BY position", (run_id,)
            ).fetchall()
        return [
            SubAgent(
                name=r["name"], expertise=r["expertise"], mandate=r["mandate"],
                constraints=r["constraints"], tone=r["tone"], is_critic=bool(r["is_critic"]),
                thesis=r["thesis"],
            )
            for r in rows
        ]

    # --- debate rounds (M2) -------------------------------------------------
    def start_round(self, run_id: str, round_num: int) -> str:
        """Begin a new debate round."""
        round_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO debate_rounds (id, run_id, round_num, status, started_at)"
                " VALUES (?,?,?,?,?)",
                (round_id, run_id, round_num, "active", _now()),
            )
        return round_id

    def complete_round(self, round_id: str) -> None:
        """Mark round as complete."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE debate_rounds SET status = ?, completed_at = ? WHERE id = ?",
                ("complete", _now(), round_id),
            )

    def get_current_round(self, run_id: str) -> Optional[dict]:
        """Get the active debate round."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM debate_rounds WHERE run_id = ? AND status = ? ORDER BY round_num DESC LIMIT 1",
                (run_id, "active"),
            ).fetchone()
        return dict(row) if row else None

    # --- agent messages (M2) ------------------------------------------------
    def add_message(self, run_id: str, round_num: int, agent_name: str, message: str, msg_type: str = "response") -> str:
        """Store an agent message in the debate."""
        msg_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO agent_messages (id, run_id, round_num, agent_name, message, message_type, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (msg_id, run_id, round_num, agent_name, message, msg_type, _now()),
            )
        return msg_id

    def get_messages(self, run_id: str, round_num: Optional[int] = None) -> List[dict]:
        """Get all messages, optionally for a specific round."""
        with self._conn() as conn:
            if round_num is not None:
                rows = conn.execute(
                    "SELECT * FROM agent_messages WHERE run_id = ? AND round_num = ? ORDER BY created_at",
                    (run_id, round_num),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM agent_messages WHERE run_id = ? ORDER BY round_num, created_at",
                    (run_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    # --- synthesis (M2) -------------------------------------------------------
    def save_synthesis(self, run_id: str, synthesis: str, consensus_level: float, converged_at: int) -> None:
        """Store the final debate synthesis."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO debate_synthesis (id, run_id, synthesis, consensus_level, converged_at, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, synthesis, consensus_level, converged_at, _now()),
            )

    def get_synthesis(self, run_id: str) -> Optional[dict]:
        """Retrieve the debate synthesis."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM debate_synthesis WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    # --- faithful-flow roster (the CCU's five role cards) ---------------------
    def save_roster(self, run_id: str, roster: List[dict]) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM dialectic_roster WHERE run_id = ?", (run_id,))
            for i, a in enumerate(roster):
                conn.execute(
                    "INSERT INTO dialectic_roster (id, run_id, position, name, role, directive, persona, rubric)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), run_id, i, a["name"], a["role"], a["directive"],
                     a.get("persona", ""), a.get("rubric", "")),
                )

    def get_roster(self, run_id: str) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, role, directive, persona, rubric FROM dialectic_roster"
                " WHERE run_id = ? ORDER BY position",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- granular debate state (STANCE + CONVERGENCE per agent per round) ------
    def add_debate_state(self, run_id: str, round_num: int, agent_name: str,
                         stance: Optional[str], convergence: Optional[float]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO debate_state (id, run_id, round_num, agent_name, stance, convergence, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, round_num, agent_name, stance, convergence, _now()),
            )

    def get_debate_state(self, run_id: str, round_num: Optional[int] = None) -> List[dict]:
        with self._conn() as conn:
            if round_num is not None:
                rows = conn.execute(
                    "SELECT round_num, agent_name, stance, convergence FROM debate_state"
                    " WHERE run_id = ? AND round_num = ? ORDER BY created_at",
                    (run_id, round_num),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT round_num, agent_name, stance, convergence FROM debate_state"
                    " WHERE run_id = ? ORDER BY round_num, created_at",
                    (run_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    # --- round-boundary checkpoints (resume/replay) ----------------------------
    def save_checkpoint(self, run_id: str, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO checkpoints (run_id, payload, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
                (run_id, json.dumps(payload), _now()),
            )

    def get_checkpoint(self, run_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT payload FROM checkpoints WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def clear_checkpoint(self, run_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM checkpoints WHERE run_id = ?", (run_id,))

    # --- faithful-flow step logs (the original's '====>' markers) -------------
    def add_log(self, run_id: str, seq: int, message: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO run_logs (id, run_id, seq, message, created_at) VALUES (?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, seq, message, _now()),
            )

    def get_logs(self, run_id: str) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT seq, message, created_at FROM run_logs WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- human-in-the-loop interventions ---------------------------------------
    def add_intervention(self, run_id: str, target: str, message: str) -> str:
        iid = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO interventions (id, run_id, target, message, created_at) VALUES (?,?,?,?,?)",
                (iid, run_id, target, message, _now()),
            )
        return iid

    def take_interventions(self, run_id: str, target: str) -> List[dict]:
        """Return the target's unconsumed feedback notes and mark them consumed."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, message, created_at FROM interventions"
                " WHERE run_id = ? AND target = ? AND consumed_at IS NULL ORDER BY created_at",
                (run_id, target),
            ).fetchall()
            for r in rows:
                conn.execute("UPDATE interventions SET consumed_at = ? WHERE id = ?", (_now(), r["id"]))
        return [dict(r) for r in rows]

    def get_interventions(self, run_id: str) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT target, message, created_at, consumed_at FROM interventions"
                " WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]
