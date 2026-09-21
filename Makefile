.PHONY: install fmt lint types test check dev run login chats migrate markets index psql db-reset db-wipe

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

test:
	uv run pytest

check: lint types test

dev:
	process-compose up

run:
	uv run python -m autobet run

login:
	uv run python -m autobet login

chats:
	uv run python -m autobet chats

migrate:
	uv run python -m autobet migrate

markets:
	uv run python -m autobet markets

config:
	uv run python -m autobet config $(ARGS)

account:
	uv run python -m autobet account $(ARGS)

book:
	uv run python -m autobet book $(ARGS)

channel:
	uv run python -m autobet channel $(ARGS)

index:
	uv run python -m autobet index

psql:
	docker compose exec postgres psql -U postgres -d autobet

db-reset:
	docker compose down -v
	docker compose up -d postgres

db-wipe:
	docker compose exec postgres psql -U postgres -d autobet -c \
	  "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

