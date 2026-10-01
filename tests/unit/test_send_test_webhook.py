"""scripts/send_test_webhook.py: signing, safety guards, and that it really drives the app."""

import importlib.util
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from reviewer.config import get_settings
from reviewer.github.signature import verify_signature
from reviewer.main import create_app
from reviewer.schemas import PullRequestEvent
from reviewer.services import review_service
from tests.fakes import clean_dependencies
from tests.helpers import AUTH, WEBHOOK_SECRET

SCRIPT = Path(__file__).parents[2] / "scripts" / "send_test_webhook.py"
spec = importlib.util.spec_from_file_location("send_test_webhook", SCRIPT)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

ENV = {"GITHUB_WEBHOOK_SECRET": WEBHOOK_SECRET}
ARGS = ["--repo", "octo/repo", "--pr", "7"]


class FakePost:
    """Stands in for httpx.post: records the request, returns a canned response."""

    def __init__(self, status=202, body=None):
        self.calls = []
        self.response = httpx.Response(status, json=body or {"status": "queued", "run_id": "r-1"})

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_payload_is_a_valid_pull_request_event():
    payload = script.build_payload("octo/repo", 7, "b" * 40, "My PR", action="synchronize")

    event = PullRequestEvent.model_validate(payload)

    assert event.action == "synchronize"
    assert (event.repository.full_name, event.pull_request.number) == ("octo/repo", 7)
    assert event.pull_request.head.sha == "b" * 40 and not event.pull_request.draft


def test_request_is_signed_so_the_app_will_accept_it(capsys):
    post = FakePost()

    assert script.main(ARGS, post=post, env=ENV) == 0

    ((url, kwargs),) = post.calls
    assert url == "http://localhost:8000/webhooks/github"
    headers, body = kwargs["headers"], kwargs["content"]
    assert verify_signature(WEBHOOK_SECRET, body, headers["X-Hub-Signature-256"])
    assert headers["X-GitHub-Event"] == "pull_request"
    assert headers["X-GitHub-Delivery"]
    assert "http://localhost:8000/runs/r-1" in capsys.readouterr().out


def test_each_run_gets_a_fresh_delivery_id_unless_one_is_given():
    post = FakePost()

    script.main(ARGS, post=post, env=ENV)
    script.main(ARGS, post=post, env=ENV)
    script.main([*ARGS, "--delivery", "fixed"], post=post, env=ENV)

    ids = [kwargs["headers"]["X-GitHub-Delivery"] for _, kwargs in post.calls]
    assert ids[0] != ids[1] and ids[2] == "fixed"


def test_secret_is_never_printed_in_any_mode(capsys):
    post = FakePost()
    script.main(ARGS, post=post, env=ENV)
    script.main([*ARGS, "--dry-run"], post=post, env=ENV)
    signature = post.calls[0][1]["headers"]["X-Hub-Signature-256"]

    output = capsys.readouterr().out
    assert WEBHOOK_SECRET not in output
    assert signature not in output and signature.removeprefix("sha256=") not in output
    assert "<hidden>" in output  # the dry run shows that a signature would be attached


def test_dry_run_sends_nothing(capsys):
    post = FakePost()

    assert script.main([*ARGS, "--dry-run"], post=post, env=ENV) == 0

    assert post.calls == []
    assert '"full_name": "octo/repo"' in capsys.readouterr().out


def test_secret_falls_back_to_the_dotenv_file_and_environment_wins():
    Path(".env").write_text("GITHUB_WEBHOOK_SECRET=from-dotenv\nOTHER=1\n")  # cwd is a tmp dir
    post = FakePost()

    script.main(ARGS, post=post, env={})
    assert verify_signature(
        "from-dotenv",
        post.calls[0][1]["content"],
        post.calls[0][1]["headers"]["X-Hub-Signature-256"],
    )

    script.main(ARGS, post=post, env=ENV)
    assert verify_signature(
        WEBHOOK_SECRET,
        post.calls[1][1]["content"],
        post.calls[1][1]["headers"]["X-Hub-Signature-256"],
    )


def test_missing_secret_is_a_clear_error_and_sends_nothing(capsys):
    post = FakePost()

    assert script.main(ARGS, post=post, env={}) == 2

    assert post.calls == [] and "GITHUB_WEBHOOK_SECRET not found" in capsys.readouterr().out


def test_remote_urls_are_refused_unless_explicitly_allowed():
    post = FakePost()
    remote = [*ARGS, "--url", "https://example.ngrok.app/webhooks/github"]

    assert script.main(remote, post=post, env=ENV) == 2
    assert post.calls == []

    assert script.main([*remote, "--allow-remote"], post=post, env=ENV) == 0
    assert len(post.calls) == 1


def test_unreachable_app_gives_a_friendly_error(capsys):
    def refuse(url, **kwargs):
        raise httpx.ConnectError("refused")

    assert script.main(ARGS, post=refuse, env=ENV) == 1
    assert "Could not reach the app" in capsys.readouterr().out


def test_rejected_request_is_reported_with_a_failing_exit_code(capsys):
    post = FakePost(status=401, body={"detail": "invalid signature"})

    assert script.main(ARGS, post=post, env=ENV) == 1
    assert "401" in capsys.readouterr().out


def test_script_drives_the_real_app_end_to_end(monkeypatch, db_tables, capsys):
    """Script -> signature check -> webhook -> DB -> (fake clients) review -> /runs."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    get_settings.cache_clear()
    monkeypatch.setattr(review_service, "build_dependencies", clean_dependencies)
    client = TestClient(create_app(), headers=AUTH)

    def post_to_app(url, *, content, headers, timeout):
        return client.post("/webhooks/github", content=content, headers=headers)

    assert script.main([*ARGS, "--delivery", "d-1"], post=post_to_app, env=ENV) == 0
    assert script.main([*ARGS, "--delivery", "d-1"], post=post_to_app, env=ENV) == 0  # redelivery
    assert script.main([*ARGS, "--draft"], post=post_to_app, env=ENV) == 0  # skipped draft

    out = capsys.readouterr().out
    assert out.count("HTTP 202") == 1 and "duplicate" in out and "draft" in out
    run_id = json.loads(out.split("HTTP 202: ")[1].splitlines()[0])["run_id"]
    assert client.get(f"/runs/{run_id}").json()["status"] == "succeeded"
    assert client.get("/runs").json()["total"] == 1
