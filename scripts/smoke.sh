#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
for check in enterprise process shifts requests personnel operations contact refunds webhooks reports wiring menu sla scheduler request_control cash_operations_sla operator_handoff operator_shift sales_review attendance accounting_correction; do
    printf '%s\n' "Running $check"
    docker compose "$@" exec -T app python - < "scripts/smoke/$check.py"
done
