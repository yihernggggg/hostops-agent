---
category: system_llm_latency
title: Agent latency high
severity: medium
default_actions: note, escalate_host
keywords: latency, slow, agent, llm, model, timeout, p90, duration, AgentLatencyHigh
---
# Agent latency high

Fires when the triage agent's p90 duration is over 10 seconds.

## Triage
1. `query_metrics agent_duration` and `query_metrics llm_errors`.
2. If the provider is slow (not erroring), guests are still served but replies are delayed. Note it.
3. Escalate to the host so they can watch the approval queue manually or switch provider.
