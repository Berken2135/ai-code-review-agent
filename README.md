<div align="center">

# 🤖 AI Code Review Agent

**An AI agent that reviews GitHub pull requests: it finds bugs, security issues and missing tests, and posts one structured review comment on the PR.**

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1C3C3C?logo=langchain&logoColor=white)](https://langchain-ai.github.io/langgraph/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker_Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
<br/>
[![CI](https://github.com/Berken2135/ai-code-review-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Berken2135/ai-code-review-agent/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-351_passing-brightgreen?logo=pytest&logoColor=white)](#-testing)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

<img src="docs/images/review-comment.png" alt="The bot's review comment on a pull request" width="820"/>

</div>

---

## Contents

- [What it does](#-what-it-does)
- [Architecture](#-architecture)
- [Agent workflow](#-agent-workflow)
- [Tech stack](#-tech-stack)
- [Quickstart](#-quickstart)
- [Configuration](#-configuration)
- [API reference](#-api-reference)
- [Testing](#-testing)
- [Project structure](#-project-structure)
- [Design decisions](#-design-decisions)
- [Known limitations](#-known-limitations)
- [Future improvements](#-future-improvements)

## ✨ What it does

1. **Receives** GitHub `pull_request` webhooks, verifies their HMAC signature and ignores redeliveries.
2. **Fetches** the PR's changed files, drops noise (lockfiles, generated, vendored and binary files) and finds the existing tests for each changed module.
3. **Reviews** the diff with an LLM for bugs, security issues, performance problems, code quality and missing edge cases, each tied to a file and line range.
4. **Analyses tests**: finds untested behaviour and writes runnable test code (pytest, Jest or Vitest).
5. **Finalises**: deduplicates and ranks findings and drops low-confidence ones. It then applies deterministic guards the model cannot override.
6. **Posts one comment** on the PR and updates that same comment on every new push instead of adding more.
7. **Records** every run, finding and LLM call (tokens, latency, retries, errors) in PostgreSQL, readable through a token-protected `/runs` API.

### Result of a live run

A sandbox PR added `get_order()` with a string-concatenated SQL query and `apply_discount()` with no tests:

| | |
|---|---|
| Model | `gpt-4o-mini`, 3 LLM calls, 0 retries |
| Cost | 4,388 tokens in total, 13.7 s end to end |
| Verdict | ❌ `request_changes` |
| Top finding | 🔴 SQL injection in `get_order`, correct file and line, confidence 0.9, with a parameterised-query fix |
| Tests | 2 suggested tests with working pytest code |

## 🏗 Architecture

```mermaid
flowchart LR
    GH["GitHub<br/>pull_request event"] -->|"signed webhook"| WH

    subgraph APP["FastAPI service (one container)"]
        WH["POST /webhooks/github<br/>HMAC check, filter, dedupe"]
        BG["Background task<br/>review_service.run"]
        AG["LangGraph agent<br/>4 nodes"]
        FMT["Comment formatter<br/>deterministic code"]
        RUNS["GET /runs<br/>bearer token"]
        WH -->|"202 + run_id"| BG
        BG --> AG --> FMT
    end

    WH -->|"run: queued"| DB[("PostgreSQL<br/>runs, findings, llm_calls")]
    BG -->|"results, tokens, latency, errors"| DB
    AG <-->|"PR metadata, files, tests"| API["GitHub REST API"]
    AG <-->|"structured JSON"| LLM["LLM provider<br/>OpenAI or Anthropic"]
    FMT -->|"create or update one comment"| API
    OPS["Operator"] --> RUNS --> DB
```

Layers depend inward only: `api → services → agent → (github, llm, db)`. Agent nodes receive their GitHub and LLM clients by injection, so every node runs in tests against fakes. All SQL lives in one repository class.

### Request lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub
    participant API as FastAPI
    participant DB as PostgreSQL
    participant BG as Background task
    GH->>API: POST /webhooks/github with X-Hub-Signature-256
    API->>API: verify HMAC, filter event and action, skip drafts
    API->>DB: upsert PR, insert run (unique delivery id)
    API-->>GH: 202 with run_id (a redelivery gets 200 duplicate)
    API->>BG: schedule review_service.run(run_id)
    BG->>DB: claim run atomically (queued to running)
    BG->>BG: run the agent against GitHub and the LLM
    BG->>GH: create or update the bot comment
    BG->>DB: save findings, llm_calls, totals and final status
```

GitHub gives a webhook 10 seconds to answer, so the endpoint does no review work inline. It records the run and returns `202` straight away.

## 🧠 Agent workflow

```mermaid
flowchart TD
    START(["Run claimed: queued to running"]) --> F
    F["fetch_context<br/>no LLM"] -->|"ok"| C
    F -->|"GitHub error"| FAIL
    C["code_review<br/>LLM, once per diff chunk"] -->|"at least one chunk ok"| T
    C -->|"every chunk failed"| FAIL
    T["test_analysis<br/>LLM, optional"] --> R
    T -.->|"fails: ships without test suggestions"| R
    R["final_review<br/>LLM ranks, code enforces"] --> P
    R -.->|"LLM fails: automatic ranking"| P
    P["Format comment, post or update it"] --> OK(["Run succeeded"])
    P -->|"GitHub refuses, e.g. 403"| FAIL
    FAIL(["Run failed: error and llm_calls kept"])
```

| Node | LLM | Responsibility |
|---|:---:|---|
| `fetch_context` | – | Loads PR metadata and changed files, and filters out lockfiles, generated, vendored, binary and deleted files. Orders files source first, then tests, config and docs. Numbers every diff line and packs files into size-limited chunks. Maps each module to its existing tests (`foo.py` ↔ `test_foo.py`, `foo.ts` ↔ `foo.test.ts`) and detects Jest or Vitest from `package.json`. |
| `code_review` | ✅ | One call per chunk. Returns findings with a category, severity, file, line range and confidence. |
| `test_analysis` | ✅ | Compares the diff with the existing tests and the findings, then proposes missing tests with runnable code. |
| `final_review` | ✅ | The model suggests an order, duplicates to drop, a summary and a verdict. Code then applies the rules below. |

**Deterministic guards.** These are code, not prompts, because models hallucinate and can be manipulated:

- A finding whose file is not in the PR is fixed if the path is unambiguous and dropped otherwise. Line numbers are clamped to lines that actually appear in the diff.
- Findings below the confidence threshold never reach the final step, and duplicates are merged.
- The model can reorder findings, but only within a severity level. It can never drop a high-severity finding, and ids it invents are ignored.
- The verdict cannot contradict the findings: a remaining high-severity finding always means `request_changes`.
- The comment is rendered by code from structured data. LLM text is HTML-escaped and `@mentions` are neutralised. The comment is kept under GitHub's 65,536-character limit by cutting the lowest-priority content first.

## 🧰 Tech stack

| Area | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Typed, PEP 695 generics |
| API | FastAPI + Uvicorn | Async webhook endpoint, background tasks, OpenAPI docs |
| Agent orchestration | LangGraph | Explicit state machine with conditional edges for the failure policy |
| LLM providers | OpenAI SDK, Anthropic SDK | One interface; switched with `LLM_PROVIDER` |
| Structured output | Pydantic v2 | Schema validation and one repair round |
| GitHub | httpx (REST v3) | PAT auth, pagination, rate-limit aware |
| Retries | tenacity | Exponential backoff with jitter, `Retry-After` honoured |
| Database | PostgreSQL 16, SQLAlchemy 2.0, Alembic, psycopg 3 | Runs, findings and LLM calls, with versioned migrations |
| Logging | structlog | JSON logs; metadata only, never prompts or secrets |
| Quality | pytest, respx, Ruff | Offline test suite, lint and format |
| Delivery | Docker, Docker Compose, GitHub Actions | One-command local stack; CI runs against real Postgres |

## 🚀 Quickstart

**Prerequisites:** Docker Desktop, Python 3.12+ (for the helper script and tests), a GitHub fine-grained PAT, and an OpenAI or Anthropic API key.

### 1. Configure

```bash
git clone https://github.com/Berken2135/ai-code-review-agent.git
cd ai-code-review-agent
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
```

Fill in `.env` (see [Configuration](#-configuration)). You need at least:

- `POSTGRES_PASSWORD`, using letters and digits only
- `GITHUB_TOKEN`
- `GITHUB_WEBHOOK_SECRET`
- `RUNS_API_TOKEN`
- `OPENAI_API_KEY`

The PAT needs **Contents: Read** and **Pull requests: Read and write** on the target repository. Some setups also need **Issues: Read and write** to post the comment.

### 2. Start

```bash
make up
```

On Windows without `make`:

```powershell
docker compose up --build -d
```

This builds the image and starts PostgreSQL and the app. Migrations run automatically when the container starts, and runs left behind by a previous process are marked `failed`. Check it:

```bash
curl http://localhost:8000/health        # {"status":"ok"}
```

### 3. Trigger a review

With the app running locally, send a signed webhook for a real PR. The app fetches the PR from GitHub, reviews it and posts the comment. No public URL is needed.

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python scripts/send_test_webhook.py --repo YOUR_USERNAME/your-sandbox-repo --pr 1
```

[`examples/sample-pr/`](examples/sample-pr/) contains a ready-made sandbox repository. `base/` is the initial commit, and `change/` is a PR with a deliberate SQL injection and missing tests.

### 4. Inspect the result

```bash
curl -H "Authorization: Bearer $RUNS_API_TOKEN" http://localhost:8000/runs
curl -H "Authorization: Bearer $RUNS_API_TOKEN" http://localhost:8000/runs/<run_id>
```

Interactive docs are at <http://localhost:8000/docs>. Use **Authorize** to enter the token.

To have GitHub call the app on every PR event instead, see [docs/tunnel.md](docs/tunnel.md).

| Command | Windows equivalent | Does |
|---|---|---|
| `make up` | `docker compose up --build -d` | Build and start app + Postgres |
| `make down` | `docker compose down` | Stop the stack (`-v` also deletes the data) |
| `make logs` | `docker compose logs -f app` | Follow the JSON logs |
| `make test` | `pytest` | Run the test suite (offline) |
| `make lint` | `ruff check . ; ruff format --check .` | Lint and format check |

## ⚙️ Configuration

All configuration comes from environment variables, loaded from `.env` by Compose and by the app. No secret is ever hard-coded or logged.

| Variable | Required | Default | Description |
|---|:---:|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | ✅ (Compose) | `reviewer` / – / `reviewer` | Database credentials. Compose builds `DATABASE_URL` from them for the app container. |
| `DATABASE_URL` | ✅ | – | SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@localhost:5432/reviewer`. Only needs setting when running outside Compose. |
| `GITHUB_TOKEN` | ✅ | – | Fine-grained PAT used to read PRs and post the comment. |
| `GITHUB_WEBHOOK_SECRET` | ✅ | – | Shared HMAC secret for webhook signatures. The webhook answers `503` while it is unset. |
| `RUNS_API_TOKEN` | ✅ | – | Bearer token for `/runs`. The endpoints answer `503` while it is unset (fail closed). |
| `LLM_PROVIDER` | | `openai` | `openai` or `anthropic`. |
| `LLM_MODEL` | | provider default | Empty means `gpt-4o-mini` (OpenAI) or `claude-sonnet-5` (Anthropic). |
| `LLM_TIMEOUT_SECONDS` | | `60` | Per-request LLM timeout. |
| `OPENAI_API_KEY` | if OpenAI | – | Only the key for the selected provider is required. |
| `ANTHROPIC_API_KEY` | if Anthropic | – | |
| `LOG_LEVEL` | | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

Agent limits live in [`agent/config.py`](src/reviewer/agent/config.py). They cover diff chunk size (24,000 characters), maximum chunks (4), minimum confidence (0.5), findings shown (15) and test suggestions (5).

## 📡 API reference

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | – | Liveness: `{"status": "ok"}` |
| `POST` | `/webhooks/github` | HMAC signature | GitHub webhook receiver |
| `GET` | `/runs` | Bearer token | List runs, newest first |
| `GET` | `/runs/{run_id}` | Bearer token | Full detail of one run |
| `GET` | `/docs` | – | Swagger UI (schema only, no data) |

### `POST /webhooks/github`

Requires the headers `X-Hub-Signature-256`, `X-GitHub-Event` and `X-GitHub-Delivery`. It handles `pull_request` events with the actions `opened`, `synchronize`, `reopened` and `ready_for_review`.

| Situation | Status | Body |
|---|---|---|
| Review queued | `202` | `{"status": "queued", "run_id": "…"}` |
| `ping` event | `200` | `{"status": "pong"}` |
| Other event or action, or a draft PR | `200` | `{"status": "ignored", "reason": "…"}` |
| Redelivery (same delivery id) | `200` | `{"status": "duplicate"}` |
| Missing or invalid signature | `401` | |
| Missing delivery id or invalid payload | `400` | |
| `GITHUB_WEBHOOK_SECRET` not configured | `503` | |

### `GET /runs`

Send the header `Authorization: Bearer <RUNS_API_TOKEN>`. Query parameters: `status` (`queued`, `running`, `succeeded` or `failed`), `repo` (exact `owner/name`), `limit` (1–100, default 20) and `offset`. A missing or wrong token gets `401`; an unconfigured token gets `503`.

```json
{
  "items": [
    {
      "id": "4623f96e-…", "status": "succeeded", "repo": "you/sandbox", "pr_number": 2,
      "head_sha": "c0ffee…", "verdict": "request_changes",
      "created_at": "…", "finished_at": "…", "duration_ms": 13700,
      "tokens": {"input": 3900, "output": 488, "total": 4388},
      "llm_latency_ms": 12900, "findings_count": 3
    }
  ],
  "total": 1, "limit": 20, "offset": 0
}
```

<sub>Example shape: individual values are illustrative.</sub>

### `GET /runs/{run_id}`

Everything from the list item, plus the following fields. An unknown id gets `404`.

| Field | Content |
|---|---|
| `summary` | The review summary posted on the PR |
| `error` | Why the run failed, e.g. `Review finished but posting the PR comment failed: … 403` |
| `warnings` | Degradations on a successful run: failed optional steps, truncated diffs, skipped files |
| `review_comment_id` | Id of the GitHub comment that was created or updated |
| `findings` | Category, severity, file, line range, title, description, suggestion and confidence |
| `test_suggestions` | Missing-test findings, including `suggested_test_code` |
| `llm_calls` | Per call: node, provider, model, tokens, latency, retries and error |

<p align="center"><img src="docs/images/runs-output.png" alt="Sample GET /runs/{id} output" width="760"/></p>

## 🧪 Testing

```bash
make test        # or: pytest
make lint
```

- **351 tests** (unit and integration) run fully **offline in about 20 seconds**, plus 1 PostgreSQL test that runs in CI. The suite needs no API keys.
  - A `conftest.py` guard makes any connection to a non-loopback host fail the test.
  - LLM providers are tested through the real SDK code with a mock HTTP transport, and GitHub with `respx`.
- **End to end:** a signed webhook goes through the DB, the agent (with a fake GitHub and a scripted `FakeLLM`), the posted comment, and then `/runs/{id}`. The failure paths are covered too: an LLM outage, a GitHub `403` on the comment post, and stuck runs swept on restart.
- **Golden file:** a sample PR runs through the whole graph and the comment must match [`expected_comment.md`](tests/fixtures/sample_pr/expected_comment.md) exactly. The PR includes a hallucinated file, an off-range line number, a duplicate finding and a prompt-injection attempt.
- **Prompt injection:** PR text reaches the model only inside delimiters that carry a random per-call id. A test simulates a fully "compromised" model and checks that the outcome stays correct.
- **Real PostgreSQL:** CI starts a Postgres 16 service, runs every migration up and down, and runs the webhook flow against it (`tests/integration/test_postgres.py`). Locally it is skipped unless `POSTGRES_TEST_URL` is set.

## 📁 Project structure

```text
ai-code-review-agent/
├── src/reviewer/
│   ├── main.py                  # App factory; the lifespan runs the startup sweep
│   ├── config.py                # pydantic-settings (environment only)
│   ├── logging.py               # structlog JSON logging
│   ├── schemas.py               # Webhook payload and GitHub API models
│   ├── api/                     # health.py, webhooks.py, runs.py (bearer auth)
│   ├── github/                  # signature.py (HMAC), auth.py (PAT), client.py (REST + retries)
│   ├── llm/                     # base.py (retries, repair, metrics), openai.py, anthropic.py,
│   │                            # factory.py, fake.py (tests), records.py (llm_calls rows)
│   ├── agent/
│   │   ├── graph.py             # LangGraph wiring + failure policy
│   │   ├── state.py             # ReviewState, Finding, TestSuggestion, Review
│   │   ├── nodes/               # fetch_context, code_review, test_analysis, final_review
│   │   ├── prompts/             # Markdown prompt templates + untrusted-data delimiters
│   │   ├── files.py             # File filtering, ordering, test mapping
│   │   ├── diff.py              # Hunk parsing, line numbering, chunking
│   │   └── validation.py        # Hallucinated-path and line-number clean-up
│   ├── services/                # review_service.py, comment_formatter.py, sweep.py
│   └── db/                      # models.py, session.py, repo.py (all SQL)
├── alembic/                     # Migrations 0001 (schema), 0002 (verdict, confidence)
├── tests/                       # unit/, integration/, fixtures/, fakes.py
├── scripts/send_test_webhook.py # Signs and sends a webhook to the local app
├── examples/sample-pr/          # Sandbox repo with a deliberate SQL injection
├── docs/                        # tunnel.md, images/
├── Dockerfile · docker-compose.yml · Makefile · .env.example
└── .github/workflows/ci.yml     # Ruff + pytest against a Postgres service
```

## 🧭 Design decisions

- **Background tasks, not a queue.** A queue (Celery, arq) adds a broker and a worker for a service that handles a few PRs at a time. The `runs` table already serves as the job record, and the startup sweep covers the main risk (see limitations). This is a deliberate v1 ceiling.
- **Code enforces, the LLM suggests.** Path validation, line clamping, confidence filtering, the verdict rules and the comment markup are all deterministic code. The model can make the review worse, but it cannot make the bot claim something the data contradicts.
- **PR content is untrusted data.** The diff, file contents, title, description and findings derived from them only ever appear in the user message, inside `<<<BEGIN/END UNTRUSTED … [random-id]>>>` delimiters. The system prompt forbids following instructions found there, and the guards above hold even if the model obeys an injection anyway.
- **Numbered diff lines.** Models are unreliable at deriving line numbers from `@@` hunk headers, so every diff line carries its new-file line number.
- **Per-file patches rather than one unified diff.** They give per-file line ranges for validation, and let the agent skip or truncate files individually.
- **One retry policy, in one place.** SDK-level retries are turned off, so `BaseLLMClient` owns backoff with jitter, counts retries and sums tokens across a structured-output repair round (at most one).
- **Failure policy per node.** `fetch_context` and `code_review` are critical. `test_analysis` and the LLM part of `final_review` degrade gracefully, and the comment says so. LLM calls from failed runs are still recorded.
- **Idempotent at two levels.** The unique `X-GitHub-Delivery` id stops a redelivery from creating a second run. An atomic `queued → running` claim stops the same run from being reviewed twice. The hidden `<!-- pr-review-agent -->` marker makes new pushes update the existing comment.
- **Fail closed.** The webhook without a secret, and `/runs` without a token, answer `503` instead of running open.
- **Ports and fakes.** Agent nodes depend on small protocols (`GitHubPort`, `LLMClient`), so the whole pipeline runs in tests against in-memory fakes.

## ⚠️ Known limitations

- **In-flight reviews are lost on restart.** Reviews run as in-process FastAPI `BackgroundTasks`, so a crash or redeploy while a review is queued or running loses it. This is mitigated by the **startup sweep**, which marks every run left `queued` or `running` as `failed` with a clear reason, so no run stays "running" forever. The PR gets a new review on the next push.
- **The sweep assumes a single process.** With several app instances, one instance restarting would also fail the other instances' in-flight runs. A heartbeat column or a real queue would fix this.
- **`/runs` auth is one static token.** There is no per-user access, expiry or rotation beyond changing the environment variable. `/docs` and `/openapi.json` stay public, but they expose schema only.
- **No concurrency limit.** Every accepted webhook starts a review in the thread pool, so a burst of PRs means a burst of parallel LLM calls.
- **File-filtering heuristics.** Lockfiles, generated files and vendored code are detected by name and directory (`dist/`, `build/`, `vendor/`, `*.min.js`, `*_pb2.py` and so on). A repository that keeps real source in `build/` would have it skipped.
- **Dedupe heuristic.** Findings are merged when they share a file and category and have the same title or overlapping lines. Two genuinely different bugs on the same lines can collapse into one.
- **Test mapping by naming convention only.** It covers Python and JS/TS, with no import analysis.
- **Budgets are in characters, not tokens.** At most 4 chunks of about 24,000 characters are reviewed, and the rest of a very large PR is skipped. The comment says so when that happens.
- **One summary comment, posted as the PAT's user.** There are no inline line comments and no bot identity; GitHub App auth would add both.
- **Quality is not yet measured systematically.** Correctness of the pipeline is tested. Review quality has been validated with live runs, not an evaluation set.
- **Cosmetic:** HTML-looking text in LLM prose is escaped, so something like `List<int>` inside inline code can render as `List&lt;int>`.

## 🔭 Future improvements

- **Durable job queue** (Postgres `SKIP LOCKED`, arq or Celery) with heartbeats, retries and a concurrency limit.
- **GitHub App authentication:** its own bot identity, per-installation tokens and higher rate limits.
- **Inline review comments** on the exact lines through the Pull Request Reviews API, alongside the summary.
- **Per-repository configuration** (`.pr-review.yml`): ignore paths, thresholds, severity floor, language hints.
- **An evaluation harness**, meaning a labelled set of PRs to measure precision and recall and to compare prompts and models before changing them.
- **Cost tracking:** price per model and cost per run and per repository in `/runs`.
- **Metrics and tracing:** Prometheus counters and OpenTelemetry spans per node and per LLM call.
- **Richer context:** token-aware chunking, and retrieval of callers and related code beyond the diff.
- **Skip unchanged commits:** no second review for the same PR and head SHA.
- **A small dashboard** over `/runs`.

## 🎬 Demo

<p align="center"><img src="docs/images/demo.gif" alt="Demo: a PR update triggers the review comment" width="820"/></p>

---

<div align="center"><sub>Built with FastAPI, LangGraph and PostgreSQL. Reviews are automated and should be verified before acting on them.</sub></div>
