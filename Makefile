.PHONY: up down logs install dev migrate test lint format

up:  ## Build and start the whole stack (app + Postgres)
	@test -f .env || cp .env.example .env
	docker compose up --build -d
	@echo "API: http://localhost:8000/health  |  docs: http://localhost:8000/docs"

down:  ## Stop the stack
	docker compose down

logs:  ## Tail app logs
	docker compose logs -f app

install:  ## Install the project with dev dependencies
	pip install -e ".[dev]"

dev:  ## Run the API locally with auto-reload (needs DATABASE_URL in .env)
	uvicorn reviewer.main:create_app --factory --reload

migrate:  ## Apply database migrations
	alembic upgrade head

test:  ## Run the test suite
	pytest

lint:  ## Ruff lint + format check
	ruff check .
	ruff format --check .

format:  ## Auto-format and fix lint issues
	ruff check --fix .
	ruff format .
