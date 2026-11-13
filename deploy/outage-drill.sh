#!/bin/sh
# Outage drill: the Collector loses its network to Langfuse for 10 wall-clock minutes, a traced
# run happens during the outage, and the Collector restarts inside it. After reconnecting,
# Langfuse must hold exactly the spans the run sent. Run from the repo root with the stack up.
#
#   sh deploy/outage-drill.sh [scenario] [outage minutes]
set -eu
scenario=${1:-scenarios/unsafe-model.yaml}
minutes=${2:-10}
run_name="drill-$(date +%Y%m%d-%H%M%S)"
compose="docker compose -f deploy/docker-compose.yml --env-file deploy/.env"
collector=$($compose ps -q otel-collector)
# only the Collector's link to Langfuse is cut; its port (on the edge network) stays reachable
network=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$collector" | tr ' ' '\n' | grep '_backend$')
export LANGFUSE_TRACING_ENVIRONMENT=ci

echo "cutting the Collector off from Langfuse ($network) for $minutes minutes"
docker network disconnect "$network" "$collector"
trap 'docker network connect "$network" "$collector" 2>/dev/null || true' EXIT  # never leave it cut
start=$(date +%s)

sent=$(uv run --env-file deploy/.env pricing-watch sim run --scenario "$scenario" --trace \
    --run-name "$run_name" | sed -n 's/^spans sent to the Collector: //p')
[ -n "$sent" ] || { echo "the traced run failed; nothing to check" >&2; exit 1; }
echo "run $run_name sent $sent spans during the outage"

sleep 60
echo "restarting the Collector mid-outage"
docker restart "$collector" >/dev/null

remaining=$(( minutes * 60 - ($(date +%s) - start) ))
[ "$remaining" -gt 0 ] && { echo "waiting ${remaining}s more"; sleep "$remaining"; }

echo "reconnecting"
docker network connect "$network" "$collector"
trap - EXIT
uv run --env-file deploy/.env pricing-watch langfuse verify-spans --scenario "$scenario" \
    --run-name "$run_name" --expected "$sent" --timeout 600
