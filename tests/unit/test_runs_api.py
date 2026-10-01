"""GET /runs and GET /runs/{id}: filters, pagination, detail shape, read-only."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from reviewer.main import create_app
from tests.helpers import AUTH, call_row, finding_row, seed_run

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(db_tables):
    return TestClient(create_app(), headers=AUTH)


def test_empty_list(client):
    assert client.get("/runs").json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_list_is_newest_first_with_summary_fields(client):
    old = seed_run(number=1, status="succeeded", created_at=T0)
    new = seed_run(
        number=2,
        status="succeeded",
        created_at=T0 + timedelta(hours=1),
        verdict="request_changes",
        total_input_tokens=300,
        total_output_tokens=45,
        total_latency_ms=1200,
        started_at=T0,
        finished_at=T0 + timedelta(seconds=3),
        findings=[finding_row(), finding_row(title="Another")],
    )

    page = client.get("/runs").json()

    assert [item["id"] for item in page["items"]] == [str(new), str(old)]
    first = page["items"][0]
    assert first["repo"] == "octo-org/orders" and first["pr_number"] == 2
    assert first["verdict"] == "request_changes"
    assert first["tokens"] == {"input": 300, "output": 45, "total": 345}
    assert first["llm_latency_ms"] == 1200
    assert first["duration_ms"] == 3000
    assert first["findings_count"] == 2
    assert page["items"][1]["findings_count"] == 0 and page["items"][1]["duration_ms"] is None


def test_filters_by_status_and_repo(client):
    seed_run(repo="a/one", number=1, status="succeeded", created_at=T0)
    seed_run(repo="a/one", number=2, status="failed", created_at=T0 + timedelta(minutes=1))
    seed_run(repo="b/two", number=1, status="succeeded", created_at=T0 + timedelta(minutes=2))

    assert client.get("/runs?status=succeeded").json()["total"] == 2
    assert client.get("/runs?status=failed").json()["total"] == 1
    assert client.get("/runs?repo=a/one").json()["total"] == 2
    both = client.get("/runs?repo=a/one&status=succeeded").json()
    assert both["total"] == 1 and both["items"][0]["pr_number"] == 1
    assert client.get("/runs?repo=nobody/nothing").json()["items"] == []


def test_pagination_reports_the_total_not_the_page_size(client):
    for i in range(5):
        seed_run(number=i + 1, created_at=T0 + timedelta(minutes=i))

    page = client.get("/runs?limit=2&offset=2").json()

    assert page["total"] == 5 and page["limit"] == 2 and page["offset"] == 2
    assert [item["pr_number"] for item in page["items"]] == [3, 2]  # newest first: 5,4 | 3,2 | 1
    assert client.get("/runs?limit=2&offset=4").json()["items"][0]["pr_number"] == 1
    assert client.get("/runs?limit=2&offset=10").json()["items"] == []


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "status=done", "limit=abc"])
def test_invalid_query_parameters_are_rejected(client, query):
    assert client.get(f"/runs?{query}").status_code == 422


def test_detail_shows_findings_test_suggestions_llm_calls_tokens_and_errors(client):
    run_id = seed_run(
        status="succeeded",
        created_at=T0,
        started_at=T0,
        finished_at=T0 + timedelta(seconds=2, milliseconds=500),
        verdict="request_changes",
        summary="One real bug.",
        warnings="test_analysis failed: LLMError: down\n2 large file diff(s) were truncated",
        review_comment_id=987,
        total_input_tokens=150,
        total_output_tokens=30,
        total_latency_ms=800,
        findings=[
            finding_row(title="low one", severity="low"),
            finding_row(title="high one", severity="high", category="security"),
            finding_row(
                title="Add a test",
                category="missing_test",
                suggested_test_code="def test_x():\n    assert True",
            ),
        ],
        calls=[
            call_row(node="code_review"),
            call_row(node="test_analysis", error="LLMError: down", input_tokens=0, output_tokens=0),
        ],
    )

    body = client.get(f"/runs/{run_id}").json()

    assert body["status"] == "succeeded" and body["verdict"] == "request_changes"
    assert body["summary"] == "One real bug." and body["error"] is None
    assert body["warnings"] == [
        "test_analysis failed: LLMError: down",
        "2 large file diff(s) were truncated",
    ]
    assert body["review_comment_id"] == 987
    assert body["duration_ms"] == 2500
    assert body["tokens"] == {"input": 150, "output": 30, "total": 180}
    assert body["llm_latency_ms"] == 800
    assert body["findings_count"] == 3
    assert [f["title"] for f in body["findings"]] == ["high one", "low one"]  # severity order
    assert body["findings"][0]["confidence"] == 0.9
    (suggestion,) = body["test_suggestions"]
    assert suggestion["title"] == "Add a test" and "def test_x" in suggestion["suggested_test_code"]
    assert [c["node"] for c in body["llm_calls"]] == ["code_review", "test_analysis"]
    assert body["llm_calls"][1]["error"] == "LLMError: down"
    assert body["llm_calls"][0]["provider"] == "openai" and body["llm_calls"][0]["retries"] == 0


def test_detail_of_a_failed_run_shows_the_error(client):
    run_id = seed_run(status="failed", error="code_review failed on every chunk: boom")

    body = client.get(f"/runs/{run_id}").json()

    assert body["status"] == "failed"
    assert body["error"] == "code_review failed on every chunk: boom"
    assert body["findings"] == [] and body["llm_calls"] == [] and body["warnings"] == []


def test_unknown_run_is_404_and_malformed_id_is_422(client):
    assert client.get(f"/runs/{uuid.uuid4()}").status_code == 404
    assert client.get("/runs/not-a-uuid").status_code == 422


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_endpoints_are_read_only(client, method):
    run_id = seed_run()

    assert getattr(client, method)("/runs").status_code == 405
    assert getattr(client, method)(f"/runs/{run_id}").status_code == 405
