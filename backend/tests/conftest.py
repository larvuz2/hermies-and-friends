"""Test fixtures: give every test an isolated on-disk sqlite DB and a fresh
FastAPI TestClient with startup/shutdown handled.
"""
import os
import sys
import uuid

import pytest

# Make backend/ importable (app.py, db.py, matching.py live one level up).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / f"hermies-{uuid.uuid4().hex}.db"
    monkeypatch.setenv("HERMIES_DB", str(db_file))

    # Import fresh so module-level rate-limit counters reset per test.
    for mod in ("app", "db", "matching"):
        sys.modules.pop(mod, None)
    import app as app_module
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as c:
        c._app_module = app_module
        yield c


def register(client, handle, represents=""):
    r = client.post("/v1/register", json={"handle": handle, "represents": represents})
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


def auth(api_key):
    return {"Authorization": f"Bearer {api_key}"}
