# Local development entry points.
#
# Native execution is the default deployment (ADR-0009), so Maestro runs through uv while
# PostgreSQL runs in its pinned container. The database opts into a loopback-only exposure
# that the hardened deployment never uses.

SHELL := /bin/bash
.DEFAULT_GOAL := help

REPO ?= $(CURDIR)/tests/fixtures/codebase
SECRETS ?= $(CURDIR)/.local/secrets
# Not named PGPORT: that is a libpq variable, and Maestro rejects ambient libpq
# configuration for Audit, so exporting it would stop the server from starting.
DB_PORT ?= 5433
PROJECT ?= maestro-dev
REPS ?= 3
ARMS ?= both
EFFORT ?= medium
EVAL_REPORT ?= $(CURDIR)/.local/eval-report.json
GITHUB_REPOSITORY ?= ansperson/maestro
AUTHORITY_DOCUMENTS ?= $(CURDIR)/docs/authority/rules.md
PROJECT_NAME ?= maestro
IMAGE ?= maestro-verifier:local
ROLES := bootstrap migration writer reader

COMPOSE := MAESTRO_ALLOWED_ROOTS="$(REPO)" \
	MAESTRO_DOCKER_PROJECT="$(PROJECT)" \
	MAESTRO_DOCKER_IMAGE="$(IMAGE)" \
	MAESTRO_DOCKER_UID="$$(id -u)" \
	MAESTRO_DOCKER_GID="$$(id -g)" \
	MAESTRO_AUDIT_BOOTSTRAP_PASSWORD_FILE="$(SECRETS)/bootstrap-password" \
	MAESTRO_AUDIT_MIGRATION_PASSWORD_FILE="$(SECRETS)/migration-password" \
	MAESTRO_AUDIT_WRITER_PASSWORD_FILE="$(SECRETS)/writer-password" \
	MAESTRO_AUDIT_READER_PASSWORD_FILE="$(SECRETS)/reader-password" \
	uv run python scripts/maestro_compose.py

# Native admin and server connect over the loopback exposure rather than the internal network.
ADMIN := MAESTRO_AUDIT_BOOTSTRAP_HOST=127.0.0.1 MAESTRO_AUDIT_BOOTSTRAP_PORT=$(DB_PORT) \
	MAESTRO_AUDIT_BOOTSTRAP_DATABASE=maestro MAESTRO_AUDIT_BOOTSTRAP_USER=postgres \
	MAESTRO_AUDIT_BOOTSTRAP_PASSWORD_FILE="$(SECRETS)/bootstrap-password" \
	MAESTRO_AUDIT_MIGRATION_HOST=127.0.0.1 MAESTRO_AUDIT_MIGRATION_PORT=$(DB_PORT) \
	MAESTRO_AUDIT_MIGRATION_DATABASE=maestro \
	MAESTRO_AUDIT_MIGRATION_USER=maestro_audit_migrator \
	MAESTRO_AUDIT_MIGRATION_PASSWORD_FILE="$(SECRETS)/migration-password" \
	MAESTRO_AUDIT_WRITER_HOST=127.0.0.1 MAESTRO_AUDIT_WRITER_PORT=$(DB_PORT) \
	MAESTRO_AUDIT_WRITER_DATABASE=maestro \
	MAESTRO_AUDIT_WRITER_USER=maestro_audit_writer \
	MAESTRO_AUDIT_WRITER_PASSWORD_FILE="$(SECRETS)/writer-password" \
	MAESTRO_AUDIT_READER_HOST=127.0.0.1 MAESTRO_AUDIT_READER_PORT=$(DB_PORT) \
	MAESTRO_AUDIT_READER_DATABASE=maestro \
	MAESTRO_AUDIT_READER_USER=maestro_audit_reader \
	MAESTRO_AUDIT_READER_PASSWORD_FILE="$(SECRETS)/reader-password"

.PHONY: help secrets db-up db-down bootstrap migrate up run ask authority eval read status verify clean

help: ## Show the available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk -F':.*?## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Override REPO=/path/to/repository to point Maestro at your own checkout."

secrets: ## Generate the four role credentials if they do not exist
	@mkdir -p "$(SECRETS)" && chmod 700 "$(SECRETS)"
	@for role in $(ROLES); do \
		file="$(SECRETS)/$$role-password"; \
		if [ ! -f "$$file" ]; then \
			openssl rand -hex 24 > "$$file" && chmod 0600 "$$file"; \
			echo "  created $$role"; \
		fi; \
	done

db-up: secrets ## Start PostgreSQL with a loopback-only exposure for native development
	@$(COMPOSE) database-up --publish-loopback $(DB_PORT)
	@echo "  PostgreSQL listening on 127.0.0.1:$(DB_PORT)"

db-down: ## Stop the database, keeping its durable volume
	@$(COMPOSE) down

