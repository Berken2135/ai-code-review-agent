"""Send a signed, realistic `pull_request` webhook to a locally running app.

The app then fetches the real PR from GitHub (using its own token), reviews it and posts the
comment, exactly as if GitHub had delivered the event. No public tunnel is needed.

    python scripts/send_test_webhook.py --repo you/test-repo --pr 1

The webhook secret is read from the GITHUB_WEBHOOK_SECRET environment variable, or from `.env`
in the current directory (only that one key is read). It is never printed.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values

DEFAULT_URL = "http://localhost:8000/webhooks/github"
SECRET_NAME = "GITHUB_WEBHOOK_SECRET"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
PLACEHOLDER_SHA = "0" * 40  # the app reviews the PR's current head and records the real sha


def build_payload(
    repo: str, pr: int, sha: str, title: str, action: str = "opened", draft: bool = False
) -> dict:
    """The parts of GitHub's `pull_request` payload the app reads (plus realistic extras)."""
    return {
        "action": action,
        "number": pr,
        "pull_request": {
            "number": pr,
            "title": title,
            "state": "open",
            "draft": draft,
            "head": {"ref": "feature", "sha": sha},
            "base": {"ref": "main", "sha": PLACEHOLDER_SHA},
        },
        "repository": {"full_name": repo},
        "sender": {"login": repo.split("/")[0]},
    }


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def load_secret(env: Mapping[str, str], dotenv_path: str = ".env") -> str | None:
    """Environment first, then `.env`. Only the webhook secret is read; nothing is echoed."""
    return env.get(SECRET_NAME) or dotenv_values(dotenv_path).get(SECRET_NAME) or None


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="owner/name of the test repository")
    parser.add_argument("--pr", required=True, type=int, help="pull request number")
    parser.add_argument("--sha", default=PLACEHOLDER_SHA, help="head commit sha (optional)")
    parser.add_argument("--title", default="Sample pull request")
    parser.add_argument("--action", default="opened")
    parser.add_argument("--draft", action="store_true", help="send a draft PR (the app skips it)")
    parser.add_argument("--delivery", default=None, help="reuse an id to test redelivery")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--allow-remote", action="store_true", help="allow a non-local --url")
    parser.add_argument("--dry-run", action="store_true", help="print the request, send nothing")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    post: Callable[..., httpx.Response] = httpx.post,
    env: Mapping[str, str] = os.environ,
) -> int:
    args = parse_args(argv)
    url = urlparse(args.url)
    if url.hostname not in LOCAL_HOSTS and not args.allow_remote:
        print(f"Refusing to send to non-local host {url.hostname!r}; pass --allow-remote.")
        return 2
    secret = load_secret(env)
    if not secret:
        print(f"{SECRET_NAME} not found in the environment or in .env")
        return 2

    payload = build_payload(args.repo, args.pr, args.sha, args.title, args.action, args.draft)
    body = json.dumps(payload).encode()
    delivery = args.delivery or str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": sign(secret, body),
    }

    if args.dry_run:
        shown = {**headers, "X-Hub-Signature-256": "sha256=<hidden>"}
        print(f"POST {args.url}\n{json.dumps(shown, indent=2)}\n{json.dumps(payload, indent=2)}")
        return 0

    print(f"POST {args.url}  (delivery {delivery})")
    try:
        response = post(args.url, content=body, headers=headers, timeout=15)
    except httpx.HTTPError as exc:
        print(f"Could not reach the app: {type(exc).__name__}. Is it running? (docker compose ps)")
        return 1

    print(f"HTTP {response.status_code}: {response.text}")
    if response.status_code == 202:
        run_id = response.json()["run_id"]
        print(f"Run details: {url.scheme}://{url.netloc}/runs/{run_id}")
        print("  (send the header 'Authorization: Bearer <RUNS_API_TOKEN>')")
    return 0 if response.status_code in (200, 202) else 1


if __name__ == "__main__":
    sys.exit(main())
