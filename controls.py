"""In-process pause/resume controls for live dialectic runs.

The engine checks its run's control before every LLM call; the web API flips
it. Pausing takes effect at the next call boundary (the current model call is
never interrupted mid-flight), so the choreography and its persistence stay
exactly as the faithful flow defines them.
"""
from __future__ import annotations

import threading
from typing import Dict


class RunControl:
    def __init__(self) -> None:
        self._resume = threading.Event()
        self._resume.set()  # runs start unpaused
        self._approved = threading.Event()
        self._compact = threading.Event()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def wait_until_resumed(self) -> None:
        self._resume.wait()

    # --- context compaction (Claude-style /compact) -------------------------
    def request_compact(self) -> None:
        """Ask the engine to compact its conversation context; honored at the
        next LLM call boundary (never mid-call)."""
        self._compact.set()

    def pop_compact_request(self) -> bool:
        """Consume a pending compaction request, if any."""
        was = self._compact.is_set()
        self._compact.clear()
        return was

    # --- cast approval gate (round 1) ---------------------------------------
    def approve(self) -> None:
        self._approved.set()

    @property
    def approved(self) -> bool:
        return self._approved.is_set()

    def wait_for_approval(self) -> None:
        self._approved.wait()


_lock = threading.Lock()
_controls: Dict[str, RunControl] = {}


def for_run(run_id: str) -> RunControl:
    with _lock:
        if run_id not in _controls:
            _controls[run_id] = RunControl()
        return _controls[run_id]
