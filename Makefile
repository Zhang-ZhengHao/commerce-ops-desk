SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

VENV ?= .venv
PYTHON ?= $(abspath $(VENV))/bin/python
TEST ?= tests

.PHONY: help setup run backend-test project-test frontend-test sqlite-migrate-test postgres-migration-test postgres-integration-test postgres-test hosted-smoke e2e-smoke public-scan lint build verify

help:
	@echo "CommerceOps Desk development targets"
	@echo "  setup                Install locked Python, Node, and browser dependencies"
	@echo "  run                  Build and start the local app from .env or safe defaults"
	@echo "  verify               Run the complete I03 verification gate"
	@echo "  backend-test         Run backend tests (override with TEST=tests/test_health.py)"
	@echo "  frontend-test        Run frontend component tests"
	@echo "  sqlite-migrate-test  Prove an empty SQLite database can migrate to head"
	@echo "  postgres-migration-test  Prove PostgreSQL migrations and selected constraints"
	@echo "  postgres-integration-test  Prove PostgreSQL runtime and concurrency behavior"
	@echo "  postgres-test       Run every non-container PostgreSQL proof"
	@echo "  hosted-smoke         Start the hosted entrypoint on a random port and probe it"
	@echo "  e2e-smoke            Run Playwright entry smoke tests"
	@echo "  public-scan          Scan the worktree and reachable Git history"
	@echo "  lint                 Run Python lint/format/type and TypeScript checks"
	@echo "  build                Produce the frontend bundle served by FastAPI"

setup:
	COMMERCE_OPS_LOCAL_VENV_DIR="$(abspath $(VENV))" bash scripts/setup-local.sh

run: build
	bash scripts/start-local.sh

backend-test:
	cd backend && "$(PYTHON)" -m pytest "$(TEST)"

project-test: build
	"$(PYTHON)" -m pytest scripts/tests

frontend-test:
	npm --prefix frontend test

sqlite-migrate-test:
	cd backend && "$(PYTHON)" -m pytest tests/test_migrations.py

postgres-migration-test:
	cd backend && "$(PYTHON)" -m pytest postgres_tests/test_harness.py postgres_tests/test_migrations.py

postgres-integration-test:
	cd backend && "$(PYTHON)" -m pytest postgres_tests/test_workflow.py postgres_tests/test_concurrency.py

postgres-test: postgres-migration-test postgres-integration-test

hosted-smoke: build
	COMMERCE_OPS_VENV_DIR="$(abspath $(VENV))" PYTHON="$(PYTHON)" bash scripts/hosted-smoke.sh

e2e-smoke:
	COMMERCE_OPS_VENV_DIR="$(abspath $(VENV))" npm --prefix frontend run test:e2e

public-scan:
	bash scripts/scan-public-history.sh

lint:
	"$(PYTHON)" -m ruff check backend scripts
	"$(PYTHON)" -m ruff format --check backend scripts
	cd backend && "$(PYTHON)" -m mypy app alembic tests postgres_tests
	npm --prefix frontend run typecheck

build:
	npm --prefix frontend run build

verify: backend-test project-test frontend-test sqlite-migrate-test hosted-smoke e2e-smoke public-scan lint build
