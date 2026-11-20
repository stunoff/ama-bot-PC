#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
for check in enterprise process shifts requests personnel operations contact refunds webhooks reports wiring menu sla scheduler request_control cash_operations_sla operator_handoff operator_shift sales_review attendance accounting_correction readiness route_probe checklist_setup enterprise_wizard scenario_activation refund_report refund_guest; do
    printf '%s\n' "Running $check"
    docker compose "$@" exec -T app python - < "scripts/smoke/$check.py"
done
