import pytest
from sqlalchemy.exc import IntegrityError

from reviewer.db.repo import RunRepository
from reviewer.db.session import session_scope

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_get_or_create_pull_request_is_idempotent(db_tables):
    with session_scope() as session:
        repo = RunRepository(session)
        first = repo.get_or_create_pull_request("o/r", 1)
        again = repo.get_or_create_pull_request("o/r", 1)
        other = repo.get_or_create_pull_request("o/r", 2)

    assert first.id == again.id
    assert other.id != first.id


def test_create_run_starts_queued_and_finish_run_completes_it(db_tables):
    with session_scope() as session:
        repo = RunRepository(session)
        run = repo.create_run(repo.get_or_create_pull_request("o/r", 1).id, SHA, "d1")
        run_id = run.id
        assert run.status == "queued"
    with session_scope() as session:
        repo = RunRepository(session)
        repo.finish_run(run_id, status="succeeded")
    with session_scope() as session:
        run = RunRepository(session).get_run(run_id)
        assert run.status == "succeeded"
        assert run.finished_at is not None


def test_duplicate_delivery_id_is_rejected_by_the_database(db_tables):
    with pytest.raises(IntegrityError), session_scope() as session:
        repo = RunRepository(session)
        pr_id = repo.get_or_create_pull_request("o/r", 1).id
        repo.create_run(pr_id, SHA, "same-delivery")
        repo.create_run(pr_id, SHA, "same-delivery")


def test_get_run_by_delivery_id(db_tables):
    with session_scope() as session:
        repo = RunRepository(session)
        repo.create_run(repo.get_or_create_pull_request("o/r", 1).id, SHA, "d1")
    with session_scope() as session:
        repo = RunRepository(session)
        assert repo.get_run_by_delivery_id("d1") is not None
        assert repo.get_run_by_delivery_id("nope") is None


def test_finish_run_unknown_run_raises(db_tables):
    import uuid

    with pytest.raises(LookupError), session_scope() as session:
        RunRepository(session).finish_run(uuid.uuid4(), status="failed")
