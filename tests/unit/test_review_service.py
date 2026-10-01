"""review_service.run: persistence, every failure path, and the 'nothing escapes' guarantee."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from reviewer.config import get_settings
from reviewer.db.models import Finding, LLMCall, Run
from reviewer.db.session import session_scope
from reviewer.github.client import GitHubError
from reviewer.llm.base import LLMError
from reviewer.llm.fake import FakeLLM
from reviewer.logging import configure_logging
from reviewer.services import review_service
from reviewer.services.review_service import Dependencies
from tests.fakes import COMMENT_ID, SHA, FakeGitHub, load_sample, sample_github, sample_llm
from tests.helpers import seed_run

SAMPLE_SHA = load_sample("pr.json")["head_sha"]


@pytest.fixture(autouse=True)
def _schema(db_tables):
    """Every test here needs the tables."""


def queued() -> uuid.UUID:
    return seed_run(repo="octo-org/orders", number=17, status="queued")


def load(run_id):
    with session_scope() as session:
        run = session.get(Run, run_id)
        findings = list(session.scalars(select(Finding).where(Finding.run_id == run_id)))
        calls = list(session.scalars(select(LLMCall).where(LLMCall.run_id == run_id)))
        return run, findings, calls


def deps(github=None, llm=None) -> Dependencies:
    return Dependencies(github or sample_github(), llm or sample_llm())


def test_success_persists_findings_test_suggestions_calls_totals_and_comment_id():
    run_id, github = queued(), sample_github()

    review_service.run(run_id, Dependencies(github, sample_llm()))

    run, findings, calls = load(run_id)
    assert run.status == "succeeded" and run.error is None
    assert run.verdict == "request_changes"
    assert run.summary.startswith("Adds discount and order lookup helpers")
    assert run.review_comment_id == COMMENT_ID
    assert run.head_sha == SAMPLE_SHA
    assert run.started_at is not None and run.finished_at is not None
    assert (run.total_input_tokens, run.total_output_tokens) == (30, 15)  # FakeLLM: 10/5 per call
    assert run.total_latency_ms == 0
    assert sorted(f.category for f in findings) == ["bug", "missing_test", "security"]
    test_suggestion = next(f for f in findings if f.category == "missing_test")
    assert "test_invalid_discount_is_rejected" in test_suggestion.suggested_test_code
    assert all(f.confidence is not None for f in findings)
    assert [c.node for c in sorted(calls, key=lambda c: c.id)] == [
        "code_review",
        "test_analysis",
        "final_review",
    ]
    (repo, number, body) = github.comments[0]
    assert (repo, number) == ("octo-org/orders", 17)
    assert body.startswith("<!-- pr-review-agent -->")


def test_optional_step_failure_still_succeeds_and_records_a_warning():
    llm = FakeLLM(
        [
            load_sample("llm_code_review.json"),
            LLMError("test model down"),
            load_sample("llm_final_review.json"),
        ]
    )
    run_id, github = queued(), sample_github()

    review_service.run(run_id, Dependencies(github, llm))

    run, findings, calls = load(run_id)
    assert run.status == "succeeded"
    assert "test_analysis failed" in run.warnings
    assert sorted(f.category for f in findings) == ["bug", "security"]
    assert any(c.node == "test_analysis" and c.error for c in calls)
    assert "Test analysis failed, so this review has no test suggestions." in github.comments[0][2]


def test_llm_failure_fails_the_run_but_keeps_the_calls_and_posts_nothing():
    run_id, github = queued(), sample_github()

    review_service.run(run_id, Dependencies(github, FakeLLM([LLMError("provider down")])))

    run, findings, calls = load(run_id)
    assert run.status == "failed"
    assert "code_review failed on every chunk" in run.error and "provider down" in run.error
    assert run.finished_at is not None and run.verdict is None
    assert findings == []
    assert [(c.node, bool(c.error)) for c in calls] == [("code_review", True)]
    assert github.comments == []


def test_github_read_failure_fails_the_run_with_a_clear_error():
    github = sample_github(fail_on={"get_pull_request": GitHubError("GitHub is down", 503)})
    run_id = queued()

    review_service.run(run_id, Dependencies(github, sample_llm()))

    run, _, _ = load(run_id)
    assert run.status == "failed"
    assert "fetch_context failed: GitHubError: GitHub is down" in run.error


def test_comment_post_failure_fails_the_run_but_keeps_the_finished_review():
    forbidden = GitHubError(
        "GitHub POST -> 403: Resource not accessible by personal access token", 403
    )
    github = sample_github(fail_on={"upsert_bot_comment": forbidden})
    run_id = queued()

    review_service.run(run_id, Dependencies(github, sample_llm()))

    run, findings, calls = load(run_id)
    assert run.status == "failed"
    assert run.error.startswith("Review finished but posting the PR comment failed")
    assert "403" in run.error
    assert run.review_comment_id is None
    assert run.verdict == "request_changes"  # the work is not thrown away
    assert len(findings) == 3 and len(calls) == 3


def test_missing_github_token_fails_the_run_without_touching_the_network():
    run_id = queued()  # no deps injected: the real build_dependencies runs

    review_service.run(run_id)

    run, _, calls = load(run_id)
    assert run.status == "failed"
    assert "OPENAI_API_KEY is not set" in run.error  # the LLM key is validated first
    assert calls == []


def test_missing_token_with_an_llm_key_names_the_token(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
    get_settings.cache_clear()
    run_id = queued()

    review_service.run(run_id)

    run, _, _ = load(run_id)
    assert run.status == "failed" and "GITHUB_TOKEN is not configured" in run.error


def test_unknown_run_id_is_ignored():
    llm = sample_llm()

    review_service.run(uuid.uuid4(), Dependencies(sample_github(), llm))

    assert llm.calls == []


@pytest.mark.parametrize("status", ["running", "succeeded", "failed"])
def test_a_run_that_is_not_queued_is_not_started_again(status):
    run_id = seed_run(repo="octo-org/orders", number=17, status=status)
    llm = sample_llm()

    review_service.run(run_id, Dependencies(sample_github(), llm))

    assert llm.calls == []
    assert load(run_id)[0].status == status


def test_running_the_same_run_twice_reviews_once():
    run_id, llm = queued(), sample_llm()

    review_service.run(run_id, Dependencies(sample_github(), llm))
    review_service.run(run_id, Dependencies(sample_github(), llm))

    assert len(llm.calls) == 3
    assert len(load(run_id)[1]) == 3  # findings were not saved twice


def test_a_persistence_failure_is_recorded_as_a_failed_run(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(review_service, "_persist", boom)
    run_id = queued()

    review_service.run(run_id, deps())  # must not raise

    run, _, _ = load(run_id)
    assert run.status == "failed"
    assert "saving the results failed: RuntimeError: disk full" in run.error


def test_an_unexpected_crash_is_recorded_and_never_escapes(monkeypatch):
    monkeypatch.setattr(review_service, "_execute", lambda *a, **k: 1 / 0)
    run_id = queued()

    review_service.run(run_id, deps())

    run, _, _ = load(run_id)
    assert run.status == "failed" and "Unexpected internal error" in run.error


def test_a_database_outage_cannot_raise_out_of_the_background_task(monkeypatch):
    def down():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(review_service, "session_scope", down)

    review_service.run(uuid.uuid4(), deps())  # logged; the startup sweep cleans up on restart


def test_secrets_never_reach_the_logs_or_the_run_record(monkeypatch, capsys):
    secrets = {"GITHUB_TOKEN": "ghp_SECRET_TOKEN_VALUE", "OPENAI_API_KEY": "sk-SECRET-KEY-VALUE"}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    configure_logging("DEBUG")
    run_id = queued()

    review_service.run(run_id, Dependencies(sample_github(), FakeLLM([LLMError("provider down")])))

    run, _, _ = load(run_id)
    output = capsys.readouterr().out
    for value in secrets.values():
        assert value not in output
        assert value not in (run.error or "")
    assert "review_run_started" in output  # the logging assertion above is not vacuous


def test_owned_clients_are_closed_but_injected_ones_are_not(monkeypatch):
    class Closable(FakeGitHub):
        closed = False

        def close(self):
            self.closed = True

    injected = Closable(files=sample_github().files)
    review_service.run(queued(), Dependencies(injected, FakeLLM([LLMError("x")])))
    assert not injected.closed  # the caller owns injected clients

    owned = Closable(files=sample_github().files)
    monkeypatch.setattr(
        review_service, "build_dependencies", lambda: Dependencies(owned, FakeLLM([LLMError("x")]))
    )
    review_service.run(queued())
    assert owned.closed  # clients the service built itself are released


def test_the_reviewed_commit_replaces_the_stale_webhook_sha():
    run_id = seed_run(repo="octo-org/orders", number=17, status="queued")  # created with "a" * 40
    assert load(run_id)[0].head_sha == SHA

    review_service.run(run_id, deps())

    assert load(run_id)[0].head_sha == SAMPLE_SHA
