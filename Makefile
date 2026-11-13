.PHONY: lint type test sim eval experiment langfuse-env langfuse-up langfuse-down langfuse-hello

lint:
	uv run ruff check .
	uv run ruff format --check .

type:
	uv run mypy

test:
	uv run pytest

# shortcut: these modules land in M1 (sim), M5 (eval) and M6 (experiment); the targets fail until then.
sim:
	uv run python -m pricing_watch.sim $(ARGS)

eval:
	uv run python -m pricing_watch.evals $(ARGS)

experiment:
	uv run python -m experiments $(ARGS)

# Self-hosted Langfuse: see deploy/docker-compose.yml
COMPOSE = docker compose -f deploy/docker-compose.yml --env-file deploy/.env

langfuse-env:
	sh deploy/make-env.sh

langfuse-up:
	$(COMPOSE) up -d --wait

langfuse-down:
	$(COMPOSE) down

langfuse-hello:
	uv run --env-file deploy/.env pricing-watch langfuse hello
