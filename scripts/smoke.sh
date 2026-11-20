#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
for check in enterprise process shifts requests personnel operations contact refunds webhooks reports; do
    printf '%s\n' "Running $check"
    docker compose "$@" exec -T app python - < "scripts/smoke/$check.py"
done