bootstrap: ## Create the separate SCRAM roles (once per database)
	@$(ADMIN) uv run python -m maestro.audit.postgres.admin bootstrap
	@echo "  roles created"

migrate: ## Apply Audit migrations
	@$(ADMIN) uv run python -m maestro.audit.postgres.admin migrate
	@echo "  migrations applied"

up: db-up bootstrap migrate ## Bring up and initialize everything the server needs

run: ## Run the stdio MCP server natively; an MCP client normally spawns this
	@MAESTRO_ALLOWED_ROOTS="$(REPO)" MAESTRO_AGENT_RUNTIME=claude \
		MAESTRO_AUDIT_WRITER_HOST=127.0.0.1 MAESTRO_AUDIT_WRITER_PORT=$(DB_PORT) \
		MAESTRO_AUDIT_WRITER_DATABASE=maestro \
		MAESTRO_AUDIT_WRITER_USER=maestro_audit_writer \
		MAESTRO_AUDIT_WRITER_PASSWORD_FILE="$(SECRETS)/writer-password" \
		uv run maestro

ask: ## Ask one question end to end: make ask Q="Does X hold?"
	@test -n "$(Q)" || { echo "  usage: make ask Q=\"your question\""; exit 2; }
	@uv run python scripts/ask.py "$(REPO)" "$(DB_PORT)" "$(SECRETS)" "$(Q)"

authority: ## Ask whether an action may proceed: make authority ISSUE=26 SUBJECT=x CHOICE=y
	@test -n "$(ISSUE)" -a -n "$(SUBJECT)" -a -n "$(CHOICE)" || { \
		echo "  usage: make authority ISSUE=26 SUBJECT=audit.backend CHOICE=postgresql"; exit 2; }
	@MAESTRO_ALLOWED_ROOTS="$(REPO)" MAESTRO_AGENT_RUNTIME=claude \
		MAESTRO_AUDIT_WRITER_HOST=127.0.0.1 MAESTRO_AUDIT_WRITER_PORT=$(DB_PORT) \
		MAESTRO_AUDIT_WRITER_DATABASE=maestro \
		MAESTRO_AUDIT_WRITER_USER=maestro_audit_writer \
		MAESTRO_AUDIT_WRITER_PASSWORD_FILE="$(SECRETS)/writer-password" \
		MAESTRO_WORKITEM_GITHUB_REPOSITORY="$(GITHUB_REPOSITORY)" \
		MAESTRO_WORKITEM_GITHUB_TOKEN_FILE="$(SECRETS)/github-token" \
		uv run python scripts/check_authority.py \
			--repository "$(REPO)" --project "$(PROJECT_NAME)" --issue "$(ISSUE)" \
			--subject "$(SUBJECT)" --choice "$(CHOICE)" \
			$(foreach doc,$(AUTHORITY_DOCUMENTS),--document "$(doc)")

eval: ## Run the evaluation: make eval [REPS=3] [ARMS=both|tool] [EFFORT=medium]
	@MAESTRO_AGENT_RUNTIME=claude \
		MAESTRO_ALLOWED_ROOTS="$(REPO)" \
		MAESTRO_AUDIT_WRITER_HOST=127.0.0.1 MAESTRO_AUDIT_WRITER_PORT=$(DB_PORT) \
		MAESTRO_AUDIT_WRITER_DATABASE=maestro \
		MAESTRO_AUDIT_WRITER_USER=maestro_audit_writer \
		MAESTRO_AUDIT_WRITER_PASSWORD_FILE="$(SECRETS)/writer-password" \
		MAESTRO_LOG_LEVEL=ERROR \
		uv run python scripts/run_evals.py \
			--repetitions $(REPS) --effort $(EFFORT) $(if $(filter tool,$(ARMS)),--no-control,) \
		> "$(EVAL_REPORT)" 2>/dev/null; \
	status=$$?; \
	uv run python scripts/eval_summary.py "$(EVAL_REPORT)"; \
	echo "  full report: $(EVAL_REPORT)"; \
	exit $$status

read: ## Query the curated read-only Audit views
	@$(ADMIN) uv run python -m maestro.audit.postgres.admin read $(ARGS)

status: ## Show the database container and whether the port answers
	@docker ps --filter "name=$(PROJECT)-audit-postgres" \
		--format '  {{.Names}}  {{.Status}}' || true
	@docker port "$(PROJECT)-audit-postgres-1" 2>/dev/null | sed 's/^/  /' \
		|| echo "  no published port"

verify: ## Run the deterministic gate the CI runs
	@uv run ruff format --check .
	@uv run ruff check .
	@uv run pyright
	@uv run pytest

clean: ## Stop the database and delete its volume and generated credentials
	@$(COMPOSE) down || true
	@docker volume rm "$(PROJECT)_audit-postgres-data" 2>/dev/null || true
	@rm -rf "$(SECRETS)"
	@echo "  removed database volume and generated credentials"
