"""Startup sweep: stuck runs are failed; an unavailable DB never stops the app from starting."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from reviewer.db.models import Run
from reviewer.db.session import session_scope
from reviewer.main import create_app
from reviewer.services.sweep import REASON, sweep_stuck_runs
from tests.helpers import seed_run


def statuses() -> dict:
    with session_scope() as session:
        return {run.id: (run.status, run.error) for run in session.scalars(select(Run))}


def test_queued_and_running_runs_are_failed_and_finished_ones_untouched(db_tables):
    queued = seed_run(number=1, status="queued")
    running = seed_run(number=2, status="running")
    done = seed_run(number=3, status="succeeded")
    failed = seed_run(number=4, status="failed", error="original error")

    assert sweep_stuck_runs() == 2

    state = statuses()
    assert state[queued] == ("failed", f"{REASON} (status was 'queued')")
    assert state[running] == ("failed", f"{REASON} (status was 'running')")
    assert state[done] == ("succeeded", None)
    assert state[failed] == ("failed", "original error")


def test_sweep_sets_finished_at_and_is_idempotent(db_tables):
    run_id = seed_run(status="running")

    assert sweep_stuck_runs() == 1
    assert sweep_stuck_runs() == 0

    with session_scope() as session:
        run = session.get(Run, run_id)
        assert run.finished_at is not None and run.started_at is not None


def test_sweep_survives_a_database_without_the_schema():
    # No db_tables fixture: the SQLite file exists but has no `runs` table.
    assert sweep_stuck_runs() == 0


def test_sweep_survives_an_unreachable_database(tmp_path, switch_database):
    switch_database(f"sqlite:///{tmp_path / 'no_such_dir' / 'x.db'}")

    assert sweep_stuck_runs() == 0  # logged, not raised


def test_app_starts_and_serves_even_if_the_database_is_unreachable(tmp_path, switch_database):
    switch_database(f"sqlite:///{tmp_path / 'no_such_dir' / 'x.db'}")

    with TestClient(create_app()) as client:  # runs the lifespan, i.e. the sweep
        assert client.get("/health").json() == {"status": "ok"}


def test_sweep_runs_on_app_startup(db_tables):
    stuck = seed_run(status="running")

    with TestClient(create_app()):
        pass

    assert statuses()[stuck][0] == "failed"
    assert "restarted" in statuses()[stuck][1]
