.PHONY: lint type test sim eval gate dataset experiment langfuse-env langfuse-up langfuse-down langfuse-hello

lint:
	uv run ruff check .
	uv run ruff format --check .

type:
	uv run mypy

test:
	uv run pytest

sim:
	uv run pricing-watch sim $(ARGS)

# the evaluator worker; `make eval ARGS=--once` polls once and exits
eval:
	uv run --env-file deploy/.env pricing-watch evals worker $(ARGS)

# the PR gate, locally: every scenario in memory with the stand-in; free, no Langfuse needed
gate:
	uv run python -m experiments.run --offline --gate

# sync the scenario catalogue to the Langfuse dataset (writes only changed scenarios)
dataset:
	uv run --env-file deploy/.env python -m experiments.sync_dataset

# an experiment run on the Langfuse dataset; e.g. make experiment ARGS="--run-name pr-1 --gate"
experiment:
	uv run --env-file deploy/.env python -m experiments.run $(ARGS)

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
