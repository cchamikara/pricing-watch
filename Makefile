.PHONY: lint type test sim eval experiment

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
