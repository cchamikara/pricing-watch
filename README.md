# pricing-watch

An autonomous pricing agent for a **simulated** home-entertainment retailer, and the layer that
watches, scores and constrains it. The store, its customers, competitors and suppliers are all
simulated; no real shop or money is involved

## How it works

![The agent's control path (simulator, triggers, agent, policy, executor, approval page) and the watching layer (Collector, Langfuse, evaluator worker, experiments)](docs/diagrams/architecture.svg)

## Setup

Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync
uv run pre-commit install
make langfuse-env     # once: deploy/.env with random secrets and API keys (git-ignored)
make langfuse-up      # Langfuse, its stores, the Collector, the evaluator worker and the app
uv run --env-file deploy/.env pricing-watch langfuse push-prompts --label production
```

Langfuse is at http://localhost:3000 (user `admin@pricing-watch.local`, password
`LANGFUSE_ADMIN_PASSWORD` in `deploy/.env`); the approval page is at http://localhost:8000.

## Running it

```sh
# one scenario, traced to Langfuse; --model gemini or claude for a real model (costs calls)
uv run --env-file deploy/.env pricing-watch sim run --scenario scenarios/unsafe-model.yaml \
    --trace --run-name my-run

# the PR gate: all 13 scenarios in memory with the stand-in model, free, about 70 s
make gate

# an experiment on the Langfuse dataset, gated; compare runs side by side in Langfuse
make dataset
make experiment ARGS="--run-name pr-1 --gate"

# the agent against its rules baseline over several seeds (free, about 10 minutes)
uv run python -m experiments.multi_seed --seeds 1,2,3

# approvals: leave them for a person, then decide on the page
uv run pricing-watch sim run --scenario scenarios/undercut-weekend.yaml --reviewer person \
    --db runs/run.db
```