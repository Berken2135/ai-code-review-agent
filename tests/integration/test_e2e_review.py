"""End to end, fully offline: signed webhook -> DB -> fake GitHub + FakeLLM -> comment -> /runs.

No keys, no network. The only fakes are the GitHub client and the LLM; everything else (signature
check, webhook, DB, background task, agent graph, formatter, persistence, API) is the real code.
"""

import json

import pytest
from fastapi.testclient import TestClient

from reviewer.config import get_settings
from reviewer.github.client import GitHubError
from reviewer.llm.base import LLMError
from reviewer.llm.fake import FakeLLM
from reviewer.main import create_app
from reviewer.services import review_service
from reviewer.services.review_service import Dependencies
from tests.fakes import COMMENT_ID, load_sample, sample_github, sample_llm
from tests.helpers import AUTH, WEBHOOK_SECRET, load_fixture, seed_run, webhook_headers

PR = load_sample("pr.json")


@pytest.fixture
def app(monkeypatch, db_tables):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    get_settings.cache_clear()
    return create_app()


@pytest.fixture
def use_deps(monkeypatch):
    """Make the background task use these fake clients instead of real ones."""

    def install(github, llm):
        monkeypatch.setattr(review_service, "build_dependencies", lambda: Dependencies(github, llm))

    return install


def send_webhook(client, delivery="delivery-e2e"):
    payload = load_fixture("pr_opened.json")
    payload["repository"]["full_name"] = PR["repo"]
    payload["pull_request"]["number"] = PR["number"]
    payload["pull_request"]["head"]["sha"] = PR["head_sha"]
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github", content=body, headers=webhook_headers(body, delivery=delivery)
    )


def test_pr_event_to_posted_comment_to_run_details(app, use_deps):
    github, llm = sample_github(), sample_llm()
    use_deps(github, llm)
    client = TestClient(app, headers=AUTH)

    response = send_webhook(client)

    # 1. the webhook accepted it
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    # 2. the agent ran against the fakes and a comment was posted, once
    assert [c.method for c in llm.calls] == ["complete_structured"] * 3
    (repo, number, body) = github.comments[0]
    assert (repo, number) == (PR["repo"], PR["number"]) and len(github.comments) == 1
    assert body.startswith("<!-- pr-review-agent -->")
    assert "SQL injection in get_order" in body
    assert "Suggested tests (1)" in body
    assert "Changes requested" in body

    # 3. /runs/{id} shows everything
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "succeeded" and run["error"] is None
    assert run["repo"] == PR["repo"] and run["pr_number"] == PR["number"]
    assert run["head_sha"] == PR["head_sha"]
    assert run["verdict"] == "request_changes"
    assert run["summary"].startswith("Adds discount and order lookup helpers")
    assert run["review_comment_id"] == COMMENT_ID
    assert run["tokens"] == {"input": 30, "output": 15, "total": 45}
    assert run["llm_latency_ms"] == 0 and run["duration_ms"] >= 0
    assert run["started_at"] and run["finished_at"] and run["warnings"] == []
    assert [(f["category"], f["severity"]) for f in run["findings"]] == [
        ("security", "high"),
        ("bug", "medium"),
    ]
    assert run["findings"][0]["file_path"] == "src/orders/service.py"
    assert (run["findings"][0]["line_start"], run["findings"][0]["line_end"]) == (9, 10)
    (suggestion,) = run["test_suggestions"]
    assert "pytest.raises(ValueError)" in suggestion["suggested_test_code"]
    assert [c["node"] for c in run["llm_calls"]] == ["code_review", "test_analysis", "final_review"]
    assert all(c["error"] is None and c["provider"] == "fake" for c in run["llm_calls"])
    assert run["findings_count"] == 3

    # 4. and it is listed
    page = client.get("/runs?status=succeeded&repo=" + PR["repo"]).json()
    assert page["total"] == 1 and page["items"][0]["id"] == run_id
    assert client.get("/runs?status=failed").json()["total"] == 0


def test_redelivery_does_not_review_or_comment_twice(app, use_deps):
    github, llm = sample_github(), sample_llm()
    use_deps(github, llm)
    client = TestClient(app, headers=AUTH)

    first = send_webhook(client, delivery="same-delivery")
    second = send_webhook(client, delivery="same-delivery")

    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json() == {"status": "duplicate"}
    assert len(github.comments) == 1 and len(llm.calls) == 3
    assert client.get("/runs").json()["total"] == 1


# --- failure paths -----------------------------------------------------------------------------


def test_llm_failure_ends_as_a_failed_run_with_the_error_and_the_call_on_record(app, use_deps):
    github = sample_github()
    use_deps(github, FakeLLM([LLMError("provider is down")]))
    client = TestClient(app, headers=AUTH)

    assert send_webhook(client).status_code == 202  # the webhook itself always answers fast
    (item,) = client.get("/runs").json()["items"]
    run = client.get(f"/runs/{item['id']}").json()

    assert run["status"] == "failed"
    assert (
        "code_review failed on every chunk" in run["error"] and "provider is down" in run["error"]
    )
    assert run["verdict"] is None and run["findings"] == [] and run["review_comment_id"] is None
    assert [(c["node"], bool(c["error"])) for c in run["llm_calls"]] == [("code_review", True)]
    assert github.comments == []  # nothing is posted for a failed review


def test_github_403_on_comment_post_fails_the_run_but_keeps_the_review(app, use_deps):
    forbidden = GitHubError(
        "GitHub POST -> 403: Resource not accessible by personal access token", 403
    )
    github = sample_github(fail_on={"upsert_bot_comment": forbidden})
    use_deps(github, sample_llm())
    client = TestClient(app, headers=AUTH)

    send_webhook(client)
    (item,) = client.get("/runs?status=failed").json()["items"]
    run = client.get(f"/runs/{item['id']}").json()

    assert run["status"] == "failed"
    assert run["error"].startswith("Review finished but posting the PR comment failed")
    assert "403" in run["error"] and "Resource not accessible" in run["error"]
    assert run["review_comment_id"] is None
    assert run["verdict"] == "request_changes" and run["findings_count"] == 3  # not lost
    assert len(run["llm_calls"]) == 3


def test_stuck_runs_are_swept_when_the_app_restarts(app, use_deps):
    stuck_running = seed_run(number=1, status="running")
    stuck_queued = seed_run(number=2, status="queued")
    finished = seed_run(number=3, status="succeeded")

    with TestClient(
        app, headers=AUTH
    ) as client:  # entering the context runs startup, i.e. the sweep
        running = client.get(f"/runs/{stuck_running}").json()
        queued = client.get(f"/runs/{stuck_queued}").json()
        done = client.get(f"/runs/{finished}").json()

    for run, was in ((running, "running"), (queued, "queued")):
        assert run["status"] == "failed"
        assert "app restarted" in run["error"] and f"status was '{was}'" in run["error"]
        assert run["finished_at"] is not None
    assert done["status"] == "succeeded" and done["error"] is None
