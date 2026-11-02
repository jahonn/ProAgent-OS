---
id: incident_triage
name: Incident Triage
description: Classify an on-call page against runbook passages and cite the lines used
---

# Incident Triage

Use this skill when a new page or alert arrives.

1. Read the page. Do not answer from memory.
2. Search the private knowledge base for the runbook that governs the symptom.
3. Assign one service: api, database, or general.
4. Assign one severity: sev1 (complete outage, customer data, or down for all customers), sev2 (elevated errors, failover, or degraded service), or sev3 (no customer impact).
5. Reply with the severity, the service, one next step, and a citation (runbook title plus a short quote).
6. If no passage matches, or the page is sev1, say so and emit a follow-up for a human. Do not invent severity rules, rollback steps, or customer updates.

`run.py` is a deterministic keyword triage for the offline demo. It is not a production classifier. Pass `passages` from `kb_search` when you have them; otherwise it reads `verticals/ops_incident/fixtures/`.
