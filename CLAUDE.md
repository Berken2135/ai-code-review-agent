# CLAUDE.md — Project rules (permanent)

Project: AI Code Review Agent — a FastAPI + LangGraph service that reviews GitHub Pull Requests.

## Rules

1. **Never run any git command** (`git init`, `add`, `commit`, `push`, ...). The owner does all git operations. Only create and edit files.
2. **Everything in English**: code, comments, docstrings, docs and the README.
3. **Never hard-code secrets.** Use environment variables only. `.env` stays in `.gitignore`; `.env.example` is kept up to date with every new variable.
4. **Work phase by phase.** After each phase: stop, summarize what was built, run `ruff` and `pytest`, show the real output, and wait for the owner's approval before starting the next phase.
5. **The README is written in the LAST phase only.** It must be employer-facing and visually rich:
   - badges: Python, FastAPI, LangGraph, PostgreSQL, Docker, CI, tests, ruff
   - Mermaid architecture diagram and Mermaid agent-flow diagram
   - tech stack table, project structure, quickstart with one-command setup (`make up`)
   - configuration table for env vars, API reference
   - design decisions, known limitations, future improvements
   - known limitations must explicitly include the BackgroundTasks limit: an in-flight review is lost if the app restarts, mitigated by the startup sweep that marks stuck runs as `failed`
   - placeholder slots in `docs/images/` for: a screenshot of a real bot review comment, a demo GIF, and a sample `/runs` output

6. **Security and network hygiene.** Never print or log secrets. Never read `.env` contents into any output. Every real network call (GitHub, LLM APIs) is opt-in and made only by the owner running the app or a script. Automated tests stay fully offline; a conftest guard blocks non-loopback sockets, and LLM tests use a mock transport, not respx (the OpenAI/Anthropic SDKs use `httpx2`).

## Decisions (approved)

- GitHub auth: Personal Access Token (PAT). GitHub App auth is future work.
- LLM: OpenAI by default, Anthropic as the second provider, selected via `LLM_PROVIDER` / `LLM_MODEL`.
- Job execution: in-process FastAPI `BackgroundTasks`, no queue.
- Output: one summary PR comment per run.
- Stack: Python 3.12+, FastAPI, LangGraph, SQLAlchemy 2.0 + Alembic (psycopg 3), PostgreSQL, structlog, tenacity, httpx, pytest, Ruff, Docker Compose.

## Architecture

Layers depend inward only: `api -> services -> agent -> (github, llm, db)`. Agent nodes receive `GitHubClient` / `LLMClient` by injection so they are testable with fakes. All SQL lives in `db/repo.py`.

## Phases

1. Skeleton (config, logging, /health, Docker, Alembic schema, CI)
2. GitHub integration (signature, webhook, client)
3. LLM layer (providers, retries, token/latency capture)
4. Agent (state, prompts, nodes, graph)
5. Service + persistence + `/runs` endpoints
6. Polish + README

## Commands

- `make up` / `make down` — run the stack with Docker Compose (Windows without `make`: `docker compose up --build -d` / `docker compose down`)
- `make test` / `make lint` / `make format` — pytest / ruff check + format check / ruff format
- `make migrate` — run Alembic migrations (the container also runs them on start)
- `python scripts/send_test_webhook.py --repo owner/name --pr N` — live run: send a signed webhook to the local app
- `examples/sample-pr/` — base and change files for a throwaway test repo (SQL injection + missing test)
