#!/bin/sh
# Write deploy/.env with random secrets and API keys for the local Langfuse stack.
# Never overwrites an existing file: its keys are already in Langfuse's database.
set -eu
env_file="$(dirname "$0")/.env"
if [ -e "$env_file" ]; then
    echo "$env_file exists; leaving it alone"
    exit 0
fi
umask 077
{
    for name in POSTGRES_PASSWORD CLICKHOUSE_PASSWORD REDIS_PASSWORD MINIO_PASSWORD \
                NEXTAUTH_SECRET SALT LANGFUSE_ADMIN_PASSWORD; do
        echo "$name=$(openssl rand -hex 16)"
    done
    echo "ENCRYPTION_KEY=$(openssl rand -hex 32)"
    public_key="pk-lf-$(openssl rand -hex 16)"
    secret_key="sk-lf-$(openssl rand -hex 16)"
    echo "LANGFUSE_PUBLIC_KEY=$public_key"
    echo "LANGFUSE_SECRET_KEY=$secret_key"
    # the Collector's auth header to Langfuse
    echo "LANGFUSE_OTLP_AUTH=$(printf '%s:%s' "$public_key" "$secret_key" | base64 | tr -d '\n')"
    # the app sends spans to the local Collector, not straight to Langfuse
    echo "OTEL_COLLECTOR_ENDPOINT=http://localhost:4318/v1/traces"
    echo "LANGFUSE_BASE_URL=http://localhost:3000"
} > "$env_file"
echo "wrote $env_file"
