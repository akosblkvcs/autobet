.PHONY: install fmt lint types check run dev login chats migrate psql db-reset

install:
	uv sync

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

types:
	uv run mypy

check: lint types

run:
	uv run python -m autobet run

dev:
	process-compose up

login:
	uv run python -m autobet login

chats:
	uv run python -m autobet chats

migrate:
	uv run python -m autobet migrate

psql:
	docker compose exec postgres psql -U postgres -d autobet

db-reset:
	docker compose down -v
	docker compose up -d postgres

