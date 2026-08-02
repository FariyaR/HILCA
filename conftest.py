"""Shared test environment.

Keeps test runs out of the live artifacts: every module that reads these env
vars (web.py, api.py, dialectic.py) gets a temp database / docs root instead of
hilca_live.db and runs/. Set BEFORE any app module is imported; load_dotenv()
does not override variables that are already set.
"""
import os
import tempfile

os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "hilca_test.db"))
os.environ.setdefault("HILCA_DOCS_ROOT", os.path.join(tempfile.gettempdir(), "hilca_test_runs"))
