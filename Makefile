.PHONY: help install fmt lint types test check \
        dev run migrate psql db-reset db-wipe \
        login chats channel index markets book \
        config account user

.DEFAULT_GOAL := help

help:  ## List these targets
	@grep -hE '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) \
	  | sed -e 's/:[^#]*## /|/' \
	  | awk -F'|' '{printf "  %-10s %s\n", $$1, $$2}'

install:  ## Sync the virtualenv from uv.lock
	uv sync

fmt:  ## Format, and autofix what lints
	uv run ruff format .
	uv run ruff check --fix .

lint:  ## Check formatting and lint
	uv run ruff format --check .
	uv run ruff check .

types:  ## mypy, strict
	uv run mypy

test:  ## pytest
	uv run pytest

check: lint types test  ## lint + types + test

dev:  ## Dev Postgres + the app, via process-compose
	process-compose up

run:  ## The service alone, in this shell
	uv run python -m autobet run

migrate:  ## Apply pending migrations
	uv run python -m autobet migrate

psql:  ## A shell on the dev database
	docker compose exec postgres psql -U postgres -d autobet

db-reset:  ## Delete the dev volume and start a fresh server
	docker compose down -v
	docker compose up -d postgres

db-wipe:  ## Empty the schema, keeping the server
	docker compose exec postgres psql -U postgres -d autobet -c \
	  "DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION postgres;"

login:  ## Interactive auth; writes data/telethon.session
	uv run python -m autobet login

chats:  ## Every chat id this session can see
	uv run python -m autobet chats

channel:  ## Watched chats; ARGS="enable <chat_id>"
	uv run python -m autobet channel $(ARGS)

index:  ## Walk the board into the events table
	uv run python -m autobet index

markets:  ## Add bet types to data/markets.json; needs an index
	uv run python -m autobet markets

book:  ## The book's endpoints; ARGS="<key> <value>", ARGS=--enable
	uv run python -m autobet book $(ARGS)

config:  ## Stored settings; ARGS="<key> <value>", --reveal shows secrets
	uv run python -m autobet config $(ARGS)

account:  ## Accounts tips are staked through; ARGS="add <email>"
	uv run python -m autobet account $(ARGS)

user:  ## Each person's stake and limits; ARGS="<email> <key> <value>"
	uv run python -m autobet user $(ARGS)
