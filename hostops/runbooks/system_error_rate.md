---
category: system_error_rate
title: HTTP 5xx error rate high
severity: high
default_actions: escalate_host, note
keywords: 5xx, error, errors, 500, http, server, failing, exception, ErrorRateHigh, PendingApprovalsHigh, approvals, backlog
---
# HTTP error rate high

Fires when more than 5% of requests return 5xx over 5 minutes.

## Triage
1. `query_metrics http_request_duration` to see which route is failing.
2. `search_logs` with level ERROR for exceptions.
3. Escalate to the host; a broken API means guest messages may be dropped. Note the incident.

## Pending approvals backlog
If PendingApprovalsHigh fires, actions are waiting on a human for over 10 minutes. Escalate to the host.
